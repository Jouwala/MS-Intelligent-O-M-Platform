"""执行器进程树验收（Linux 7.1/7.2 + TASK-10）。

覆盖：超时/取消终止**整个进程组**并 wait() 回收、忽略 SIGTERM 时升级 SIGKILL、
孙进程不泄漏、执行期续租回调。POSIX 专用用例在 Windows 上跳过（CI ubuntu 真跑）。
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

import pytest

from conftest import ex, wintree

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="POSIX 进程组与 SIGTERM/SIGKILL 语义；Windows 用 taskkill /T"
)
WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="Windows taskkill /T 进程树语义专用（CI ubuntu 上跳过）"
)

#: 模拟「不响应 Ctrl+Break 的控制台进程」：宽限信号杀不掉，只能靠 taskkill /F /T
_IGNORE_BREAK = (
    "import signal as _signal\n"
    "try:\n"
    "    _signal.signal(_signal.SIGBREAK, _signal.SIG_IGN)\n"
    "except (AttributeError, ValueError):\n"
    "    pass\n"
)

#: 子进程：写自己的 pid → 再起一个孙进程并写孙 pid → 一起 sleep 60
_CHILD_SCRIPT = (
    _IGNORE_BREAK
    + "import subprocess, sys, time\n"
    "open(sys.argv[1], 'w').write(str(__import__('os').getpid()))\n"
    "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[2], 'w').write(str(grandchild.pid))\n"
    "time.sleep(60)\n"
)

#: 根进程：起子进程后自己也 sleep 60（超时/取消时根必须仍存活，才能 /F /T 枚举整棵树）
_ROOT_SLEEP_SCRIPT = (
    _IGNORE_BREAK
    + "import subprocess, sys, time\n"
    f"subprocess.Popen([sys.executable, '-c', {_CHILD_SCRIPT!r}, sys.argv[1], sys.argv[2]])\n"
    "time.sleep(60)\n"
)

#: 根进程：起子进程后 0.8s 正常退出（留子/孙进程残留，验证正常退出路径也会清理）
_ROOT_EXIT_SCRIPT = (
    _IGNORE_BREAK
    + "import subprocess, sys, time\n"
    f"subprocess.Popen([sys.executable, '-c', {_CHILD_SCRIPT!r}, sys.argv[1], sys.argv[2]])\n"
    "time.sleep(0.8)\n"
)


def _wait_file(path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return True
        time.sleep(0.02)
    return path.exists() and bool(path.read_text(encoding="utf-8").strip())


def _windows_pid_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if wintree.pid_alive(pid) is False:
            return True
        time.sleep(0.05)
    return wintree.pid_alive(pid) is False


def _executor(**overrides) -> ex.Executor:
    kwargs = dict(term_grace_s=0.2, kill_wait_s=3.0, poll_interval_s=0.02)
    kwargs.update(overrides)
    return ex.Executor(**kwargs)


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True  # Windows 上已退出进程的 os.kill 会抛 OSError
        time.sleep(0.02)
    return False


def test_timeout_terminates_process_tree_and_sets_reason(tmp_path):
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    result = _executor().run(cmd, tmp_path, timeout_s=0.3)
    assert result.killed_reason == "timeout"
    assert result.tree_exited is True
    assert result.exit_code is not None
    assert result.ok is False
    assert "执行超时" in result.summary


def test_cancel_event_terminates_process_group(tmp_path):
    event = threading.Event()
    timer = threading.Timer(0.3, event.set)
    timer.start()
    try:
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        result = _executor().run(cmd, tmp_path, timeout_s=60, cancel_event=event)
    finally:
        timer.cancel()
    assert result.killed_reason == "cancelled"
    assert result.tree_exited is True
    assert result.ok is False
    assert "取消" in result.summary


def test_callable_cancelled_before_start(tmp_path):
    event = threading.Event()
    event.set()
    result = _executor().run(lambda _path: 0, tmp_path, cancel_event=event)
    assert result.killed_reason == "cancelled"
    assert result.exit_code is None and result.ok is False


def test_callable_runs_and_records_changes(tmp_path):
    def job(path):
        (path / "made.txt").write_text("x", encoding="utf-8")
        return 0

    result = _executor().run(job, tmp_path)
    assert result.exit_code == 0
    assert result.killed_reason is None
    assert result.tree_exited is True


def test_on_tick_called_while_running(tmp_path):
    ticks = {"n": 0}

    def on_tick():
        ticks["n"] += 1

    cmd = [sys.executable, "-c", "import time; time.sleep(0.5)"]
    result = _executor().run(cmd, tmp_path, timeout_s=10, on_tick=on_tick)
    assert result.exit_code == 0
    assert ticks["n"] >= 1  # 续租回调在轮询循环里被调用


@POSIX_ONLY
def test_ignored_sigterm_escalates_to_sigkill(tmp_path):
    """旧实现只 kill 直接子进程；子进程忽略 SIGTERM 时必须升级 SIGKILL 并确认退出。"""
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = _executor().run([sys.executable, "-c", script], tmp_path, timeout_s=0.3)
    elapsed = time.monotonic() - started
    assert result.killed_reason == "timeout"
    assert result.tree_exited is True
    assert result.exit_code == -signal.SIGKILL
    assert elapsed >= 0.3 + 0.2  # 确实等过 SIGTERM 宽限才升级


@POSIX_ONLY
def test_grandchild_process_is_reaped(tmp_path):
    """shell/脚本里起的孙进程必须随进程组一起清理，不能泄漏。"""
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    result = _executor().run(
        [sys.executable, "-c", script, str(pid_file)], tmp_path, timeout_s=0.8
    )
    assert result.killed_reason == "timeout"
    assert pid_file.exists(), "孙进程应在超时前写出自己的 pid"
    grandchild_pid = int(pid_file.read_text().strip())
    assert _wait_dead(grandchild_pid) is True, "孙进程未被回收，说明只杀了直接子进程"


@POSIX_ONLY
def test_root_process_exit_but_group_alive_is_killed(tmp_path):
    """根进程先退出、孙进程仍活（同进程组）：确认 tree_exited 依据进程组而非根进程。"""
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "sys.exit(0)\n"
    )
    result = _executor().run(
        [sys.executable, "-c", script, str(pid_file)], tmp_path, timeout_s=10
    )
    assert result.exit_code == 0
    assert result.tree_exited is True
    grandchild_pid = int(pid_file.read_text().strip())
    assert _wait_dead(grandchild_pid) is True


# ---------------------------------------------------------------------------
# Windows 真跑：孙进程必须被 /F /T 清理，且 tree_exited 不得假阳性
#（Muse 3 在 commit 0a91985 上实测的阻断缺陷；这些用例在 Windows 上真实执行）
# ---------------------------------------------------------------------------


def _assert_windows_tree_gone(child_pid_file, grandchild_pid_file):
    assert _wait_file(child_pid_file), "子进程应在终止前写出自己的 pid"
    assert _wait_file(grandchild_pid_file), "孙进程应在终止前写出自己的 pid"
    child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
    grandchild_pid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())
    assert _windows_pid_gone(child_pid), f"子进程 {child_pid} 仍存活（进程树泄漏）"
    assert _windows_pid_gone(grandchild_pid), f"孙进程 {grandchild_pid} 仍存活（进程树泄漏）"


@WINDOWS_ONLY
def test_windows_timeout_kills_grandchild_and_tree_exited_true(tmp_path):
    """超时路径：根存活时快照子树 → /F /T 清理 → 孙进程消失且 tree_exited=True。"""
    child_file = tmp_path / "child.pid"
    grandchild_file = tmp_path / "grandchild.pid"
    executor = _executor(kill_wait_s=8.0)
    result = executor.run(
        [sys.executable, "-c", _ROOT_SLEEP_SCRIPT, str(child_file), str(grandchild_file)],
        tmp_path,
        timeout_s=1.2,
    )
    assert result.killed_reason == "timeout"
    assert result.tree_exited is True, "孙进程未被清理却报 tree_exited=True 就是假阳性"
    assert "进程树未确认退出" not in result.summary
    _assert_windows_tree_gone(child_file, grandchild_file)


@WINDOWS_ONLY
def test_windows_cancel_kills_grandchild_and_tree_exited_true(tmp_path):
    """取消路径：与超时同一条 /F /T 整树清理链路，不得有孙进程残留。"""
    child_file = tmp_path / "child.pid"
    grandchild_file = tmp_path / "grandchild.pid"
    event = threading.Event()
    timer = threading.Timer(1.2, event.set)
    timer.start()
    try:
        executor = _executor(kill_wait_s=8.0)
        result = executor.run(
            [sys.executable, "-c", _ROOT_SLEEP_SCRIPT, str(child_file), str(grandchild_file)],
            tmp_path,
            timeout_s=60,
            cancel_event=event,
        )
    finally:
        timer.cancel()
    assert result.killed_reason == "cancelled"
    assert result.tree_exited is True
    _assert_windows_tree_gone(child_file, grandchild_file)


@WINDOWS_ONLY
def test_windows_root_exit_leaving_orphans_is_cleaned(tmp_path):
    """根正常退出、子/孙残留：终结阶段按旧 PPID 快照发现并清理，tree_exited=True。"""
    child_file = tmp_path / "child.pid"
    grandchild_file = tmp_path / "grandchild.pid"
    executor = _executor(kill_wait_s=8.0)
    result = executor.run(
        [sys.executable, "-c", _ROOT_EXIT_SCRIPT, str(child_file), str(grandchild_file)],
        tmp_path,
        timeout_s=30,
    )
    assert result.exit_code == 0  # 根进程正常退出，任务本身成功
    assert result.tree_exited is True, "根退出但子/孙残留时必须清理，不得误报 tree_exited"
    _assert_windows_tree_gone(child_file, grandchild_file)

