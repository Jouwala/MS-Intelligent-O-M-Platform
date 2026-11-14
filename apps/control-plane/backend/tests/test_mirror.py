"""T05（回写侧）：outbox 重放到 GitHub —— 幂等、失败保留、最终收敛。

用记录型内存传输模拟 GitHub（不访问网络）；关键验证点是「评论不重复、标签必须
收敛到目标状态、失败只标记不抛异常、重试后自动修复」。
"""

from __future__ import annotations

import pytest

import sched_testkit as tk
from scheduler import GitHubMirror
from scheduler.completion import OUTBOX_ERROR, OUTBOX_PENDING, completion_marker


@pytest.fixture()
def issue_number() -> int:
    return 42


@pytest.fixture()
def transport(issue_number):
    transport = tk.RecordingTransport()
    transport.add_issue(issue_number)
    return transport


@pytest.fixture()
def mirror(transport):
    return GitHubMirror(transport)


@pytest.fixture()
def completed(svc, issue_number):
    """完成任务并向 outbox 写入 3 条待重放操作。"""
    svc.upsert_task(f"issue-{issue_number}", issue_number, repo="o/r")
    lease = tk.claim(svc, f"issue-{issue_number}")
    svc.transition(
        f"issue-{issue_number}",
        attempt_id="a-1",
        fencing_token=lease.fencing_token,
        state="running",
    )
    return svc.complete(
        f"issue-{issue_number}",
        attempt_id="a-1",
        fencing_token=lease.fencing_token,
        idempotency_key="key-1",
        summary="执行完成，自检通过。",
    )


def test_m01_drain_applies_comment_labels_audit(svc, transport, mirror, completed, issue_number):
    assert mirror.drain(svc) == 3
    assert mirror.last_errors == []
    bodies = transport.comment_bodies(issue_number)
    assert len(bodies) == 1
    assert "TASK_COMPLETED" in bodies[0]
    assert completion_marker("key-1") in bodies[0]
    assert transport.get_issue(issue_number).labels == (
        "agent:muse-2",
        "state:in-review",
    )
    assert svc.pending_outbox() == []


def test_m02_second_drain_has_no_side_effects(svc, transport, mirror, completed, issue_number):
    assert mirror.drain(svc) == 3
    before = transport.add_comment_calls
    assert mirror.drain(svc) == 0
    assert transport.add_comment_calls == before
    assert len(transport.comment_bodies(issue_number)) == 1


def test_m03_comment_already_posted_is_not_duplicated(svc, transport, mirror, completed, issue_number):
    # 模拟「评论已发出、进程在 mark_outbox 前崩溃」：outbox 行仍是 pending，评论已在远端
    marker = completion_marker("key-1")
    transport.add_comment(issue_number, f"<!-- agent-msg: TASK_COMPLETED -->\n{marker}")
    assert mirror.drain(svc) == 3
    assert len(transport.comment_bodies(issue_number)) == 1  # 没有第二条


def test_m04_label_failure_is_retried_and_repaired(svc, transport, mirror, completed, issue_number):
    transport.fail_labels_times = 1
    applied = mirror.drain(svc)
    assert applied == 2  # comment + audit 成功，labels 失败
    assert mirror.last_errors and "模拟改标签失败" in mirror.last_errors[0]
    pending = svc.pending_outbox()
    assert [op.kind for op in pending] == ["labels"]
    assert pending[0].status == OUTBOX_ERROR and pending[0].attempts == 1
    assert len(transport.comment_bodies(issue_number)) == 1

    # 重试：只剩 labels，评论不再重发
    assert mirror.drain(svc) == 1
    assert mirror.last_errors == []
    assert svc.pending_outbox() == []
    assert transport.get_issue(issue_number).labels[-1] == "state:in-review"
    assert transport.set_labels_calls == 2  # 失败一次 + 成功一次
    assert len(transport.comment_bodies(issue_number)) == 1


