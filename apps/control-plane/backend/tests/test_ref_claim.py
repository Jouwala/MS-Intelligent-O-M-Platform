"""PR-1（认领）：HWM CAS 认领，只依赖 `ref_claim.RefClaimer` + fake GitHub server。

覆盖：唯一赢家、同/异实例冲突、422 输家重读（存活→LeaseHeld / 过期→更高 token）、
身份校验不白烧 token、过期租约接管、HWM 单调不复用、终态任务不再认领。
断言落在服务端真实 refs 与评论上（`fake_github.refs_snapshot()`）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from scheduler import ref_api
from scheduler.api import (
    CLAIMED,
    RETRYABLE,
    AlreadyTerminal,
    LeaseHeld,
    SchedulerError,
)
from scheduler.ref_claim import RefClaimer
from scheduler.ref_view import state_comment_body

AGENT = "muse-2"
INSTANCE = "1a2b3c4d"
ATTEMPT = "a1b2c3d4e5f6"


def claimer(ref_store, ref_transport, clock, **kwargs) -> RefClaimer:
    return RefClaimer(ref_store, ref_transport, repo="o/r", clock=clock, **kwargs)


def claim(authority: RefClaimer, task_id="issue-1", *, agent=AGENT, instance=INSTANCE,
          attempt=ATTEMPT, ttl=None):
    return authority.claim(
        task_id, agent_id=agent, instance_id=instance, attempt_id=attempt, lease_ttl=ttl
    )


def hwm_refs(fake_github, issue=1) -> list[str]:
    prefix = f"refs/heads/agent-hwm/issue-{issue}/"
    return sorted(ref for ref in fake_github.refs_snapshot() if ref.startswith(prefix))


def lease_refs(fake_github, issue=1) -> list[str]:
    prefix = f"refs/heads/agent-lease/issue-{issue}/"
    return sorted(ref for ref in fake_github.refs_snapshot() if ref.startswith(prefix))


class RaceWinnerStore:
    """第一次创建指定 HWM ref 时先真实写入「并发赢家」状态，再返回 False（422 语义）。"""

    def __init__(self, inner, *, target_ref: str, winner_lease: str = "", winner_only_hwm=False):
        self.inner = inner
        self.target_ref = target_ref
        self.winner_lease = winner_lease
        self.winner_only_hwm = winner_only_hwm
        self.armed = True

    def create(self, ref: str, sha: str) -> bool:
        if self.armed and ref == self.target_ref:
            self.armed = False
            self.inner.create(ref, sha)  # 赢家的 HWM（真实创建）
            if not self.winner_only_hwm and self.winner_lease:
                self.inner.create(self.winner_lease, sha)  # 赢家的租约（真实创建）
            return False
        return self.inner.create(ref, sha)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def test_claim_creates_hwm_and_lease_refs(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = claimer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    assert lease.fencing_token == 1
    assert lease.task_id == "issue-1"
    assert hwm_refs(fake_github) == ["refs/heads/agent-hwm/issue-1/00000001"]
    refs = lease_refs(fake_github)
    assert len(refs) == 1
    parsed = ref_api.parse_lease(refs[0])
    assert parsed is not None
    assert (parsed.token, parsed.agent_id, parsed.instance_id, parsed.attempt_id) == (
        1, AGENT, INSTANCE, ATTEMPT
    )
    view = authority.view(1, task_id="issue-1")
    assert view.state == CLAIMED and view.max_token == 1
    assert fake_github.comment_bodies(1)  # 认领审计评论


def test_claim_other_instance_is_lease_held(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    first = claimer(ref_store, ref_transport, ref_clock)
    claim(first)
    second = claimer(ref_store, ref_transport, ref_clock)
    with pytest.raises(LeaseHeld) as info:
        claim(second, instance="9f9f9f9f", attempt="bbbbbbbbbbbb")
    assert info.value.instance == INSTANCE
    assert info.value.holder == AGENT
    assert len(lease_refs(fake_github)) == 1  # 没有第二把租约


def test_same_instance_reclaim_bumps_token(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = claimer(ref_store, ref_transport, ref_clock)
    first = claim(authority)
    second = claim(authority, attempt="cccccccccccc")
    assert second.fencing_token == first.fencing_token + 1
    assert len(hwm_refs(fake_github)) == 2


def test_loser_422_rereads_live_winner_and_backs_off(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    now_ms = int(ref_clock().timestamp() * 1000)
    winner_lease = ref_api.lease_ref(1, 1, now_ms, "muse-9", "9f9f9f9f", "9999aaaa")
    racy = RaceWinnerStore(ref_store, target_ref=ref_api.hwm_ref(1, 1), winner_lease=winner_lease)
    with pytest.raises(LeaseHeld) as info:
        claim(claimer(racy, ref_transport, ref_clock))
    assert info.value.instance == "9f9f9f9f"
    assert hwm_refs(fake_github) == ["refs/heads/agent-hwm/issue-1/00000001"]
    assert lease_refs(fake_github) == [winner_lease]


def test_loser_422_rereads_expired_winner_then_wins_higher_token(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    stale_ms = int(ref_clock().timestamp() * 1000) - 10 * 60 * 1000
    stale_lease = ref_api.lease_ref(1, 1, stale_ms, "muse-9", "9f9f9f9f", "9999aaaa")
    racy = RaceWinnerStore(ref_store, target_ref=ref_api.hwm_ref(1, 1), winner_lease=stale_lease)
    lease = claim(claimer(racy, ref_transport, ref_clock))
    assert lease.fencing_token == 2
    # HWM 单调：过期赢家的 token1 保留，绝不复用
    assert hwm_refs(fake_github) == [
        "refs/heads/agent-hwm/issue-1/00000001",
        "refs/heads/agent-hwm/issue-1/00000002",
    ]


def test_loser_422_with_no_lease_backs_off_conservatively(
    fake_github, ref_store, ref_transport, ref_clock
):
    """赢家 CAS 成功后、lease 未建成的在途窗口：失败者**保守退让**（下一轮再试），
    不能立刻抢更高 token（那会产生两个赢家）。"""
    fake_github.add_issue(1)
    racy = RaceWinnerStore(
        ref_store, target_ref=ref_api.hwm_ref(1, 1), winner_only_hwm=True
    )
    with pytest.raises(LeaseHeld):
        claim(claimer(racy, ref_transport, ref_clock))
    # 只烧掉赢家的 token1，没有多余的 HWM/租约
    assert hwm_refs(fake_github) == ["refs/heads/agent-hwm/issue-1/00000001"]
    assert lease_refs(fake_github) == []


def test_expired_lease_can_be_reclaimed(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = claimer(ref_store, ref_transport, ref_clock, lease_ttl=timedelta(seconds=60))
    first = claim(authority)
    ref_clock.advance(61)
    second = claim(authority, agent="muse-3", instance="9f9f9f9f", attempt="bbbbbbbbbbbb")
    assert second.fencing_token == first.fencing_token + 1
    assert authority.view(1).state == CLAIMED


def test_hwm_monotonic_never_reused(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = claimer(ref_store, ref_transport, ref_clock)
    tokens = [
        claim(authority, attempt=f"{index:012x}").fencing_token for index in range(3)
    ]
    assert tokens == [1, 2, 3]
    assert hwm_refs(fake_github) == [
        "refs/heads/agent-hwm/issue-1/00000001",
        "refs/heads/agent-hwm/issue-1/00000002",
        "refs/heads/agent-hwm/issue-1/00000003",
    ]
    # 每代只保留最后一条 lease ref（旧代在认领时被清理）
    assert len(lease_refs(fake_github)) == 1
    assert ref_api.parse_lease(lease_refs(fake_github)[0]).token == 3


def test_claim_rejects_unsafe_identity_without_burning_token(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    authority = claimer(ref_store, ref_transport, ref_clock)
    with pytest.raises(ValueError):
        claim(authority, attempt="not-hex")
    with pytest.raises(ValueError):
        claim(authority, agent="Muse Two")
    assert hwm_refs(fake_github) == []


def test_claim_terminal_task_is_already_terminal_and_keeps_refs(
    fake_github, ref_store, ref_transport, ref_clock
):
    """已终态（完成标记）不再认领：不新增 HWM token（不白烧代次）。"""
    fake_github.add_issue(1)
    fake_github.seed_ref(ref_api.hwm_ref(1, 1))
    body = ref_api.completion_comment_body(
        issue_number=1,
        token=1,
        attempt_id="9999aaaa",
        terminal_status="completed",
        idempotency_key="old-key",
        summary="历史完成",
        timestamp="2026-11-14T02:00:00Z",
        agent_id="muse-9",
    )
    fake_github.add_comment(1, body)
    before = set(hwm_refs(fake_github))
    with pytest.raises(AlreadyTerminal):
        claim(claimer(ref_store, ref_transport, ref_clock))
    assert set(hwm_refs(fake_github)) == before


def test_claim_treats_cancel_requested_without_lease_as_terminal(
    fake_github, ref_store, ref_transport, ref_clock
):
    """取消请求 + 租约已被清（Reaper 回收）→ 派生 cancelled，认领必须拒绝（不复活）。"""
    fake_github.add_issue(1)
    fake_github.seed_ref(ref_api.hwm_ref(1, 1))
    fake_github.add_comment(
        1,
        state_comment_body(issue_number=1, token=1, state="cancel_requested",
                           timestamp=ref_clock()),
    )
    with pytest.raises(AlreadyTerminal):
        claim(claimer(ref_store, ref_transport, ref_clock))


def test_claim_unavailable_ref_layer_raises_scheduler_error(
    fake_github, ref_store, ref_transport, ref_clock
):
    """refs 创建全部异常（非 422）时不要伪装成 LeaseHeld/成功，显式报错。"""
    fake_github.add_issue(1)
    authority = claimer(ref_store, ref_transport, ref_clock)
    fake_github.fail_next("/git/refs", status=500, times=10, method="POST")
    with pytest.raises((SchedulerError,)):
        claim(authority)
    assert RETRYABLE  # 语义提示：任务仍可被后续重试认领（无副作用）
