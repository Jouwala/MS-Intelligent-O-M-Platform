"""Worker 任务流程（fake GitHub）：成功/无命令/失败/超时/范围冲突/取消/续租。

断言全部落在**真实 fake GitHub 的 refs 与 Issue 评论/标签**上，不 mock 权威逻辑。
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import timedelta

from _linux_fixtures import (  # noqa: F401 - pytest 夹具（含依赖链）
    fake_github,
    make_config,
    make_worker,
    runtime,
)
from _linux_testkit import make_body


def _add_task(server, number=1, *, agent="muse-1-linux", command=None, scope=()):
    server.add_issue(
        number,
        labels=(f"agent:{agent}", "state:todo"),
        title=f"任务 {number}",
        body=make_body(command=command, scope=scope),
    )


def _refs(server, prefix: str) -> list[str]:
    return [ref for ref in server.refs_snapshot() if ref.startswith(prefix)]


def _comments(server, number=1) -> list[str]:
    return server.comment_bodies(number)


def _wait_until(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_success_command_completes_with_comment_labels_and_ref_cleanup(
    fake_github, make_worker
):
    _add_task(
        fake_github,
        command=[sys.executable, "-c", "print('hello-from-linux-worker')"],
        scope=["notes.txt"],
    )
    worker = make_worker("muse-1-linux")
    result = worker.run_cycle()
    assert result["outcome"] == "completed", result

    labels = fake_github.issue_labels(1)
    assert "state:in-review" in labels
    assert "agent:muse-1-linux" in labels
    assert "state:todo" not in labels

    completions = [body for body in _comments(fake_github, 1) if "ref-completion-id:" in body]
    assert len(completions) == 1, completions
    # lease ref 完成后删除；HWM 是权威代次、只增不删；范围锁也清掉
    assert _refs(fake_github, "refs/heads/agent-lease/issue-1/") == []
    assert len(_refs(fake_github, "refs/heads/agent-hwm/issue-1/")) == 1
    assert _refs(fake_github, "refs/heads/agent-scope/") == []

    task = worker.scheduler.get_task("issue-1")
    assert task.state == "completed"
    assert task.idempotency_key
    # 重复 complete（同一 attempt/幂等键）不产生第二条完成评论
    replay = worker.scheduler.complete(
        "issue-1",
        attempt_id=task.attempt_id,
        fencing_token=task.fencing_token,
        idempotency_key=task.idempotency_key,
        summary="重复回传",
    )
    assert replay.created is False
    assert len(
        [body for body in _comments(fake_github, 1) if "ref-completion-id:" in body]
    ) == 1
    log_file = worker.config.task_log_dir(1) / "execution.log"
    assert log_file.is_file()
    assert "hello-from-linux-worker" in log_file.read_text(encoding="utf-8")


def test_no_command_blocks_and_releases_lease(fake_github, make_worker):
    _add_task(fake_github, scope=["a.txt"])  # 有「改动文件」但没有「执行命令」
    worker = make_worker("muse-1-linux")
    result = worker.run_cycle()
    assert result["outcome"] == "blocked", result

    labels = fake_github.issue_labels(1)
    assert "state:blocked" in labels and "state:todo" not in labels
    assert _refs(fake_github, "refs/heads/agent-lease/issue-1/") == []
    assert len(_refs(fake_github, "refs/heads/agent-hwm/issue-1/")) == 1
    bodies = "\n".join(_comments(fake_github, 1))
    assert "state=blocked" in bodies
    assert "ref-completion-id:" not in bodies
    state = worker.scheduler.get_task("issue-1").state
    assert state in (worker.S.BLOCKED, worker.S.QUEUED, worker.S.RETRYABLE)


def test_execution_failure_transitions_failed(fake_github, make_worker):
    _add_task(fake_github, command=[sys.executable, "-c", "import sys; sys.exit(3)"])
    worker = make_worker("muse-1-linux")
    result = worker.run_cycle()
    assert result["outcome"] == "failed", result

    labels = fake_github.issue_labels(1)
    assert "state:blocked" in labels
    assert _refs(fake_github, "refs/heads/agent-lease/issue-1/") == []
    bodies = "\n".join(_comments(fake_github, 1))
    assert "state=failed" in bodies
    assert "execution-failed" in bodies
    assert "ref-completion-id:" not in bodies


def test_execution_timeout_transitions_failed(fake_github, make_worker):
    _add_task(fake_github, command=[sys.executable, "-c", "import time; time.sleep(5)"])
    worker = make_worker("muse-1-linux", executor_timeout_seconds=0.5)
    started = time.monotonic()
    result = worker.run_cycle()
    assert result["outcome"] == "failed", result
    assert time.monotonic() - started < 20
    labels = fake_github.issue_labels(1)
    assert "state:blocked" in labels
    assert _refs(fake_github, "refs/heads/agent-lease/issue-1/") == []
    bodies = "\n".join(_comments(fake_github, 1))
    assert "executor-timeout" in bodies


def test_scope_conflict_releases_and_keeps_other_lock(fake_github, make_worker):
    _add_task(fake_github, 1, command=["true"], scope=["apps/x"])
    _add_task(fake_github, 2, agent="muse-2-linux", command=["true"], scope=["apps/x"])
    holder = make_worker("muse-2-linux")
    holder.scheduler.upsert_task("issue-2", 2, repo=holder.config.repo, scopes=["apps/x"])
    lease = holder.scheduler.claim(
        "issue-2",
        agent_id=holder.config.ref_agent_id,
        instance_id=holder.config.ref_instance,
        attempt_id="a1b2c3d4e5f6a7b8",
        lease_ttl=timedelta(seconds=600),
    )
    holder.scheduler.transition(
        "issue-2",
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
        state=holder.S.RUNNING,
    )
    holder.scheduler.acquire_scope(
        "issue-2",
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
        scopes=["apps/x"],
    )

    worker = make_worker("muse-1-linux")
    result = worker.run_cycle()
    assert result["outcome"] == "scope-conflict", result
    assert "state:todo" in fake_github.issue_labels(1)
    assert _refs(fake_github, "refs/heads/agent-lease/issue-1/") == []
    # 别人的租约与范围锁不得被误放
    assert len(_refs(fake_github, "refs/heads/agent-lease/issue-2/")) == 1
    assert len(_refs(fake_github, "refs/heads/agent-scope/")) >= 1


def test_cancel_during_execution_acks_cancel(fake_github, make_worker):
    _add_task(fake_github, command=[sys.executable, "-c", "import time; time.sleep(30)"])
    worker = make_worker("muse-1-linux", executor_timeout_seconds=60)
    worker.cancel_poll_seconds = 0.1
    other = make_worker("muse-2-linux")
    outcome: dict = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault("value", worker.run_cycle()), daemon=True
    )
    thread.start()
    try:
        # 等 Worker 进入 running（认领/租约已建立），再发取消请求
        assert _wait_until(
            lambda: any("state=running" in body for body in _comments(fake_github, 1)),
            timeout=20,
        )
        other.scheduler.request_cancel("issue-1", reason="测试取消")
        thread.join(timeout=40)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            other.scheduler.request_cancel("issue-1", reason="兜底取消")
            thread.join(timeout=10)
    assert outcome.get("value") == {"outcome": "cancelled", "issue": 1, "task_id": "issue-1"}
    labels = fake_github.issue_labels(1)
    assert "state:todo" in labels  # cancelled 由权威层收敛回 todo
    assert _refs(fake_github, "refs/heads/agent-lease/issue-1/") == []
    bodies = "\n".join(_comments(fake_github, 1))
    assert "state=cancelled" in bodies
    assert "ref-completion-id:" not in bodies


def test_renew_advances_fencing_token_and_completion_uses_it(fake_github, make_worker):
    _add_task(
        fake_github,
        command=[sys.executable, "-c", "import time; time.sleep(0.8); print('done')"],
    )
    worker = make_worker(
        "muse-1-linux", renew_interval_seconds=0.1, executor_timeout_seconds=30
    )
    result = worker.run_cycle()
    assert result["outcome"] == "completed", result
    hwm = _refs(fake_github, "refs/heads/agent-hwm/issue-1/")
    assert len(hwm) >= 2, hwm  # 续租推进过代次
    latest_token = max(int(ref.rsplit("/", 1)[1]) for ref in hwm)
    completion = next(
        body for body in _comments(fake_github, 1) if "ref-completion-id:" in body
    )
    assert f"token={latest_token:08d}" in completion
    assert _refs(fake_github, "refs/heads/agent-lease/issue-1/") == []