def test_m05_comment_failure_does_not_raise_and_retries(svc, transport, mirror, completed, issue_number):
    transport.fail_comment_times = 1
    applied = mirror.drain(svc)  # 不抛异常
    assert applied == 2
    assert any("op=" in error for error in mirror.last_errors)
    pending = [op for op in svc.pending_outbox() if op.kind == "comment"]
    assert pending and pending[0].status == OUTBOX_ERROR

    assert mirror.drain(svc) == 1
    assert len(transport.comment_bodies(issue_number)) == 1


def test_m06_labels_already_converged_no_write(svc, transport, mirror, completed, issue_number):
    transport.get_issue(issue_number).labels = ("agent:muse-2", "state:in-review")
    mirror.drain(svc)
    assert transport.set_labels_calls == 0  # 已是目标状态，幂等跳过


def test_m07_unknown_outbox_kind_marked_error_without_raising(
    svc, transport, mirror, completed, issue_number, clock
):
    svc.store.run(
        "INSERT INTO outbox(task_id, kind, payload, status, attempts, last_error,"
        " created_at, updated_at) VALUES (?,?,?,?,0,'',?,?)",
        ("issue-42", "bogus", "{}", OUTBOX_PENDING, 0, 0),
    )
    mirror.drain(svc)
    assert any("bogus" in error for error in mirror.last_errors)
    bogus = svc.store.one("SELECT * FROM outbox WHERE kind = 'bogus'")
    assert bogus["status"] == OUTBOX_ERROR


def test_m08_mirror_accepts_scheduler_in_constructor(svc, transport, completed, issue_number):
    mirror = GitHubMirror(transport, svc)
    assert mirror.drain() == 3
    assert len(transport.comment_bodies(issue_number)) == 1


def test_m09_drain_respects_limit(svc, transport, mirror, completed):
    assert mirror.drain(svc, limit=1) == 1
    assert len(svc.pending_outbox()) == 2


def test_m10_works_with_existing_in_memory_transport(svc, adapter_module, issue_number):
    """兼容既有 InMemoryGitHubTransport（Worker/适配器测试长期使用的那个）。"""
    transport = adapter_module.InMemoryGitHubTransport()
    transport.create_issue(
        issue_number, "任务", labels=("agent:muse-2", "state:claimed")
    )
    svc.upsert_task(f"issue-{issue_number}", issue_number, repo="o/r")
    lease = tk.claim(svc, f"issue-{issue_number}")
    svc.transition(
        f"issue-{issue_number}",
        attempt_id="a-1",
        fencing_token=lease.fencing_token,
        state="running",
    )
    svc.complete(
        f"issue-{issue_number}",
        attempt_id="a-1",
        fencing_token=lease.fencing_token,
        idempotency_key="key-mem",
        summary="完成",
    )
    mirror = GitHubMirror(transport)
    assert mirror.drain(svc) == 3
    assert mirror.last_errors == []
    comments = transport.list_comments(issue_number)
    assert len(comments) == 1
    assert completion_marker("key-mem") in comments[0].body
    assert "state:in-review" in transport.get_issue(issue_number).labels


def test_m11_failed_terminal_mirrors_blocked_label_and_task_failed(
    svc, transport, mirror, issue_number
):
    """执行失败（非 QA 失败）镜像到 state:blocked，评论头是扩展的 TASK_FAILED。"""
    task_id = f"issue-{issue_number}"
    svc.upsert_task(task_id, issue_number, repo="o/r")
    lease = tk.claim(svc, task_id)
    svc.transition(task_id, attempt_id="a-1", fencing_token=lease.fencing_token, state="running")
    svc.complete(
        task_id,
        attempt_id="a-1",
        fencing_token=lease.fencing_token,
        idempotency_key="fail-key",
        summary="命令退出码非 0",
        terminal_status="failed",
    )
    assert mirror.drain(svc) == 3
    assert transport.get_issue(issue_number).labels == ("agent:muse-2", "state:blocked")
    assert "TASK_FAILED" in transport.comment_bodies(issue_number)[0]
