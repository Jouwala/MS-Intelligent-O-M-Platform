"""Worker 存活心跳（TASK-03 / TASK-14）。

背景缺陷（2026-11-14 复核属实）：

- 旧实现只在 `run_once()` 开头写一次心跳，长任务（例如 30 分钟）执行期间
  心跳文件不再更新，看门狗按 180s 阈值把**还在干活的 Worker** 当卡死杀掉；
- 旧实现 `write_text()` 直接覆写目标文件，看门狗可能读到半截 JSON 并据此
  断定 Worker 死亡（TASK-14）。

本模块把「Worker 存活」这一件事单独收敛成 `HeartbeatWriter`：

- **原子写**：写同目录临时文件 → `flush()` + `os.fsync()` → `os.replace()`，
  读方任一时刻看到的都是完整旧内容或完整新内容（POSIX 上再 fsync 目录项）；
- **后台线程**：`start()` 后按 interval（默认 30s，可配）持续写；`stop()`
  用事件唤醒而不是干等一个 interval，Worker 退出路径不会被心跳线程拖住；
- **内容**：agent / instance / pid / at / task / state，看门狗与排障都靠它；
- **可注入** clock / sleep：离线自测用假时钟推进 200s / 30 分钟，不需要真的等。

职责边界：本模块只回答「Worker 进程还活着」。任务租约续期是另一条职责，
由调度器 `scheduler.renew(...)` 承担（见 worker.py 的执行期续租回调）——
把两者混在一起会让「进程活着但任务已死」或反之的故障无法区分。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

#: 心跳间隔默认值。看门狗的 stale 阈值必须明显大于它（默认 180s = 6 个周期），
#: 否则一次磁盘抖动就会触发误杀。
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_epoch(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def read_heartbeat_timestamp(path: Path) -> Optional[float]:
    """心跳证据时间戳（epoch 秒）；文件不存在返回 None。

    证据优先级：JSON 里的 `at` → 文件 mtime（解析失败兜底）。看门狗用它与
    「本次实例启动时刻」比较，避免把上一次运行留下的旧心跳当成死亡证据。
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str(data["at"]))
        return _to_epoch(at)
    except (OSError, ValueError, KeyError, TypeError):
        # 文件被并发替换/半截写入/字段缺失：mtime 仍是一个可用证据（TASK-14）
        return stat.st_mtime


