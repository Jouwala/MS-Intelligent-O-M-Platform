"""Windows 进程树清理与「确认退出」（TASK-09 / TASK-10 / TASK-20 的 Windows 覆盖）。

旧实现（commit 0a91985）在 Windows 上有三个叠加缺陷，Muse 3 独立验收实测为阻断级：

1. 宽容阶段用 `taskkill /T /PID root`（不带 /F），对控制台进程返回 rc=128
   （"can only be terminated forcefully"），随后 `proc.terminate()` 只杀掉根进程；
2. 根一死，旧 `_group_alive`（Windows 只检查 `proc.poll()`）立刻判「树已退出」，
   `terminate_tree` 宽限循环提前 return True，强制阶段的 `taskkill /F /T` 永远执行不到；
3. 于是孙进程继续存活，而 `ExecutionResult.tree_exited=True`（**假阳性**），
   Worker 据此会误删仍被占用的 worktree。

本模块把 Windows 侧改成「快照 → 根存活时强制 /F /T → 逐个确认」：

- `descendants(root_pid)`：优先用原生 Toolhelp32 快照（`Process32FirstW`，
  毫秒级、不依赖子进程）；不可用时按任务要求退回
  `wmic process get ProcessId,ParentProcessId /format:csv`（Win11 可能已移除），
  再退回 PowerShell `Get-CimInstance Win32_Process`。根已退出时直系子进程仍保留
  旧 PPID，因此照样能被发现；
- `terminate(proc, ...)`：**终止前先快照整棵树**；宽限阶段只发
  `CTRL_BREAK_EVENT` / `taskkill /T`（允许失败，**绝不**先 `proc.terminate()`
  把根杀掉）；根仍存活时执行 `taskkill /F /T /PID root`（唯一能带走整棵树的时机）；
  根已退出则逐个 `/F /T` 补杀残留；`wait()` 回收后用 `tasklist /FI "PID eq X"`
  逐个确认快照子孙消失（优先用原生 OpenProcess 查询，tasklist 兜底）；
- `tree_exited = 根退出 && 快照子孙全部确认消失`；查询失败（拿不到进程表/
  查不到进程状态）按「可能存活」处理，宁可误报未退出也不给假阳性。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from typing import Iterable, Optional

#: Windows 上不弹出控制台窗口（taskkill/tasklist/wmic/powershell 都是短命子进程）
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_IS_WINDOWS = os.name == "nt"
_kernel32_cache: tuple = ()
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _kernel32():
    """缓存 ctypes 句柄；非 Windows / ctypes 不可用时返回 ()。"""
    global _kernel32_cache
    if _kernel32_cache:
        return _kernel32_cache
    if not _IS_WINDOWS:
        return ()
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32_cache = (ctypes, wintypes, kernel32)
    except (ImportError, OSError, AttributeError):
        _kernel32_cache = ()
    return _kernel32_cache


def _run(args: list[str], timeout: float = 20.0) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _toolhelp_table() -> Optional[dict[int, int]]:
    """原生 Toolhelp32 进程快照：返回 {pid: ppid}，毫秒级且无需子进程。"""
    handles = _kernel32()
    if not handles:
        return None
    ctypes, wintypes, kernel32 = handles

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    try:
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    except (AttributeError, OSError):
        return None
    if snapshot == INVALID_HANDLE_VALUE or not snapshot:
        return None
    table: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table or None


def process_table() -> Optional[dict[int, int]]:
    """一次性拿 {pid: ppid} 全表；拿不到返回 None（调用方按「未知」保守处理）。

    顺序：原生 Toolhelp32（快）→ wmic（任务要求首选兜底）→ PowerShell CIM。
    """
    table = _toolhelp_table()
    if table:
        return table
    proc = _run(["wmic", "process", "get", "ProcessId,ParentProcessId", "/format:csv"])
    if proc is not None and proc.returncode == 0 and proc.stdout:
        table = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("Node,"):
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                ppid, pid = int(parts[1]), int(parts[2])
            except ValueError:
                continue
            table[pid] = ppid
        if table:
            return table
    # wmic 被移除/不可用时的兜底：PowerShell CIM
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress"
    )
    proc = _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=30
    )
    if proc is None or proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    try:
        data = json.loads(proc.stdout.strip())
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]
    table = {}
    for row in data:
        try:
            table[int(row["ProcessId"])] = int(row["ParentProcessId"])
        except (KeyError, TypeError, ValueError):
            continue
    return table or None


def descendants(root_pid: int, table: Optional[dict[int, int]] = None) -> set[int]:
    """root_pid 的全部子孙 PID 快照（找不到进程表时返回空集合）。"""
    table = process_table() if table is None else table
    if not table:
        return set()
    children: dict[int, list[int]] = {}
    for pid, ppid in table.items():
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = [root_pid]
    seen = {root_pid}
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            if child in seen:
                continue
            seen.add(child)
            found.add(child)
            stack.append(child)
    return found


def _pid_alive_native(pid: int) -> Optional[bool]:
    """原生 OpenProcess + GetExitCodeProcess：毫秒级判断 pid 是否存活。"""
    handles = _kernel32()
    if not handles:
        return None
    ctypes, wintypes, kernel32 = handles
    try:
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except (AttributeError, OSError):
        return None
    if not handle:
        err = ctypes.get_last_error()
        if err == 87:  # ERROR_INVALID_PARAMETER：进程不存在
            return False
        if err == 5:  # ERROR_ACCESS_DENIED：进程存在（系统进程）
            return True
        return None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> Optional[bool]:
    """单个 PID 是否存活；无法确认返回 None（调用方按存活处理）。

    优先原生 OpenProcess（快、无子进程），失败再按任务要求用
    `tasklist /FI "PID eq <pid>"` 逐个确认。
    """
    native = _pid_alive_native(pid)
    if native is not None:
        return native
    proc = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
    if proc is None or proc.returncode != 0:
        return None
    out = proc.stdout or ""
    if "No tasks" in out or not out.strip():
        return False
    for line in out.splitlines():
        cols = [cell.strip().strip('"') for cell in line.split('","')]
        if len(cols) >= 2 and cols[1] == str(pid):
            return True
    return False


def alive_pids(pids: Iterable[int]) -> set[int]:
    """返回「确认存活或无法确认」的 PID 集合（查询失败按存活处理，避免假阳性）。"""
    alive: set[int] = set()
    for pid in pids:
        if pid_alive(pid) is not False:
            alive.add(pid)
    return alive


def kill_tree(pid: int) -> Optional[subprocess.CompletedProcess]:
    """强制终止整棵树：`taskkill /F /T /PID`（必须在进程仍存活时调用才枚举得到树）。"""
    return _run(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=30)


def kill_pids(pids: Iterable[int]) -> None:
    for pid in sorted(set(pids)):
        kill_tree(pid)


def has_leftovers(root_pid: int, tracked: Iterable[int] = ()) -> bool:
    """根已退出后是否仍有残留进程（快照 ∪ 当前 PPID 仍指向 root 的直系子孙）。"""
    candidates = set(tracked) | descendants(root_pid)
    return bool(alive_pids(candidates))


def terminate(
    proc: subprocess.Popen,
    root_pid: int,
    *,
    grace_s: float,
    wait_s: float,
    poll_interval_s: float,
    tracked: Iterable[int] = (),
    log=None,
) -> bool:
    """Windows 进程树终止：快照 → 宽限（不杀根）→ /F /T 整树 → wait → 逐个确认。

    返回 tree_exited：True 表示根退出且**所有快照子孙都确认消失**。
    """
    snapshot = set(tracked)
    log = log or (lambda _msg: None)

    # 1) 终止前快照：根存活时 BFS 拿整棵树（这是后续唯一可信的「应消失」清单）
    if proc.poll() is None:
        snapshot |= descendants(root_pid)
    log(f"[executor] Windows 终止进程树：root={root_pid} 快照={sorted(snapshot)}")

    # 2) 宽限阶段：只请求优雅退出，绝不在根存活时 proc.terminate()（否则 /T 枚举不到树）
    if proc.poll() is None:
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError, AttributeError):
            pass
        _run(["taskkill", "/T", "/PID", str(root_pid)], timeout=15)  # 允许失败
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if proc.poll() is not None and not alive_pids(snapshot):
                return True
            time.sleep(poll_interval_s)

    # 3) 强制阶段：根仍存活 → /F /T 一次带走整棵树（关键时机，不能等根先死）
    if proc.poll() is None:
        snapshot |= descendants(root_pid)  # 强制前最后补一次快照（根还活着，树最全）
        kill_tree(root_pid)
        if proc.poll() is None:
            try:
                proc.kill()  # 兜底：/F /T 已经先试过
            except OSError:
                pass
    else:
        kill_pids(snapshot)  # 根已退出：逐个补杀残留（每个都 /F /T 带走其子树）

    try:
        proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        pass

    # 4) 确认阶段：根退出 + 快照子孙消失；期间新发现的残留（PPID 仍指 root）也补杀
    deadline = time.monotonic() + wait_s
    next_scan = 0.0
    while True:
        now = time.monotonic()
        if now >= next_scan:
            snapshot |= descendants(root_pid)
            next_scan = now + max(0.2, wait_s / 10)
        alive = alive_pids(snapshot)
        if proc.poll() is not None and not alive:
            return True
        if now >= deadline:
            break
        if alive:
            kill_pids(alive)
        time.sleep(poll_interval_s)

    snapshot |= descendants(root_pid)
    tree_exited = proc.poll() is not None and not alive_pids(snapshot)
    if not tree_exited:
        log(f"[executor] 严重：Windows 进程树未确认退出：残留={sorted(alive_pids(snapshot))}")
    return tree_exited
