"""PR-5（完成）：幂等完成评论 + 标签有界重试，不使用本地 outbox/mirror。

断言：完成评论是唯一权威标记（重复提交只 1 条）、旧 token 被 fencing 拒绝、
标签是展示层（失败不回滚完成、有界重试后记录 `label_errors` 交给 Actions 对账）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from scheduler.api import (
    CLAIMED,
    COMPLETED,
    FAILED,
    RUNNING,
    AlreadyTerminal,
    CompleteOutcome,
    InvalidTransition,
    SchedulerError,
    StaleFencing,
)
from scheduler.ref_completion import COMPLETION_LABELS, RefCompleter

AGENT = "muse-2"
INSTANCE = "1a2b3c4d"
ATTEMPT = "a1b2c3d4e5f6"


def completer(ref_store, ref_transport, clock, **kwargs) -> RefCompleter:
    return RefCompleter(ref_store, ref_transport, repo="o/r", clock=clock, **kwargs)


def claim(authority, *, agent=AGENT, instance=INSTANCE, attempt=ATTEMPT):
    return authority.claim(
        "issue-1", agent_id=agent, instance_id=instance, attempt_id=attempt
    )


def running(authority) -> object:
    lease = claim(authority)
    authority.transition(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token, state=RUNNING
    )
    return lease


def completion_comments(fake_github, key="k1") -> list[str]:
    marker = f"ref-completion-id: {key}"
    return [body for body in fake_github.comment_bodies(1) if marker in body]


def lease_refs(fake_github) -> list[str]:
    prefix = "refs/heads/agent-lease/issue-1/"
    return [ref for ref in fake_github.refs_snapshot() if ref.startswith(prefix)]


def hwm_refs(fake_github) -> list[str]:
    prefix = "refs/heads/agent-hwm/issue-1/"
    return sorted(ref for ref in fake_github.refs_snapshot() if ref.startswith(prefix))


def test_complete_idempotent_posts_one_comment(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1, labels=("state:running",))
    authority = completer(ref_store, ref_transport, ref_clock)
    lease = running(authority)
    first = authority.complete(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
        idempotency_key="k1", summary="完成",
    )
    second = authority.complete(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
        idempotency_key="k1", summary="完成",
    )
    assert isinstance(first, CompleteOutcome) and first.created is True
    assert second.created is False and second.terminal_status == COMPLETED
    assert len(completion_comments(fake_github)) == 1
    view = authority.view(1, task_id="issue-1")
    assert view.state == COMPLETED and view.terminal_status == COMPLETED
    assert view.idempotency_key == "k1"
    assert lease_refs(fake_github) == []
    assert hwm_refs(fake_github) == ["refs/heads/agent-hwm/issue-1/00000001"]
    # 标签收敛到 completed 对应的展示标签
    assert fake_github.issue_labels(1) == ("state:in-review",)


def test_complete_same_attempt_different_key_replays(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    authority = completer(ref_store, ref_transport, ref_clock)
    lease = running(authority)
    authority.complete(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
        idempotency_key="k1", summary="完成",
    )
    replay = authority.complete(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
        idempotency_key="k2", summary="重放",
    )
    assert replay.created is False and replay.idempotency_key == "k1"
    assert completion_comments(fake_github, "k2") == []


def test_complete_new_key_after_terminal_raises(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = completer(ref_store, ref_transport, ref_clock)
    lease = running(authority)
    authority.complete(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
        idempotency_key="k1", summary="完成",
    )
    with pytest.raises(SchedulerError):
        authority.complete(
            "issue-1", attempt_id="bbbbbbbbbbbb", fencing_token=lease.fencing_token,
            idempotency_key="k-other", summary="再来一次",
        )


def test_complete_from_claimed_is_invalid_transition(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    authority = completer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    with pytest.raises(InvalidTransition):
        authority.complete(
            "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
            idempotency_key="k1", summary="跳过 running",
        )


def test_complete_old_token_after_renew_is_stale(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = completer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    renewed = authority.renew("issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token)
    with pytest.raises(StaleFencing):
        authority.complete(
            "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
            idempotency_key="k-old", summary="旧执行者",
        )
    authority.transition(
        "issue-1", attempt_id=ATTEMPT, fencing_token=renewed.fencing_token, state=RUNNING
    )
    outcome = authority.complete(
        "issue-1", attempt_id=ATTEMPT, fencing_token=renewed.fencing_token,
        idempotency_key="k-new", summary="新执行者",
    )
    assert outcome.created is True


def test_complete_after_expiry_but_before_takeover_is_allowed(
    fake_github, ref_store, ref_transport, ref_clock
):
    """租约过期只决定「谁还能认领」；未被新实例接管时旧执行者完成仍有效。"""
    fake_github.add_issue(1, labels=("state:running",))
    authority = completer(
        ref_store, ref_transport, ref_clock, lease_ttl=timedelta(seconds=60)
    )
    lease = claim(authority)
    authority.transition(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token, state=RUNNING
    )
    ref_clock.advance(61)
    outcome = authority.complete(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
        idempotency_key="k-late", summary="过期但未被接管",
    )
    assert outcome.created is True
    assert authority.view(1).state == COMPLETED


def test_completed_task_cannot_be_reclaimed(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = completer(ref_store, ref_transport, ref_clock)
    lease = running(authority)
    authority.complete(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
        idempotency_key="k1", summary="完成",
    )
    before = set(hwm_refs(fake_github))
    with pytest.raises(AlreadyTerminal):
        claim(authority, agent="muse-3", instance="9f9f9f9f", attempt="bbbbbbbbbbbb")
    assert set(hwm_refs(fake_github)) == before  # 终态不再白烧 token


def test_label_write_failure_is_retried_bounded(fake_github, ref_store, ref_transport, ref_clock):
    """标签写失败（有界重试后成功）：完成照常成立，标签最终收敛。"""
    fake_github.add_issue(1, labels=("state:running",))
    authority = completer(ref_store, ref_transport, ref_clock, label_retries=2)
    lease = running(authority)
    # 每次标签调用最多 2 次 HTTP（transport max_retries=1）；注入 2 次失败让首轮重试耗尽
    fake_github.fail_next("/labels", status=500, times=2)
    outcome = authority.complete(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
        idempotency_key="k1", summary="完成",
    )
    assert outcome.created is True
    assert fake_github.issue_labels(1) == ("state:in-review",)
    assert authority.label_errors == []


def test_persistent_label_failure_does_not_fail_completion(
    fake_github, ref_store, ref_transport, ref_clock
):
    """标签持续失败：完成（评论=权威标记）仍成功，失败记录留给 Actions 对账收敛。"""
    fake_github.add_issue(1, labels=("state:running",))
    authority = completer(ref_store, ref_transport, ref_clock, label_retries=1)
    lease = running(authority)
    fake_github.fail_next("/labels", status=500, times=10)
    outcome = authority.complete(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
        idempotency_key="k1", summary="完成",
    )
    assert outcome.created is True
    assert len(completion_comments(fake_github)) == 1
    assert authority.view(1).state == COMPLETED
    assert authority.label_errors  # 可观测，不静默
    assert fake_github.issue_labels(1) == ("state:running",)  # 标签暂未收敛


def test_completion_labels_mapping_matches_sqlite_semantics():
    assert COMPLETION_LABELS[COMPLETED] == "state:in-review"
    assert COMPLETION_LABELS[FAILED] == "state:blocked"
    assert COMPLETION_LABELS["cancelled"] == "state:todo"
