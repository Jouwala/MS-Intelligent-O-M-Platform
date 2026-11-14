"""Worker 看门狗（TASK-03）。

职责：拉起 Worker 子进程，周期性读心跳文件；心跳过期（Worker 卡死）
或进程退出即重启，重启次数有上限，超限退出并报告。

旧实现的三个缺陷（2026-11-14 复核属实，必须修）：

1. **重启前只 `terminate()` 不等待**：旧进程可能还没死就拉起新实例，
   两台 Worker 同时抢同一批任务（双实例）；严重时旧进程还持有 worktree。
   现在必须走「SIGTERM → 宽限 → 轮询确认退出 → SIGKILL（必要时进程组）
   → `wait()` 回收」，**旧进程未确认退出绝不启动新实例**；
2. **心跳解析失败直接当死亡**：看门狗读到半截 JSON 就杀掉正在写心跳的
   Worker。现在解析失败回退文件 mtime（见 heartbeat.read_heartbeat_age），
   两种证据都拿不到才按「尚无心跳」处理，并且 stale 阈值（默认 180s）
   明显大于 Worker 心跳间隔（默认 30s），不会因一次写抖动误杀；
3. **不处理 SIGTERM/SIGINT**：看门狗被任务计划/运维停止时子进程会孤儿化。
   现在注册信号处理，收到信号先终止并确认子进程退出，再以 0 退出。

这些缺陷之外的第四个问题（2026-11-14 Lead 复审）：

4. **旧心跳文件导致重启后立即误杀**：旧进程被杀后心跳文件仍留在磁盘上，
   新实例刚拉起、还没写出自己的心跳时，看门狗会把「200s 前的旧文件」
   当成卡死证据，立刻再重启，直到 max_restarts 放弃。现在 `_is_stale()`
   会把心跳证据时间戳与本次实例的 `_started_at` 比较：**证据早于本次启动
   ⇒ 视为尚无本次心跳**，只按 `start_grace_s` 宽限判断。

可注入 process_factory / clock / sleep：离线自测不需要真的等时间流逝。

心跳文件命名约定与 worker.py 一致：`<repo>/.agent-worktrees/.heartbeat-<agent>.json`。
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: E402

_heartbeat = _bootstrap.load_sibling("heartbeat")


@dataclass
class WatchdogConfig:
    stale_after_s: float = 180.0      # 心跳超过这个秒数没更新 → 判定卡死
    check_interval_s: float = 15.0    # 看门狗自身检查间隔
    heartbeat_interval_s: float = 30.0  # Worker 心跳间隔（stale 必须明显大于它）
    max_restarts: int = 5             # 累计重启上限，超限放弃并以非零码退出
    start_grace_s: float = 60.0       # 刚拉起后的宽限期（首个心跳写出前不算卡死）
    term_grace_s: float = 10.0        # SIGTERM 后等待退出的宽限
    kill_wait_s: float = 10.0         # SIGKILL 后等待确认退出的上限


def _default_process_factory(cmd: list[str]) -> object:
    kwargs: dict = {}
    if os.name == "posix":
        # 新 Session：看门狗一次 killpg 能覆盖 Worker 及其执行期子进程
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


class Watchdog:
    def __init__(
        self,
        worker_cmd: list[str],
        heartbeat_file: Path,
        config: WatchdogConfig | None = None,
        process_factory: Optional[Callable[[list[str]], object]] = None,
        clock: Callable[[], float] = time.time,
        sleep: Optional[Callable[[float], None]] = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.worker_cmd = worker_cmd
        self.heartbeat_file = Path(heartbeat_file)
        self.config = config or WatchdogConfig()
        self.process_factory = process_factory or _default_process_factory
        self.clock = clock
        # sleep=None 时用 Event.wait：信号/外部 stop 能立刻唤醒主循环
        self._sleep = sleep
        self.log = log
        self.restarts = 0
        self._proc: Optional[object] = None
        self._started_at = 0.0
        self._wake = threading.Event()
        self._stop_requested = False
        self._saved_handlers: dict[int, object] = {}

    # -- 心跳 ---------------------------------------------------------------------
    def heartbeat_timestamp(self) -> Optional[float]:
        """心跳证据时间戳（epoch 秒）；不存在返回 None。解析失败按 mtime 兜底。"""
        return _heartbeat.read_heartbeat_timestamp(self.heartbeat_file)

    def heartbeat_age(self) -> Optional[float]:
        """心跳文件年龄（秒）；文件不存在返回 None。解析失败按 mtime 兜底。"""
        return _heartbeat.read_heartbeat_age(self.heartbeat_file, self.clock())

    def _is_stale(self) -> bool:
        ts = self.heartbeat_timestamp()
        elapsed = self.clock() - self._started_at
        if ts is None or ts < self._started_at:
            # 文件不存在，或磁盘上只是**上一次运行**留下的旧心跳：不能当成本次
            # 实例的死亡证据，只按启动宽限期判断（否则重启后会立即反复误杀）。
            return elapsed > self.config.start_grace_s
        return self.clock() - ts > self.config.stale_after_s

    # -- 进程管理 -------------------------------------------------------------------
    def _poll(self, proc: object) -> Optional[int]:
        try:
            return proc.poll()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 已回收/假进程都可以视为退出
            return 0

    def _reap(self, proc: object) -> bool:
        """确认进程已退出并回收；返回是否确认。"""
        if self._poll(proc) is None:
            return False
        try:
            proc.wait(timeout=1.0)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 已回收或假进程没有 wait
            pass
        return True

    def _signal(self, proc: object, *, force: bool) -> None:
        """向 Worker 及其子进程发信号：POSIX killpg；Windows taskkill /T。"""
        pid = getattr(proc, "pid", None)
        if os.name == "posix" and isinstance(pid, int):
            sig = signal.SIGKILL if force else signal.SIGTERM
            try:
                os.killpg(os.getpgid(pid), sig)
                return
            except (ProcessLookupError, PermissionError):
                pass
        if os.name != "posix" and isinstance(pid, int):
            args = ["taskkill", "/T", "/PID", str(pid)]
            if force:
                args.insert(1, "/F")
            try:
                subprocess.run(args, capture_output=True, timeout=30)
                return
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            (proc.kill if force else proc.terminate)()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    def _wait_exit(self, proc: object, timeout_s: float) -> bool:
        deadline = self.clock() + timeout_s
        poll_s = 0.2 if timeout_s >= 1 else max(0.01, timeout_s / 10)
        while self.clock() < deadline:
            if self._reap(proc):
                return True
            if self._sleep is not None:
                self._sleep(poll_s)
            else:
                time.sleep(poll_s)
        return self._reap(proc)

    def _stop(self) -> bool:
        """SIGTERM → 宽限 → SIGKILL → wait，确认退出才算停干净。返回是否确认。"""
        proc = self._proc
        if proc is None:
            return True
        if self._reap(proc):
            self._proc = None
            return True
        self.log("[watchdog] 停止 Worker：SIGTERM")
        self._signal(proc, force=False)
        if self._wait_exit(proc, self.config.term_grace_s):
            self._proc = None
            return True
        self.log("[watchdog] Worker 未在宽限内退出，升级 SIGKILL")
        self._signal(proc, force=True)
        if self._wait_exit(proc, self.config.kill_wait_s):
            self._proc = None
            return True
        self.log("[watchdog] 严重：Worker 未确认退出，保留句柄且不再拉起新实例")
        return False

    def _start(self) -> None:
        self._proc = self.process_factory(self.worker_cmd)
        self._started_at = self.clock()
        self.log(f"[watchdog] Worker 已拉起：{' '.join(self.worker_cmd)}")

    def _restart(self, reason: str) -> bool:
        """重启 Worker；超过上限返回 False 表示放弃。旧实例未确认退出也返回 False。"""
        if self.restarts >= self.config.max_restarts:
            self.log(f"[watchdog] 重启已达上限 {self.config.max_restarts} 次，放弃：{reason}")
            return False
        self.restarts += 1
        self.log(f"[watchdog] 第 {self.restarts} 次重启：{reason}")
        if not self._stop():
            # 绝不双实例：宁可放弃重启并退出，也不让两个 Worker 同时跑
            return False
        self._start()
        return True

    # -- 信号 ---------------------------------------------------------------------
    def request_stop(self) -> None:
        self._stop_requested = True
        self._wake.set()

    def _handle_signal(self, signum: int, _frame: object = None) -> None:
        self.log(f"[watchdog] 收到信号 {signum}，准备停止 Worker")
        self.request_stop()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._saved_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle_signal)
            except (ValueError, OSError):  # 非主线程/受限环境
                self._saved_handlers.pop(sig, None)

    def _restore_signal_handlers(self) -> None:
        for sig, handler in self._saved_handlers.items():
            try:
                signal.signal(sig, handler)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
        self._saved_handlers.clear()

    # -- 主循环 -----------------------------------------------------------------------
    def _sleep_interval(self) -> None:
        if self._sleep is not None:
            self._sleep(self.config.check_interval_s)
        else:
            self._wake.wait(self.config.check_interval_s)
            self._wake.clear()

    def run(self, max_cycles: Optional[int] = None) -> int:
        """返回：0=正常停止（max_cycles 到达或被信号要求停），2=重启失败放弃。"""
        self._install_signal_handlers()
        self._start()
        cycles = 0
        try:
            while True:
                self._sleep_interval()
                if self._stop_requested:
                    self.log("[watchdog] 收到停止请求，终止 Worker 后退出")
                    self._stop()
                    return 0
                cycles += 1
                proc = self._proc
                if proc is None or self._poll(proc) is not None:
                    exit_code = self._poll(proc) if proc is not None else None
                    if not self._restart(f"Worker 进程已退出（exit={exit_code}）"):
                        return 2
                elif self._is_stale():
                    if not self._restart("Worker 心跳过期（疑似卡死）"):
                        return 2
                if max_cycles is not None and cycles >= max_cycles:
                    return 0 if self._stop() else 2
        finally:
            self._restore_signal_handlers()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Worker 看门狗")
    parser.add_argument("--agent", default="muse-2", help="Worker 身份（决定心跳文件名）")
    parser.add_argument("--repo-root", default=".", help="仓库根目录")
    parser.add_argument("--stale-after", type=float, default=180.0)
    parser.add_argument("--max-restarts", type=int, default=5)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    heartbeat = repo_root / ".agent-worktrees" / f".heartbeat-{args.agent}.json"
    worker_py = Path(__file__).resolve().parent / "worker.py"
    cmd = [sys.executable, str(worker_py), "--agent", args.agent, "--repo-root", str(repo_root)]
    config = WatchdogConfig(stale_after_s=args.stale_after, max_restarts=args.max_restarts)
    return Watchdog(cmd, heartbeat, config).run()


if __name__ == "__main__":
    raise SystemExit(main())
