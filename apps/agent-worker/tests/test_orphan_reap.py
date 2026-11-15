"""#77 孤儿回收：Worker 根进程异常退出后，上一代任务进程树的回收。

链路三段，各测一段：
1. heartbeat 载荷能持久化 task_pid/task_pgid（写入、清除、读回）；
2. executor 的 on_spawn 回调把子进程 pid/pgid 交出来；
3. watchdog 的 _reap_previous_generation 判定与真实回收：
   上一代还活着不碰、进程组不符不碰（防 pid 复用）、工作目录不在
   worktree 根之下不碰；证据齐全时真实杀掉独立会话的睡眠进程。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import ex, hb, wd

CMD = ["python", "worker.py"]


def _clock():
    return datetime(2026, 11, 15, 18, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. heartbeat 载荷：task_pid/task_pgid 的写入、清除与读回
# ---------------------------------------------------------------------------


def test_heartbeat_payload_carries_task_process(tmp_path):
    writer = hb.HeartbeatWriter(
        tmp_path / "hb.json", agent_id="muse-3", instance_id="i-1", clock=_clock
    )
    payload = writer.write()
    assert payload["task_pid"] is None and payload["task_pgid"] is None

    writer.update(task_pid=4242, task_pgid=4242)
    payload = writer.write()
    assert payload["task_pid"] == 4242 and payload["task_pgid"] == 4242

    # 传 0 清除（任务树确认退出后 Worker 这么做）
    writer.update(task_pid=0, task_pgid=0)
    payload = writer.write()
    assert payload["task_pid"] is None and payload["task_pgid"] is None

    # 不传参不改动
    writer.update(task_pid=777)
    writer.update(state="running")
    assert writer.payload()["task_pid"] == 777


def test_read_heartbeat_payload(tmp_path):
    path = tmp_path / "hb.json"
    assert hb.read_heartbeat_payload(path) is None  # 文件不存在
    path.write_text("{不是 JSON", encoding="utf-8")
    assert hb.read_heartbeat_payload(path) is None  # 半截/损坏不猜
    path.write_text(json.dumps({"pid": 10, "task_pid": 20}), encoding="utf-8")
    data = hb.read_heartbeat_payload(path)
    assert data is not None and data["task_pid"] == 20


# ---------------------------------------------------------------------------
# 2. executor on_spawn 回调
# ---------------------------------------------------------------------------


def test_executor_on_spawn_reports_pid_and_pgid(tmp_path):
    seen: list[tuple[int, int]] = []
    executor = ex.Executor(term_grace_s=0.2, kill_wait_s=3.0, poll_interval_s=0.02)
    result = executor.run(
        [sys.executable, "-c", "pass"],
        tmp_path,
        on_spawn=lambda pid, pgid: seen.append((pid, pgid)),
    )
    assert result.ok
    assert len(seen) == 1
    pid, pgid = seen[0]
    assert pid > 0
    if os.name == "posix":
        assert pgid == pid  # start_new_session：子进程自成一组，pgid==pid


# ---------------------------------------------------------------------------
# 3. watchdog 回收判定（假进程证据，只验决策不真杀）
# ---------------------------------------------------------------------------


def _watchdog(tmp_path, **cfg_overrides):
    cfg = dict(
        stale_after_s=180.0, check_interval_s=1.0, heartbeat_interval_s=30.0,
        max_restarts=3, start_grace_s=0.0, term_grace_s=0.05, kill_wait_s=0.5,
    )
    cfg.update(cfg_overrides)
    heartbeat = tmp_path / ".heartbeat-muse-3.json"
    dog = wd.Watchdog(CMD, heartbeat, wd.WatchdogConfig(**cfg), log=lambda _m: None)
    return dog, heartbeat


def _write_payload(path: Path, **fields) -> None:
    base = {
        "agent": "muse-3", "instance": "i-1", "pid": 111,
        "at": "2026-11-15T18:00:00+00:00", "task": "t-1", "state": "running",
        "task_pid": 222, "task_pgid": 222,
    }
    base.update(fields)
    path.write_text(json.dumps(base), encoding="utf-8")


@pytest.fixture()
def kills(monkeypatch):
    """拦截真实杀进程：记录 _kill_orphan_tree 的调用。"""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        wd.Watchdog, "_kill_orphan_tree",
        lambda self, pid, pgid: calls.append((pid, pgid)) or True,
    )
    return calls


def _evidence(monkeypatch, *, alive: set[int], pgid_of=None, cwd_of=None):
    monkeypatch.setattr(wd, "_pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(wd, "_proc_pgid", pgid_of or (lambda pid: 222))
    monkeypatch.setattr(wd, "_proc_cwd", cwd_of or (lambda pid: None))


def test_reap_when_previous_dead_and_task_alive(tmp_path, monkeypatch, kills):
    dog, heartbeat = _watchdog(tmp_path)
    _write_payload(heartbeat)  # prev pid=111（死），task 222（活）
    worktree = tmp_path / "task-1"
    worktree.mkdir()
    _evidence(monkeypatch, alive={222}, cwd_of=lambda pid: worktree)
    dog._reap_previous_generation()
    assert kills == [(222, 222)]


def test_no_reap_when_previous_worker_alive(tmp_path, monkeypatch, kills):
    dog, heartbeat = _watchdog(tmp_path)
    _write_payload(heartbeat)
    _evidence(monkeypatch, alive={111, 222})
    dog._reap_previous_generation()
    assert kills == []  # 上一代在跑，它的孩子不是孤儿


def test_no_reap_when_task_already_dead(tmp_path, monkeypatch, kills):
    dog, heartbeat = _watchdog(tmp_path)
    _write_payload(heartbeat)
    _evidence(monkeypatch, alive=set())
    dog._reap_previous_generation()
    assert kills == []


@pytest.mark.skipif(os.name != "posix", reason="进程组校验为 POSIX 口径，Windows 不走该分支")
def test_no_reap_on_pgid_mismatch(tmp_path, monkeypatch, kills):
    dog, heartbeat = _watchdog(tmp_path)
    _write_payload(heartbeat)
    worktree = tmp_path / "task-1"
    worktree.mkdir()
    _evidence(
        monkeypatch, alive={222},
        pgid_of=lambda pid: 999,  # 实际进程组与记录不符 → pid 已被复用
        cwd_of=lambda pid: worktree,
    )
    dog._reap_previous_generation()
    assert kills == []


@pytest.mark.skipif(os.name != "posix", reason="工作目录校验为 POSIX 口径，Windows 不走该分支")
def test_no_reap_when_cwd_outside_worktrees(tmp_path, monkeypatch, kills):
    dog, heartbeat = _watchdog(tmp_path)
    _write_payload(heartbeat)
    _evidence(
        monkeypatch, alive={222},
        cwd_of=lambda pid: Path("/somewhere/else"),
    )
    dog._reap_previous_generation()
    assert kills == []


def test_no_reap_without_task_record(tmp_path, monkeypatch, kills):
    dog, heartbeat = _watchdog(tmp_path)
    _write_payload(heartbeat, task_pid=None, task_pgid=None)
    _evidence(monkeypatch, alive={222})
    dog._reap_previous_generation()
    assert kills == []


# ---------------------------------------------------------------------------
# 4. POSIX 真实回收：独立会话的睡眠进程被整组杀掉
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


def _wait_dead(proc: subprocess.Popen, timeout: float = 5.0) -> bool:
    """等 Popen 子进程退出（必须用 poll 回收僵尸，kill(pid,0) 对僵尸仍成功）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.02)
    return proc.poll() is not None


