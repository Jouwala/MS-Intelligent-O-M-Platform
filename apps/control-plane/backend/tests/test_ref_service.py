"""PR-5（组装）：`GitHubRefScheduler` 实现 `api.Scheduler` 的外围职责。

覆盖：upsert/get 派生、Worker 注册表（本地 no-op 与 registry Issue 评论两种模式）、
范围锁委托 ref_scope、回收（ref_reap + 孤儿 scope 清理）、分派幂等、outbox 不存在、
审计、以及冻结的构造签名（B2 factory 依赖）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from scheduler import ref_api
from scheduler.api import (
    CANCEL_REQUESTED,
    CLAIMED,
    COMPLETED,
    QUEUED,
    RUNNING,
    ScopeConflict,
    SchedulerError,
    StaleFencing,
    TaskNotFound,
)
from scheduler.ref_service import GitHubRefScheduler

AGENT = "muse-2"
INSTANCE = "1a2b3c4d"
ATTEMPT = "a1b2c3d4e5f6"


def scheduler(ref_store, ref_transport, ref_clock, **kwargs) -> GitHubRefScheduler:
    return GitHubRefScheduler(ref_store, ref_transport, repo="o/r", clock=ref_clock, **kwargs)


def claim(authority, task_id="issue-1", *, agent=AGENT, instance=INSTANCE, attempt=ATTEMPT):
    return authority.claim(
        task_id, agent_id=agent, instance_id=instance, attempt_id=attempt
    )


def lease_refs(fake_github, issue=1) -> list[str]:
    prefix = f"refs/heads/agent-lease/issue-{issue}/"
    return [ref for ref in fake_github.refs_snapshot() if ref.startswith(prefix)]


def test_frozen_constructor_signature_matches_factory(fake_github, ref_store, ref_transport, ref_clock):
    """B2 的 factory 只传冻结关键字（不穿私有参数）必须可用。"""
    authority = GitHubRefScheduler(
        ref_store,
        ref_transport,
        repo="o/r",
        clock=ref_clock,
        lease_ttl=timedelta(minutes=10),
        registry_issue=None,
        case_insensitive_scopes=False,
    )
    assert isinstance(authority, GitHubRefScheduler)


def test_upsert_task_is_idempotent_and_does_not_write_authority(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    authority = scheduler(ref_store, ref_transport, ref_clock)
    first = authority.upsert_task("issue-1", 1, repo="o/r", title="标题", scopes=["a.py"])
    second = authority.upsert_task("issue-1", 1, repo="o/r", title="标题")
    assert first.state == QUEUED and second.state == QUEUED
    assert first.title == "标题" and second.scopes == ("a.py",)
    # 不写权威状态：没有 HWM / lease ref
    assert not [ref for ref in fake_github.refs_snapshot() if "/agent-hwm/" in ref]


def test_get_task_derives_state_from_refs_and_comments(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = scheduler(ref_store, ref_transport, ref_clock)
    authority.upsert_task("issue-1", 1)
    lease = claim(authority)
    assert authority.get_task("issue-1").state == CLAIMED
    authority.transition(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token, state=RUNNING
    )
    assert authority.get_task("issue-1").state == RUNNING
    authority.complete(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
        idempotency_key="k1", summary="完成",
    )
    task = authority.get_task("issue-1")
    assert task.state == COMPLETED and task.terminal_status == COMPLETED
    assert task.idempotency_key == "k1" and task.fencing_token == 1


def test_get_task_unknown_issue_is_task_not_found(fake_github, ref_store, ref_transport, ref_clock):
    authority = scheduler(ref_store, ref_transport, ref_clock)
    with pytest.raises(TaskNotFound):
        authority.get_task("issue-404")


def test_worker_registry_local_noop_mode(fake_github, ref_store, ref_transport, ref_clock):
    authority = scheduler(ref_store, ref_transport, ref_clock)
    worker = authority.register_worker(
        AGENT, INSTANCE, hostname="host-a", platform="linux", capabilities=("python", "git")
    )
    assert worker.state == "online" and worker.hostname == "host-a"
    assert authority.heartbeat_worker(AGENT, INSTANCE) is True
    assert authority.heartbeat_worker(AGENT, "unknown") is False
    workers = authority.list_workers(stale_after=timedelta(seconds=90))
    assert [item.instance_id for item in workers] == [INSTANCE]
    assert workers[0].capabilities == ("python", "git")
    ref_clock.advance(3600)
    assert authority.list_workers(stale_after=timedelta(seconds=90))[0].state == "offline"


def test_worker_registry_issue_comments_and_heartbeat_throttle(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(99)
    authority = scheduler(ref_store, ref_transport, ref_clock, registry_issue=99)
    worker = authority.register_worker(AGENT, INSTANCE, hostname="h1", platform="linux")
    assert worker.state == "online"
    comments_after_register = len(fake_github.comment_bodies(99))
    assert comments_after_register >= 1
    # 节流窗口内不重复发评论，但心跳被接受
    assert authority.heartbeat_worker(AGENT, INSTANCE) is True
    assert len(fake_github.comment_bodies(99)) == comments_after_register
    ref_clock.advance(61)
    assert authority.heartbeat_worker(AGENT, INSTANCE) is True
    assert len(fake_github.comment_bodies(99)) == comments_after_register + 1
    workers = authority.list_workers(stale_after=timedelta(seconds=90))
    assert workers[0].hostname == "h1" and workers[0].state == "online"


def test_list_workers_parses_adapter_style_registry_comments(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(99)
    fake_github.add_comment(
        99,
        "<!-- worker-registry: muse-3, at: 2026-11-14T02:00:00Z -->\n"
        "<!-- worker-instance: inst-9, hostname: h9, platform: linux -->\n"
        "Worker 注册：`muse-3`",
    )
    authority = scheduler(ref_store, ref_transport, ref_clock, registry_issue=99)
    workers = {item.instance_id: item for item in authority.list_workers()}
    assert "inst-9" in workers
    assert workers["inst-9"].hostname == "h9"


def test_scope_delegation_acquire_holders_release(fake_github, ref_store, ref_transport, ref_clock):
    pytest.importorskip("scheduler.ref_scope")
    fake_github.add_issue(1)
    fake_github.add_issue(2)
    authority = scheduler(ref_store, ref_transport, ref_clock)
    first = claim(authority)
    authority.acquire_scope(
        "issue-1",
        attempt_id=ATTEMPT,
        fencing_token=first.fencing_token,
        scopes=["src/a.py"],
    )
    holders = authority.scope_holders()
    assert "issue-1" in holders
    # 第二个任务申请重叠范围 → ScopeConflict（不同任务）
    second = claim(authority, "issue-2", instance="9f9f9f9f", attempt="bbbbbbbbbbbb")
    with pytest.raises(ScopeConflict):
        authority.acquire_scope(
            "issue-2",
            attempt_id="bbbbbbbbbbbb",
            fencing_token=second.fencing_token,
            scopes=["src/a.py"],
        )
    assert authority.release_scope(
        "issue-1", attempt_id=ATTEMPT, fencing_token=first.fencing_token
    ) is True
    assert "issue-1" not in authority.scope_holders()
    with pytest.raises(StaleFencing):
        authority.release_scope("issue-1", attempt_id=ATTEMPT, fencing_token=999)


def test_reap_expired_reports_and_cleans_orphan_scopes(
    fake_github, ref_store, ref_transport, ref_clock
):
    pytest.importorskip("scheduler.ref_scope")
    fake_github.add_issue(1)
    authority = scheduler(
        ref_store, ref_transport, ref_clock, lease_ttl=timedelta(seconds=60)
    )
    lease = claim(authority)
    authority.acquire_scope(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token, scopes=["src/a.py"]
    )
    ref_clock.advance(61)
    report = authority.reap_expired()
    assert report.expired_leases == ("issue-1",)
    assert report.retryable == ("issue-1",)
    assert report.orphan_attempts == (ATTEMPT,)
    assert any("/agent-scope/" in ref for ref in report.released_locks)
    assert lease_refs(fake_github) == []
    assert authority.get_task("issue-1").state == "retryable"
    # 幂等：第二次没有可回收项
    assert authority.reap_expired().expired_leases == ()


def test_reap_cancel_requested_converges_cancelled(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    authority = scheduler(
        ref_store, ref_transport, ref_clock, lease_ttl=timedelta(seconds=60)
    )
    claim(authority)
    authority.request_cancel("issue-1", reason="停止")
    assert authority.get_task("issue-1").state == CANCEL_REQUESTED
    ref_clock.advance(61)
    authority.reap_expired()
    task = authority.get_task("issue-1")
    assert task.state == "cancelled" and task.is_terminal


def test_pending_outbox_is_empty_and_mark_outbox_raises(
    fake_github, ref_store, ref_transport, ref_clock
):
    authority = scheduler(ref_store, ref_transport, ref_clock)
    assert authority.pending_outbox() == []
    with pytest.raises(SchedulerError):
        authority.mark_outbox(1, status="done")


def test_register_dispatch_idempotent_and_status(fake_github, ref_store, ref_transport, ref_clock):
    first = scheduler(ref_store, ref_transport, ref_clock)
    second = scheduler(ref_store, ref_transport, ref_clock)
    assert first.register_dispatch("repo|pr-1|deadbeef", issue_number=7, pr_number=3) is True
    assert second.register_dispatch("repo|pr-1|deadbeef", issue_number=7) is False
    record = second.dispatch_state("repo|pr-1|deadbeef")
    assert record is not None and record.status == "registered"
    second.set_dispatch_status("repo|pr-1|deadbeef", "comment_posted", comment_posted=True)
    assert second.dispatch_state("repo|pr-1|deadbeef").comment_posted is True
    assert second.dispatch_state("never-registered") is None
    with pytest.raises(SchedulerError):
        second.set_dispatch_status("never-registered", "x")


def test_list_audit_derives_from_comments(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = scheduler(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    authority.transition(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token, state=RUNNING
    )
    authority.renew("issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token)
    events = [entry["event"] for entry in authority.list_audit("issue-1", limit=10)]
    assert events[0] == "renew"
    assert "claim" in events and "transition" in events
