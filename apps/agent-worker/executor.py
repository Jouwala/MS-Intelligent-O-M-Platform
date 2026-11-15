"""任务执行器（TASK-03 / Linux 7.1 / 7.2 / TASK-10 / TASK-20）。

在任务的 Worktree 里执行命令（或注入的 callable，供离线自测），并做四件守卫：

- **范围守卫**：执行后用 git status 核对实际改动文件，超出任务声明范围的
  改动单独标出（out_of_scope），提交时只提交范围内文件；
- **进程树生命周期**：Popen + 独立进程组（POSIX `start_new_session=True`，
  Windows `CREATE_NEW_PROCESS_GROUP`）。超时/取消时先 SIGTERM 整个进程组，
  宽限期后 SIGKILL 整个进程组，最后 `wait()` 回收并确认进程树退出。
  - POSIX：killpg(SIGTERM) → 宽限 → killpg(SIGKILL)，用 `killpg(pgid, 0)`
    确认整个进程组消失；
  - Windows：**根进程存活时**快照整棵树（wmic/CIM 取 PPID 关系）→ 宽限阶段
    只发 CTRL_BREAK/taskkill /T（绝不先 terminate 根，否则 /T 枚举不到树）→
    `taskkill /F /T /PID root` 强杀 → 逐个 tasklist 确认快照子孙消失
    （实现见 `windows_tree.py`；旧实现只杀直接子进程并误报 tree_exited=True，
    孙进程泄漏，Muse 3 验收实测为阻断缺陷）；
- **环境白名单**：任务子进程不继承 Worker 的完整环境（里面可能有 Secret /
  生产凭据），只传 `env_filter.build_task_env()` 过滤出的最小白名单，
  部署追加项须经构造参数 `env_extra_allowed` 显式声明（#78）；
- **取消**：执行期轮询 `cancel_event`，命中即终止进程组，结果带 killed 原因；
- **续租回调**：`on_tick` 每个轮询周期调用一次，Worker 用它续任务租约
  （与心跳文件的「进程存活」是两条独立职责）。

`changed_files()/commit_in_scope()` 的既有行为保持不变。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: E402

_wintree = _bootstrap.load_sibling("windows_tree")
_env_filter = _bootstrap.load_sibling("env_filter")

Command = Union[Sequence[str], Callable[[Path], int]]

#: 终止进程树时「先礼后兵」的宽限秒数与 SIGKILL 后回收等待秒数。
DEFAULT_TERM_GRACE_S = 5.0
DEFAULT_KILL_WAIT_S = 10.0
DEFAULT_POLL_INTERVAL_S = 0.05


@dataclass
class ExecutionResult:
    exit_code: Optional[int]
    duration_s: float
    changed_files: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    log_path: Optional[Path] = None
    summary: str = ""
    #: None / "timeout" / "cancelled"：结果被强制终止的原因（TASK-10 验收点）
    killed_reason: Optional[str] = None
    #: 进程树是否确认退出；False 时调用方不得删除 worktree（子进程可能仍占用目录）
    tree_exited: bool = True
    pid: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.out_of_scope and self.killed_reason is None


def _norm(p: str) -> str:
    return p.replace("\\", "/").strip("/")


def _in_scope(path: str, scope: Sequence[str]) -> bool:
    np = _norm(path)
    for entry in scope:
        ne = _norm(entry)
        if np == ne or np.startswith(ne + "/"):
            return True
    return False


def _pump(stream, chunks: list[str]) -> None:
    """后台读子进程输出，避免管道写满导致子进程阻塞（大输出日志必须这样读）。"""
    try:
        for line in iter(stream.readline, ""):
            chunks.append(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


class Executor:
    def __init__(
        self,
        timeout_s: float = 1800.0,
        *,
        term_grace_s: float = DEFAULT_TERM_GRACE_S,
        kill_wait_s: float = DEFAULT_KILL_WAIT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        windows_snapshot_interval_s: float = 2.0,
        log: Optional[Callable[[str], None]] = None,
        env_extra_allowed: tuple = (),
    ) -> None:
        #: 任务子进程环境白名单的部署追加项（变量名）；默认空——Secret 与
        #: 生产凭据一律不进任务环境，追加必须显式、会随执行日志留痕（#78）。
        self.env_extra_allowed = tuple(env_extra_allowed)
        self.timeout_s = timeout_s
        self.term_grace_s = term_grace_s
        self.kill_wait_s = kill_wait_s
        self.poll_interval_s = max(0.01, poll_interval_s)
        # Windows 执行期每隔这么久快照一次子孙（防「父进程中途退出、孙进程被重新
        # 挂到更上层」后彻底失联）；终止前还会在根存活时再强制快照一次。
        self.windows_snapshot_interval_s = max(0.2, windows_snapshot_interval_s)
        self.log = log

    # -- git 辅助 ---------------------------------------------------------------
    @staticmethod
    def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def changed_files(self, worktree: Path) -> list[str]:
        proc = self._git(worktree, "status", "--porcelain")
        files: list[str] = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            # porcelain 格式：XY <path>，重命名是 old -> new，取 new
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files.append(path.strip('"'))
        return files

    # -- 进程树 ---------------------------------------------------------------
    @staticmethod
    def _spawn(
        argv: list[str], worktree: Path, env: Optional[dict] = None
    ) -> tuple[subprocess.Popen, int]:
        kwargs: dict = {
            "cwd": str(worktree),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "errors": "replace",
            "bufsize": 1,
            # env=None 时 Popen 默认继承父进程环境——任务命令禁止走这条路，
            # run() 永远传白名单过滤后的 env；None 只留给内部防御性调用。
            "env": env,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True  # 新 Session → 新进程组，killpg 可覆盖整棵树
        else:  # Windows
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(argv, **kwargs)
        # POSIX 下 start_new_session 保证 pgid == 子进程 pid；Windows 用 pid 定位树根
        return proc, proc.pid

    @staticmethod
    def _posix_group_alive(pgid: int, proc: subprocess.Popen) -> bool:
        """POSIX：进程组里是否还有任意进程（含孙进程；根退出但孙进程存活仍为 True）。"""
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def _posix_signal_group(proc: subprocess.Popen, pgid: int, *, force: bool) -> None:
        """向整个 POSIX 进程组发 SIGTERM/SIGKILL；进程组消失时退回单进程信号。"""
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError):
            pass  # 进程组已消失或权限受限，退回单进程信号
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass

    def _posix_terminate_tree(
        self, proc: subprocess.Popen, pgid: int, grace: float, wait_after: float
    ) -> bool:
        if proc.poll() is not None and not self._posix_group_alive(pgid, proc):
            return True
        self._posix_signal_group(proc, pgid, force=False)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if proc.poll() is not None and not self._posix_group_alive(pgid, proc):
                return True
            time.sleep(self.poll_interval_s)

        self._posix_signal_group(proc, pgid, force=True)
        try:
            proc.wait(timeout=wait_after)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + wait_after
        while time.monotonic() < deadline:
            if proc.poll() is not None and not self._posix_group_alive(pgid, proc):
                return True
            time.sleep(self.poll_interval_s)
        return proc.poll() is not None and not self._posix_group_alive(pgid, proc)

    def _tree_has_leftovers(
        self, proc: subprocess.Popen, pgid: int, tracked: set[int]
    ) -> bool:
        """根已退出后是否仍有残留进程（POSIX 看进程组；Windows 看子孙快照/PPID）。"""
        if os.name != "posix":
            return _wintree.has_leftovers(proc.pid, tracked)
        return self._posix_group_alive(pgid, proc)

    def terminate_tree(
        self,
        proc: subprocess.Popen,
        pgid: int,
        *,
        grace_s: Optional[float] = None,
        wait_s: Optional[float] = None,
        known_descendants: Optional[set[int]] = None,
    ) -> bool:
        """终止整棵进程树并确认退出。返回 tree_exited。

        POSIX：SIGTERM 进程组 → 宽限 → SIGKILL 进程组 → wait。
        Windows：快照 → 宽限（不杀根）→ 根存活时 /F /T 整树 → wait →
        逐个 tasklist 确认快照子孙消失（细节见 windows_tree.py）。
        """
        grace = self.term_grace_s if grace_s is None else grace_s
        wait_after = self.kill_wait_s if wait_s is None else wait_s
        if os.name != "posix":
            return _wintree.terminate(
                proc,
                pgid,
                grace_s=grace,
                wait_s=wait_after,
                poll_interval_s=self.poll_interval_s,
                tracked=known_descendants or (),
                log=self.log,
            )
        return self._posix_terminate_tree(proc, pgid, grace, wait_after)

    # -- 执行 ---------------------------------------------------------------------
    def run(
        self,
        command: Command,
        worktree: Path,
        scope: Sequence[str] = (),
        timeout_s: Optional[float] = None,
        log_dir: Optional[Path] = None,
        *,
        cancel_event: Optional[threading.Event] = None,
        on_tick: Optional[Callable[[], None]] = None,
    ) -> ExecutionResult:
        timeout = timeout_s if timeout_s is not None else self.timeout_s
        started = time.monotonic()
        exit_code: Optional[int] = None
        killed: Optional[str] = None
        tree_exited = True
        pid: Optional[int] = None
        output = ""

        if callable(command):
            # 进程内 callable 无法被强杀（测试注入用），取消只能执行前短路
            if cancel_event is not None and cancel_event.is_set():
                killed = "cancelled"
            else:
                exit_code = int(command(worktree))
        else:
            task_env = _env_filter.build_task_env(
                extra_allowed=self.env_extra_allowed
            )
            if self.log is not None:
                extra_note = (
                    f"，额外放行 {sorted(self.env_extra_allowed)}"
                    if self.env_extra_allowed
                    else ""
                )
                self.log(
                    f"任务环境白名单：放行 {len(task_env)} 个变量{extra_note}（#78）"
                )
            proc, pgid = self._spawn(list(command), worktree, env=task_env)
            pid = proc.pid
            tracked: set[int] = set()  # Windows：执行期快照到的子孙 PID
            chunks: list[str] = []
            reader = threading.Thread(
                target=_pump, args=(proc.stdout, chunks), name="exec-output", daemon=True
            )
            reader.start()
            try:
                next_snapshot = 0.0
                while True:
                    rc = proc.poll()
                    if rc is not None:
                        exit_code = rc
                        break
                    if cancel_event is not None and cancel_event.is_set():
                        killed = "cancelled"
                        break
                    if timeout is not None and time.monotonic() - started > timeout:
                        killed = "timeout"
                        break
                    if os.name != "posix" and time.monotonic() >= next_snapshot:
                        # 执行期周期性快照：避免父进程中途退出后孙进程彻底失联
                        tracked |= _wintree.descendants(proc.pid)
                        next_snapshot = time.monotonic() + self.windows_snapshot_interval_s
                    if on_tick is not None:
                        try:
                            on_tick()
                        except Exception:  # noqa: BLE001 - 续租失败不打断执行
                            pass
                    time.sleep(self.poll_interval_s)
                if killed is not None:
                    tree_exited = self.terminate_tree(
                        proc, pgid, known_descendants=tracked
                    )
                    exit_code = proc.poll()
                elif os.name != "posix":
                    # 根正常退出时也补一次快照：直系子进程会保留旧 PPID，仍能发现
                    tracked |= _wintree.descendants(proc.pid)
            finally:
                if killed is None:
                    if proc.poll() is None:
                        # 理论不可达（循环只在退出/取消/超时处 break）：保底杀干净
                        tree_exited = self.terminate_tree(
                            proc, pgid, known_descendants=tracked
                        )
                    elif self._tree_has_leftovers(proc, pgid, tracked):
                        # 根进程正常退出但仍有残留子孙（后台守护式）：必须清理，
                        # 否则进程树泄漏，Worker 可能误以为任务已结束。
                        tree_exited = self.terminate_tree(
                            proc, pgid, known_descendants=tracked
                        )
                reader.join(timeout=5.0)
                output = "".join(chunks)

        duration = time.monotonic() - started

        log_path: Optional[Path] = None
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "execution.log"
            log_path.write_text(output, encoding="utf-8", errors="replace")

        changed = self.changed_files(worktree)
        if log_dir is not None:
            # 执行器自己的日志不算任务改动（真实仓库里 TMP/ 本就被 gitignore，
            # 这里再显式排除，保证未配 .gitignore 的仓库行为一致）
            try:
                log_prefix = _norm(str(log_dir.relative_to(worktree)))
                changed = [
                    f
                    for f in changed
                    if not (_norm(f) == log_prefix or _norm(f).startswith(log_prefix + "/"))
                ]
            except ValueError:
                pass
        out_of_scope = [f for f in changed if scope and not _in_scope(f, scope)]

        if killed == "timeout":
            summary = f"执行超时（>{timeout}s），已终止进程树（{duration:.1f}s）"
        elif killed == "cancelled":
            summary = f"执行被取消，已终止进程树（{duration:.1f}s）"
        elif exit_code is None:
            summary = f"执行未返回退出码（{duration:.1f}s）"
        elif exit_code == 0:
            summary = f"执行成功（{duration:.1f}s，改动 {len(changed)} 个文件）"
        else:
            summary = f"执行失败 exit={exit_code}（{duration:.1f}s）"
        if out_of_scope:
            summary += f"；超范围改动 {len(out_of_scope)} 个：{', '.join(out_of_scope)}"
        if not tree_exited:
            summary += "；进程树未确认退出（禁止删除 worktree）"
        return ExecutionResult(
            exit_code=exit_code,
            duration_s=duration,
            changed_files=changed,
            out_of_scope=out_of_scope,
            log_path=log_path,
            summary=summary,
            killed_reason=killed,
            tree_exited=tree_exited,
            pid=pid,
        )

    # -- 提交（只提交范围内文件） ---------------------------------------------------
    def commit_in_scope(
        self, worktree: Path, scope: Sequence[str], message: str
    ) -> Optional[str]:
        """把范围内改动提交，返回提交 SHA；无改动返回 None。超范围文件不进提交。"""
        changed = [f for f in self.changed_files(worktree) if _in_scope(f, scope)]
        if not changed:
            return None
        add = self._git(worktree, "add", "--", *changed)
        if add.returncode != 0:
            raise RuntimeError(f"git add 失败：{add.stderr.strip()}")
        commit = self._git(worktree, "commit", "-m", message)
        if commit.returncode != 0:
            raise RuntimeError(f"git commit 失败：{commit.stderr.strip()}")
        head = self._git(worktree, "rev-parse", "HEAD")
        return head.stdout.strip() or None