@pytest.mark.skipif(os.name != "posix", reason="进程组语义为 POSIX")
def test_reap_kills_real_orphan_process_group(tmp_path):
    root = tmp_path / ".agent-worktrees"
    worktree = root / "task-77"
    worktree.mkdir(parents=True)
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=worktree, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        heartbeat = root / ".heartbeat-muse-3.json"
        _write_payload(
            heartbeat, pid=_dead_pid(),
            task_pid=sleeper.pid, task_pgid=sleeper.pid,
        )
        dog, _ = _watchdog(root, term_grace_s=0.2, kill_wait_s=3.0)
        dog.heartbeat_file = heartbeat
        dog._reap_previous_generation()
        assert _wait_dead(sleeper), "孤儿睡眠进程应被回收"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="进程组语义为 POSIX")
def test_reap_spares_process_outside_worktree(tmp_path):
    root = tmp_path / ".agent-worktrees"
    root.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=elsewhere, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        heartbeat = root / ".heartbeat-muse-3.json"
        _write_payload(
            heartbeat, pid=_dead_pid(),
            task_pid=sleeper.pid, task_pgid=sleeper.pid,
        )
        dog, _ = _watchdog(root)
        dog.heartbeat_file = heartbeat
        dog._reap_previous_generation()
        assert sleeper.poll() is None, "worktree 外的进程不得误杀"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
