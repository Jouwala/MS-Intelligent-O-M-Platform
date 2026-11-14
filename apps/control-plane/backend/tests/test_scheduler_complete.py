"""T05：完成幂等、outbox 对账、旧执行者拒绝、complete 与 cancel 并发。

这些用例验证的是「本地终态一次成型 + GitHub 回写可重放」：complete 的事务只写
终态/幂等记录/outbox，评论与标签由 mirror 异步重放；因此这里用真实 SQLite 检查
事务结果，不 Mock 数据库。
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

import sched_testkit as tk
from scheduler import (
    InvalidTransition,
    SchedulerError,
    SchedulerService,
    StaleFencing,
)
from scheduler.api import COMPLETED
from scheduler.completion import (
    OUTBOX_DONE,
    OUTBOX_ERROR,
    OUTBOX_PENDING,
    completion_marker,
)


def _running(svc, task_id="issue-1", *, instance="i-1", attempt="a-1"):
    svc.upsert_task(task_id, 1)
    lease = tk.claim(svc, task_id, instance=instance, attempt=attempt)
    svc.transition(
        task_id, attempt_id=attempt, fencing_token=lease.fencing_token, state="running"
    )
    return lease


def _complete(svc, lease, key="k1", summary="完成"):
    return svc.complete(
        "issue-1",
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
        idempotency_key=key,
        summary=summary,
    )


def test_c01_complete_writes_terminal_completion_and_outbox(svc):
    lease = _running(svc)
    outcome = _complete(svc, lease)
    assert outcome.created is True
    assert outcome.terminal_status == COMPLETED
    assert outcome.outbox_ops == 3
    task = svc.get_task("issue-1")
    assert task.state == COMPLETED and task.is_terminal
    assert task.idempotency_key == "k1"
    assert task.lease_expire_at is None  # 终态快照不再显示租约
    assert svc.store.one("SELECT * FROM leases WHERE task_id = ?", ("issue-1",)) is None
    rows = svc.store.all("SELECT kind, status FROM outbox ORDER BY op_id")
    assert [(row["kind"], row["status"]) for row in rows] == [
        ("comment", OUTBOX_PENDING),
        ("labels", OUTBOX_PENDING),
        ("audit", OUTBOX_PENDING),
    ]
    completed = svc.store.one("SELECT * FROM completions WHERE idempotency_key = ?", ("k1",))
    assert completed is not None and completed["attempt_id"] == "a-1"


def test_c02_replay_same_key_no_new_side_effects(svc):
    lease = _running(svc)
    _complete(svc, lease)
    before = svc.store.all("SELECT op_id, status FROM outbox ORDER BY op_id")
    replay = _complete(svc, lease)
    assert replay.created is False and replay.outbox_ops == 0
    assert replay.terminal_status == COMPLETED
    after = svc.store.all("SELECT op_id, status FROM outbox ORDER BY op_id")
    assert [(r["op_id"], r["status"]) for r in before] == [
        (r["op_id"], r["status"]) for r in after
    ]
    assert [a["event"] for a in svc.list_audit("issue-1")].count("complete") == 1


def test_c03_replay_same_attempt_with_different_key_is_replay(svc):
    lease = _running(svc)
    _complete(svc, lease, key="k1")
    replay = svc.complete(
        "issue-1",
        attempt_id="a-1",
        fencing_token=lease.fencing_token,
        idempotency_key="k2",
        summary="换一个幂等键重放",
    )
    assert replay.created is False
    assert replay.idempotency_key == "k1"
    assert len(svc.store.all("SELECT * FROM outbox")) == 3


def test_c04_terminal_task_with_new_attempt_refused(svc):
    lease = _running(svc)
    _complete(svc, lease)
    with pytest.raises(SchedulerError):
        svc.complete(
            "issue-1",
            attempt_id="a-2",
            fencing_token=lease.fencing_token,
            idempotency_key="k-other",
            summary="重复完成",
        )


def test_c05_old_attempt_and_token_rejected(svc, clock):
    first = _running(svc)
    clock.advance(11 * 60)
    # 过期后由另一个实例重新认领
    svc.upsert_task("issue-1", 1)
    second = tk.claim(svc, "issue-1", agent="muse-3", instance="i-2", attempt="a-2")
    svc.transition(
        "issue-1", attempt_id="a-2", fencing_token=second.fencing_token, state="running"
    )
    for attempt, token in (("a-1", first.fencing_token), ("a-2", first.fencing_token)):
        with pytest.raises(StaleFencing):
            svc.complete(
                "issue-1",
                attempt_id=attempt,
                fencing_token=token,
                idempotency_key="late-" + attempt,
                summary="迟到完成",
            )
    assert svc.get_task("issue-1").state == "running"


def test_c06_complete_from_claimed_without_running_is_invalid(svc):
    svc.upsert_task("issue-1", 1)
    lease = tk.claim(svc)
    with pytest.raises(InvalidTransition):
        _complete(svc, lease)
    assert svc.get_task("issue-1").state == "claimed"


def test_c07_complete_after_reap_is_stale(svc, clock):
    lease = tk.prepare_running(svc)
    clock.advance(11 * 60)
    svc.reap_expired()
    with pytest.raises(StaleFencing):
        _complete(svc, lease)
    assert svc.get_task("issue-1").state in ("expired", "retryable", "queued")


def test_c08_complete_failed_terminal_and_label_target(svc):
    lease = _running(svc)
    outcome = svc.complete(
        "issue-1",
        attempt_id="a-1",
        fencing_token=lease.fencing_token,
        idempotency_key="fail-1",
        summary="执行失败：测试挂起",
        terminal_status="failed",
    )
    assert outcome.created is True
    task = svc.get_task("issue-1")
    assert task.state == "failed" and task.error_message
    payloads = {
        row["kind"]: row["payload"] for row in svc.store.all("SELECT * FROM outbox")
    }
    # 执行失败不是 QA 冒烟失败：落 state:blocked（协议允许从任何状态进入）
    assert "state:blocked" in payloads["labels"]
    assert completion_marker("fail-1") in payloads["comment"]


def test_c09_outbox_mark_and_retry_state(svc):
    lease = _running(svc)
    _complete(svc, lease)
    ops = svc.pending_outbox()
    svc.mark_outbox(ops[0].op_id, status=OUTBOX_DONE)
    svc.mark_outbox(ops[1].op_id, status=OUTBOX_ERROR, error="模拟失败")
    pending = svc.pending_outbox()
    assert [op.kind for op in pending] == ["labels", "audit"]
    failed = pending[0]
    assert failed.status == OUTBOX_ERROR
    assert failed.attempts == 1
    assert "模拟失败" in failed.last_error
    with pytest.raises(SchedulerError):
        svc.mark_outbox(999999, status=OUTBOX_DONE)
    with pytest.raises(ValueError):
        svc.mark_outbox(ops[2].op_id, status="bogus")


def test_c10_idempotency_key_cannot_cross_tasks(svc):
    first = _running(svc)
    _complete(svc, first, key="shared-key")
    svc.upsert_task("issue-2", 2)
    lease2 = tk.claim(svc, "issue-2", instance="i-9", attempt="a-9")
    svc.transition(
        "issue-2", attempt_id="a-9", fencing_token=lease2.fencing_token, state="running"
    )
    with pytest.raises(SchedulerError):
        svc.complete(
            "issue-2",
            attempt_id="a-9",
            fencing_token=lease2.fencing_token,
            idempotency_key="shared-key",
            summary="跨任务复用幂等键",
        )


def test_c11_complete_and_cancel_concurrent_converge(db_path):
    fixed = tk.T0
    setup = SchedulerService(db_path, clock=lambda: fixed)
    setup.upsert_task("issue-1", 1)
    lease = tk.claim(setup, "issue-1")
    setup.transition(
        "issue-1", attempt_id="a-1", fencing_token=lease.fencing_token, state="running"
    )
    setup.close()

    outcome: dict[str, str] = {}
    barrier = threading.Barrier(2, timeout=20)

    def do_complete() -> None:
        service = SchedulerService(db_path, clock=lambda: fixed)
        try:
            barrier.wait()
            try:
                service.complete(
                    "issue-1",
                    attempt_id="a-1",
                    fencing_token=lease.fencing_token,
                    idempotency_key="race-k",
                    summary="并发完成",
                )
                outcome["complete"] = "ok"
            except InvalidTransition:
                outcome["complete"] = "invalid"
        finally:
            service.close()

    def do_cancel() -> None:
        service = SchedulerService(db_path, clock=lambda: fixed)
        try:
            barrier.wait()
            service.request_cancel("issue-1", reason="并发取消")
        finally:
            service.close()

    threads = [threading.Thread(target=do_complete), threading.Thread(target=do_cancel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    check = SchedulerService(db_path, clock=lambda: fixed)
    try:
        task = check.get_task("issue-1")
        assert task.state in ("completed", "cancel_requested")
        if task.state == "completed":
            assert task.terminal_status == COMPLETED
            assert len(check.pending_outbox()) == 3
        else:
            assert outcome["complete"] == "invalid"
            assert check.store.one("SELECT * FROM completions") is None
            acked = check.ack_cancel(
                "issue-1", attempt_id="a-1", fencing_token=lease.fencing_token
            )
            assert acked.state == "cancelled"
            assert check.store.one("SELECT * FROM leases WHERE task_id = ?", ("issue-1",)) is None
    finally:
        check.close()


def test_c12_upsert_never_resets_state_or_fencing_token(svc):
    lease = _running(svc)
    before = svc.get_task("issue-1")
    svc.upsert_task("issue-1", 1, repo="o/r", title="新标题", scopes=["apps/x.py"])
    after = svc.get_task("issue-1")
    assert after.state == before.state
    assert after.fencing_token == before.fencing_token
    assert after.attempt_id == "a-1"
    assert after.title == "新标题"
    assert after.scopes == ("apps/x.py",)
    # 空 scopes 不覆盖已有范围
    svc.upsert_task("issue-1", 1)
    assert svc.get_task("issue-1").scopes == ("apps/x.py",)


def test_c13_expired_lease_not_taken_over_can_still_complete(svc, clock):
    """租约过期但没人接管：旧执行者的结果仍允许完成（authority 看 fencing，不看过期）。"""
    lease = _running(svc)
    clock.advance(11 * 60)  # 过期；未 reap、未被新实例 claim
    outcome = _complete(svc, lease)
    assert outcome.created is True
    assert svc.get_task("issue-1").state == COMPLETED


def test_c14_expired_lease_taken_over_is_stale(svc, clock):
    """租约过期且已被新实例接管：旧执行者 complete 必然 StaleFencing。"""
    first = _running(svc)
    clock.advance(11 * 60)
    svc.upsert_task("issue-1", 1)
    second = tk.claim(svc, "issue-1", agent="muse-3", instance="i-2", attempt="a-2")
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(StaleFencing):
        _complete(svc, first)
