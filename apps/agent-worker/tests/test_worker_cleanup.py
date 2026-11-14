"""Worker 统一清理矩阵 + 取消 + 心跳 + 注册（TASK-06 / TASK-10 / TASK-11）。

覆盖：成功全流程（幂等完成/镜像/工作区保留）、无执行命令、Hook 失败、
范围锁超时、执行失败、内部异常、断网、取消（进程被停且不 complete）；
并验证「清理异常不覆盖原始异常」「子进程未确认退出不删 worktree」。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import gi, hi, make_worker, tl, wk

BODY_OK = (
    "## 目标\n写结果。\n## 改动文件\n- `result.txt`\n## 执行命令\n```\necho hi\n```\n"
)
BODY_SCOPE_ONLY = "## 目标\n写结果。\n## 改动文件\n- `result.txt`\n"


def _issue(transport, number: int = 70, body: str = BODY_OK, agent: str = "muse-2"):
    return transport.create_issue(
        number, f"任务 {number}", body, labels=(f"agent:{agent}", "state:todo")
    )


def _runner_ok(path):
    (path / "result.txt").write_text("ok\n", encoding="utf-8")
    return 0


def _wait_until(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


def test_success_full_lifecycle(transport, clock, git_repo, scheduler, mirror):
    issue = _issue(transport)
    worker = make_worker(transport, git_repo, clock, scheduler, mirror, runner=_runner_ok)
    assert worker.handle_task(issue) == "completed"

    snap = scheduler.get_task("issue-70")
    assert snap.state == "completed"
    assert scheduler.leases.get("issue-70") is None
    assert scheduler.scope_locks.get("issue-70") is None
    assert worker.active is None
    assert worker.cleanup_errors == []
    assert len(scheduler.completions) == 1
    assert scheduler.completions[0]["status"] == "completed"
    assert mirror.applied == 1  # outbox 已通过 mirror 回写

    wt_path = worker.allocator.base / "task-70"
    assert wt_path.exists()  # 成功保留 worktree（要 review / 开 PR）
    persisted = json.loads(
        (wt_path / "TMP" / "execution-result.json").read_text(encoding="utf-8")
    )
    assert persisted["attempt"] == scheduler.completions[0]["key"]
    assert persisted["exit_code"] == 0
    assert (wt_path / "result.txt").exists()


def test_repeated_complete_same_attempt_is_idempotent(
    transport, clock, git_repo, scheduler, mirror
):
    issue = _issue(transport)
    worker = make_worker(transport, git_repo, clock, scheduler, mirror, runner=_runner_ok)
    assert worker.handle_task(issue) == "completed"

    S = wk.scheduler_module()
    key = scheduler.completions[0]["key"]
    task = scheduler.get_task("issue-70")
    active = tl.ActiveTask(
        issue=70,
        task_id="issue-70",
        slug="task-70",
        attempt_id=key,
        fencing_token=task.fencing_token,
    )
    # 同一幂等键重复完成：返回成功但不产生第二条结果/第二条 outbox
    assert worker.lifecycle.complete(active, "重复回传", S) is True
    assert len(scheduler.completions) == 1
    assert len(scheduler.outbox) == 1


def test_run_once_writes_heartbeat_and_registers_host_fields(
    transport, clock, git_repo, scheduler, mirror
):
    _issue(transport)
    worker = make_worker(
        transport, git_repo, clock, scheduler, mirror, runner=_runner_ok,
        heartbeat_interval_s=0.02,
    )
    info = worker.register()
    assert info["instance"] == worker.config.instance_id
    reg = scheduler.workers[("muse-2", worker.config.instance_id)]
    assert reg["hostname"] == hi.hostname()
    assert reg["platform"] == hi.platform_label()
    assert "python" in reg["capabilities"]

    assert worker.run_once() == "completed"
    payload = json.loads(worker.heartbeat_file.read_text(encoding="utf-8"))
    assert payload["agent"] == "muse-2"
    assert payload["instance"] == worker.config.instance_id
    assert payload["pid"] == os.getpid()
    assert payload["at"]
    assert "task" in payload and "state" in payload
    assert worker.heartbeat.running is False


def test_heartbeat_continues_while_task_is_running(
    transport, clock, git_repo, scheduler, mirror
):
    """执行期后台心跳仍在写（确定性同步，不依赖 wall-clock）。

    旧写法用 `threading.Timer(0.25)` 放行 runner，再断言 `writes >= 3`：
    `HeartbeatWriter` 把 interval 下限钳到 0.1s，0.25s 理论只够 3 拍、余量为 0；
    Linux CI 上心跳线程启动/调度抖动 50~100ms 就只剩 2 拍，于是
    `assert 2 >= 3` flake（run 43651353717）。

    现在：runner 用 Event 把执行**确定性地**阻塞住；测试轮询等待
    「执行开始后 HeartbeatWriter 至少再写 3 次」才放行 runner。
    断言阈值不变，也不再依赖任何固定 sleep 时长。
    """
    issue = _issue(transport)
    running = threading.Event()
    release = threading.Event()

    def slow_runner(path):
        running.set()
        assert release.wait(10.0), "测试未放行 runner（执行期心跳等待超时）"
        (path / "result.txt").write_text("ok\n", encoding="utf-8")
        return 0

    worker = make_worker(
        transport, git_repo, clock, scheduler, mirror, runner=slow_runner,
        heartbeat_interval_s=0.02,
    )
    outcome: dict = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault("value", worker.run_once()), daemon=True
    )
    thread.start()
    try:
        assert running.wait(10.0), "任务执行未开始"
        assert worker.heartbeat is not None
        baseline = worker.heartbeat.writes
        deadline = time.monotonic() + 10.0
        while worker.heartbeat.writes - baseline < 3:
            if time.monotonic() > deadline:
                raise AssertionError(
                    "执行期心跳停滞：runner 阻塞期间只写了 "
                    f"{worker.heartbeat.writes - baseline} 次（期望 ≥3）"
                )
            time.sleep(0.005)
    finally:
        release.set()
        thread.join(15.0)

    assert not thread.is_alive()
    assert outcome.get("value") == "completed"
    assert worker.heartbeat.running is False


# ---------------------------------------------------------------------------
# 清理矩阵：阻塞类
# ---------------------------------------------------------------------------


def test_blocked_no_command_releases_lease_and_scope(
    transport, clock, git_repo, scheduler, mirror
):
    issue = _issue(transport, body=BODY_SCOPE_ONLY)  # 有范围、无执行命令
    worker = make_worker(transport, git_repo, clock, scheduler, mirror)
    assert worker.handle_task(issue) == "blocked-no-command"
    assert worker.active is None
    assert worker.cleanup_errors == []
    assert scheduler.leases.get("issue-70") is None
    assert scheduler.scope_locks.get("issue-70") is None
    assert scheduler.tasks["issue-70"]["state"] in ("blocked", "retryable")
    assert "issue-70" in scheduler.scope_release_calls
    assert "issue-70" in scheduler.release_calls


def test_blocked_hooks_releases_everything(transport, clock, git_repo, scheduler, mirror):
    subprocess.run(
        ["git", "-C", str(git_repo), "config", "--unset", "core.hooksPath"],
        capture_output=True,
    )
    issue = _issue(transport)
    worker = make_worker(transport, git_repo, clock, scheduler, mirror, runner=_runner_ok)
    assert worker.handle_task(issue) == "blocked-hooks"
    assert worker.active is None
    assert scheduler.leases.get("issue-70") is None
    assert scheduler.scope_locks.get("issue-70") is None


def test_blocked_scope_timeout_releases_lease(
    transport, clock, git_repo, scheduler, mirror
):
    issue = _issue(transport)
    # 另一个在途任务占住重叠范围
    scheduler.upsert_task("issue-999", 999, scopes=["result.txt"])
    lease_other = scheduler.claim(
        "issue-999", agent_id="muse-3", instance_id="other", attempt_id="att-other"
    )
    scheduler.acquire_scope(
        "issue-999",
        attempt_id="att-other",
        fencing_token=lease_other.fencing_token,
        scopes=["result.txt"],
    )
    worker = make_worker(
        transport, git_repo, clock, scheduler, mirror, runner=_runner_ok,
        scope_wait_timeout_s=0,
    )
    assert worker.handle_task(issue) == "blocked-scope"
    assert worker.active is None
    assert scheduler.leases.get("issue-70") is None
    assert "issue-70" not in scheduler.scope_locks  # 自己从未拿到锁
    assert scheduler.tasks["issue-70"]["state"] in ("blocked", "retryable")
    assert scheduler.leases.get("issue-999") is not None  # 不得误放别人的锁


# ---------------------------------------------------------------------------
# 清理矩阵：执行失败 / 内部异常 / 断网
# ---------------------------------------------------------------------------


def test_execution_failure_keeps_diagnostics_but_releases_locks(
    transport, clock, git_repo, scheduler, mirror
):
    def runner_fail(path):
        (path / "result.txt").write_text("partial\n", encoding="utf-8")
        return 2

    issue = _issue(transport)
    worker = make_worker(transport, git_repo, clock, scheduler, mirror, runner=runner_fail)
    assert worker.handle_task(issue) == "failed"
    assert worker.active is None
    assert worker.cleanup_errors == []
    assert scheduler.leases.get("issue-70") is None
    assert scheduler.scope_locks.get("issue-70") is None
    assert scheduler.completions[0]["status"] == "failed"
    assert mirror.applied == 1
    wt_path = worker.allocator.base / "task-70"
    assert wt_path.exists()  # 失败保留诊断产物
    persisted = json.loads(
        (wt_path / "TMP" / "execution-result.json").read_text(encoding="utf-8")
    )
    assert persisted["exit_code"] == 2
    assert (wt_path / "TMP" / "execution.log").exists()


def test_internal_exception_cleans_then_reraises(transport, clock, git_repo, scheduler, mirror):
    def runner_boom(_path):
        raise ValueError("原始异常")

    issue = _issue(transport)
    worker = make_worker(transport, git_repo, clock, scheduler, mirror, runner=runner_boom)
    with pytest.raises(ValueError, match="原始异常"):
        worker.handle_task(issue)
    assert worker.active is None
    assert scheduler.leases.get("issue-70") is None
    assert scheduler.scope_locks.get("issue-70") is None
    assert any("未确认退出" in err for err in worker.cleanup_errors)


def test_cleanup_failure_does_not_override_original_exception(
    transport, clock, git_repo, scheduler, mirror, monkeypatch
):
    def broken(*_args, **_kwargs):
        raise RuntimeError("清理也失败")

    issue = _issue(transport)
    worker = make_worker(transport, git_repo, clock, scheduler, mirror, runner=_runner_ok)
    # 让执行开始前的状态迁移就失败（模拟调度器异常）；清理阶段的释放也失败。
    # 断言：向外抛出的仍是**原始**异常，清理异常只进 cleanup_errors。
    monkeypatch.setattr(scheduler, "transition", broken)
    monkeypatch.setattr(scheduler, "release", broken)
    with pytest.raises(RuntimeError, match="清理也失败"):
        worker.handle_task(issue)
    assert worker.active is None
    # 原始异常就是 transition 抛出的那个；cleanup_errors 里不得只剩它
    assert any("清理也失败" in err for err in worker.cleanup_errors)


def test_network_failure_while_polling_returns_offline(
    transport, clock, git_repo, scheduler, mirror, monkeypatch
):
    worker = make_worker(transport, git_repo, clock, scheduler, mirror, runner=_runner_ok)

    def boom():
        raise gi.TransportError("模拟断网")

    monkeypatch.setattr(worker.adapter, "list_claimable", boom)
    assert worker.run_once() == "offline"
    assert scheduler.leases == {} and scheduler.completions == []
    assert worker.heartbeat.running is False


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def test_cancel_terminates_process_and_never_completes(
    transport, clock, git_repo, scheduler, mirror
):
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    body = (
        "## 目标\n长时间任务。\n## 改动文件\n- `result.txt`\n"
        "## 执行命令\n```json\n" + json.dumps(command) + "\n```\n"
    )
    issue = _issue(transport, number=77, body=body)
    worker = make_worker(
        transport, git_repo, clock, scheduler, mirror, cancel_poll_interval_s=0.02
    )
    outcome: dict = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("value", worker.handle_task(issue)))
    thread.start()
    try:
        assert _wait_until(lambda: "issue-77" in scheduler.leases)
        scheduler.request_cancel("issue-77", reason="人工取消")
        thread.join(30)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():  # 兜底：测试失败时不要让子进程挂着
            scheduler.request_cancel("issue-77")
            thread.join(10)

    assert outcome.get("value") == "cancelled"
    assert scheduler.ack_calls == ["issue-77"]
    assert scheduler.completions == []  # 取消任务不得 complete
    assert scheduler.tasks["issue-77"]["state"] == "cancelled"
    assert scheduler.leases.get("issue-77") is None
    assert scheduler.scope_locks.get("issue-77") is None
    assert worker.active is None
    assert not (worker.allocator.base / "task-77").exists()  # 子进程确认退出后回收


def test_cancel_before_claim_never_executes(transport, clock, git_repo, scheduler, mirror):
    issue = _issue(transport)
    scheduler.upsert_task("issue-70", 70)
    scheduler.request_cancel("issue-70", reason="提前取消")
    worker = make_worker(transport, git_repo, clock, scheduler, mirror, runner=_runner_ok)
    assert worker.handle_task(issue) == "claim-conflict"
    assert scheduler.completions == []
    assert worker.active is None


def test_cancel_racing_completion_acks_cancel_without_completing(
    transport, clock, git_repo, scheduler, mirror, monkeypatch
):
    """取消恰好在执行结束与 complete 之间到达：撞上非法迁移也要转成 cancelled。"""
    issue = _issue(transport)

    def runner(path):
        (path / "result.txt").write_text("ok\n", encoding="utf-8")
        scheduler.request_cancel("issue-70", reason="完成瞬间取消")
        return 0

    worker = make_worker(transport, git_repo, clock, scheduler, mirror, runner=runner)
    real = scheduler.is_cancel_requested
    calls = {"n": 0}

    def lagging(task_id):
        # 第一次（执行后检查）漏掉 → 迫使 complete 撞上非法迁移
        calls["n"] += 1
        return False if calls["n"] == 1 else real(task_id)

    monkeypatch.setattr(scheduler, "is_cancel_requested", lagging)
    assert worker.handle_task(issue) == "cancelled"
    assert scheduler.completions == []  # 取消任务绝不产生完成结果
    assert scheduler.ack_calls == ["issue-70"]
    assert scheduler.tasks["issue-70"]["state"] == "cancelled"
    assert scheduler.leases.get("issue-70") is None


# ---------------------------------------------------------------------------
# 生命周期单元：清理顺序 + worktree 策略
# ---------------------------------------------------------------------------


class RecordingAllocator:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, slug: str, force: bool = False) -> bool:
        self.released.append(slug)
        return True


def _lifecycle(scheduler, allocator=None, log=None):
    return tl.TaskLifecycle(
        scheduler,
        allocator or RecordingAllocator(),
        wk.WorkerConfig(),
        log=log or (lambda _m: None),
    )


def test_cleanup_releases_scope_before_lease(scheduler, clock):
    scheduler.upsert_task("issue-9", 9)
    lease = scheduler.claim("issue-9", agent_id="a", instance_id="i", attempt_id="att")
    scheduler.acquire_scope(
        "issue-9", attempt_id="att", fencing_token=lease.fencing_token, scopes=["x.py"]
    )
    active = tl.ActiveTask(
        issue=9,
        task_id="issue-9",
        slug="task-9",
        attempt_id="att",
        fencing_token=lease.fencing_token,
        scope=("x.py",),
        scope_acquired=True,
        lease_held=True,
    )
    outcome = _lifecycle(scheduler).cleanup(active, "failed")
    assert outcome.ok
    names = [name for name, _ in scheduler.calls if name in ("release_scope", "release")]
    assert names == ["release_scope", "release"]
    assert scheduler.leases.get("issue-9") is None
    assert scheduler.scope_locks.get("issue-9") is None


def test_worktree_cleanup_policy(scheduler):
    allocator = RecordingAllocator()
    lifecycle = _lifecycle(scheduler, allocator)
    cases = [
        ("completed", True, []),
        ("failed", True, []),
        ("blocked-hooks", True, []),
        ("cancelled", True, ["task-x"]),
        ("cancelled", False, []),  # 子进程未确认退出 → 不允许删
        ("error", False, []),
    ]
    for outcome, confirmed, expected in cases:
        active = tl.ActiveTask(
            issue=1,
            task_id="issue-1",
            slug="task-x",
            worktree=Path("dummy-worktree"),
            executor_confirmed_exit=confirmed,
        )
        result = lifecycle.cleanup(active, outcome)
        assert allocator.released == expected, outcome
        if outcome == "cancelled" and not confirmed:
            assert any("未确认退出" in err for err in result.errors)
        allocator.released.clear()