def read_heartbeat_payload(path: Path) -> Optional[dict]:
    """读取心跳 JSON 载荷原文；文件不存在/解析失败返回 None。

    与 read_heartbeat_timestamp 不同，这里不做 mtime 兜底：调用方（看门狗
    的孤儿回收）要的是载荷里的 pid / task_pid 等字段，半截 JSON 猜不出
    字段，宁可按「无记录」处理，不拿过期猜测去杀进程。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_heartbeat_age(path: Path, now_ts: float) -> Optional[float]:
    """读取心跳年龄（秒）；文件不存在返回 None。

    **解析失败不等于死亡**（TASK-14 的验收点）：JSON 读不出来 / 缺 `at` /
    时间串非法时退回文件 mtime 判定；两种证据都拿不到才返回 None，
    由调用方按「尚无本次实例心跳」处理（例如刚启动的宽限期）。
    """
    ts = read_heartbeat_timestamp(path)
    if ts is None:
        return None
    return max(0.0, now_ts - ts)


class HeartbeatWriter:
    """原子写心跳文件 + 可选后台线程持续刷新。"""

    def __init__(
        self,
        path: Path,
        *,
        agent_id: str,
        instance_id: str,
        interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        clock: Callable[[], datetime] = utcnow,
        sleep: Optional[Callable[[float], None]] = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.path = Path(path)
        self.agent_id = agent_id
        self.instance_id = instance_id
        self.interval_s = max(0.1, float(interval_s))
        self.clock = clock
        # sleep=None 时用 Event.wait：stop() 能立刻唤醒，不会干等一个 interval
        self._sleep = sleep
        self.log = log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._task_id = ""
        self._state = "idle"
        self._task_pid: Optional[int] = None
        self._task_pgid: Optional[int] = None
        self.writes = 0

    # -- 状态 ---------------------------------------------------------------
    def update(
        self,
        *,
        task_id: Optional[str] = None,
        state: Optional[str] = None,
        task_pid: Optional[int] = None,
        task_pgid: Optional[int] = None,
    ) -> None:
        """更新心跳附加状态（执行期 task/state 会变，看门狗与排障按它定位）。

        task_pid / task_pgid 是当前任务子进程的 pid 与进程组 id（#77 孤儿
        回收的唯一持久线索）：任务启动时写入，任务树确认退出后传 0 清除。
        None 表示本次不改动（与 task_id/state 的口径一致）。
        """
        with self._lock:
            if task_id is not None:
                self._task_id = task_id
            if state is not None:
                self._state = state
            if task_pid is not None:
                self._task_pid = task_pid if task_pid > 0 else None
            if task_pgid is not None:
                self._task_pgid = task_pgid if task_pgid > 0 else None

    def payload(self) -> dict:
        with self._lock:
            return {
                "agent": self.agent_id,
                "instance": self.instance_id,
                "pid": os.getpid(),
                "at": self.clock().astimezone(timezone.utc).isoformat(),
                "task": self._task_id,
                "state": self._state,
                "task_pid": self._task_pid,
                "task_pgid": self._task_pgid,
            }

    # -- 原子写 -------------------------------------------------------------
    _REPLACE_RETRIES = (0.0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.1)

    def _replace(self, tmp: Path) -> None:
        """os.replace 原子替换；Windows 上并发读句柄会短暂导致 Access denied。

        看门狗/排障随时可能正在读心跳文件，而 Windows 的 CRT open 不带
        FILE_SHARE_DELETE，替换目标被打开时 os.replace 会抛 PermissionError。
        这是**短暂的读窗口**而不是写失败，重试几次即可（POSIX 上通常一次成功）。
        """
        last_exc: Optional[OSError] = None
        for delay in self._REPLACE_RETRIES:
            if delay > 0:
                time.sleep(delay)
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError as exc:  # Windows 并发读窗口
                last_exc = exc
        if last_exc is not None:
            raise last_exc

    def _atomic_write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False)
        # 临时文件必须与目标同目录：跨文件系统 os.replace 会退化成复制+删除
        tmp = self.path.with_name(
            f"{self.path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            self._replace(tmp)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        if os.name == "posix":
            # 目录项也落盘，掉电后不会出现「文件在但内容空」；Windows 无此语义
            try:
                dir_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass

    def write(self) -> dict:
        """写一次心跳，返回写入的 payload（调用方/测试可直接检查）。"""
        with self._lock:
            return self._write_locked()

    def _write_locked(self) -> dict:
        payload = {
            "agent": self.agent_id,
            "instance": self.instance_id,
            "pid": os.getpid(),
            "at": self.clock().astimezone(timezone.utc).isoformat(),
            "task": self._task_id,
            "state": self._state,
            "task_pid": self._task_pid,
            "task_pgid": self._task_pgid,
        }
        self._atomic_write(payload)
        self.writes += 1
        return payload

    # -- 后台线程 -----------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动后台心跳线程；重复调用幂等。"""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{self.agent_id}", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.write()
            except OSError as exc:  # 心跳写失败不能让 Worker 执行线程崩掉
                self.log(f"[heartbeat] 写心跳失败：{exc}")
            if self._sleep is not None:
                self._sleep(self.interval_s)
            else:
                self._stop.wait(self.interval_s)

    def stop(self, timeout_s: float = 5.0) -> None:
        """停止后台线程并 join；对未启动/已停止的实例幂等。"""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout_s)
            if thread.is_alive():
                self.log("[heartbeat] 心跳线程未在超时内退出（daemon 由进程退出兜底）")

    def __enter__(self) -> "HeartbeatWriter":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def heartbeat_path(base: Path, agent_id: str) -> Path:
    """心跳文件命名约定（worker.py 与 watchdog.py 必须一致）。"""
    return Path(base) / f".heartbeat-{agent_id}.json"
