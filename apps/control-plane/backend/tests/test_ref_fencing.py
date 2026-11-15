"""PR-2（fencing）：代次校验、续租推进 token、旧 token 写操作全部 StaleFencing。

全部走 fake GitHub server 的真实 refs：断言的「旧 token 失效」来自 `max_token(HWM)
!= presented token` 与 lease ref 是否存在，而不是 mock 返回值。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from scheduler import ref_api
from scheduler.api import (
    CANCEL_REQUESTED,
    CLAIMED,
    RETRYABLE,
    RUNNING,
    InvalidTransition,
    StaleFencing,
)
from scheduler.ref_fencing import RefFencer

AGENT = "muse-2"
INSTANCE = "1a2b3c4d"
ATTEMPT = "a1b2c3d4e5f6"


def fencer(ref_store, ref_transport, clock, **kwargs) -> RefFencer:
    return RefFencer(ref_store, ref_transport, repo="o/r", clock=clock, **kwargs)


def claim(authority, *, agent=AGENT, instance=INSTANCE, attempt=ATTEMPT, ttl=None):
    return authority.claim(
        "issue-1", agent_id=agent, instance_id=instance, attempt_id=attempt, lease_ttl=ttl
    )


def lease_refs(fake_github, issue=1) -> list[str]:
    prefix = f"refs/heads/agent-lease/issue-{issue}/"
    return sorted(ref for ref in fake_github.refs_snapshot() if ref.startswith(prefix))


def hwm_refs(fake_github, issue=1) -> list[str]:
    prefix = f"refs/heads/agent-hwm/issue-{issue}/"
    return sorted(ref for ref in fake_github.refs_snapshot() if ref.startswith(prefix))


def test_renew_advances_token_deletes_old_lease_and_old_token_is_stale(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock)
    first = claim(authority)
    old_ref = lease_refs(fake_github)[0]
    renewed = authority.renew("issue-1", attempt_id=ATTEMPT, fencing_token=first.fencing_token)
    assert renewed.fencing_token == 2
    refs = lease_refs(fake_github)
    assert len(refs) == 1 and refs[0] != old_ref
    assert ref_api.parse_lease(refs[0]).token == 2
    for call in (
        lambda: authority.renew("issue-1", attempt_id=ATTEMPT, fencing_token=1),
        lambda: authority.transition(
            "issue-1", attempt_id=ATTEMPT, fencing_token=1, state=RUNNING
        ),
        lambda: authority.release("issue-1", attempt_id=ATTEMPT, fencing_token=1),
        lambda: authority.release(
            "issue-1", attempt_id=ATTEMPT, fencing_token=1, terminal_state="failed"
        ),
    ):
        with pytest.raises(StaleFencing):
            call()
    task = authority.transition(
        "issue-1", attempt_id=ATTEMPT, fencing_token=renewed.fencing_token, state=RUNNING
    )
    assert task.state == RUNNING and task.fencing_token == 2


def test_renew_rejects_token_advanced_by_another_instance(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    assert ref_store.create(ref_api.hwm_ref(1, lease.fencing_token + 1), "a" * 40)
    with pytest.raises(StaleFencing):
        authority.renew("issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token)


def test_renew_wrong_attempt_or_missing_lease_is_stale(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    with pytest.raises(StaleFencing):
        authority.renew("issue-1", attempt_id="ffffffffffff", fencing_token=lease.fencing_token)
    # 租约被删（模拟已完成/已释放）后，同 token 续租也必须被拒
    ref_store.delete(lease_refs(fake_github)[0])
    with pytest.raises(StaleFencing):
        authority.renew("issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token)


def test_renew_then_old_token_cannot_do_anything(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock)
    first = claim(authority)
    renewed = authority.renew("issue-1", attempt_id=ATTEMPT, fencing_token=first.fencing_token)
    assert renewed.fencing_token == first.fencing_token + 1
    with pytest.raises(StaleFencing):
        authority.release("issue-1", attempt_id=ATTEMPT, fencing_token=first.fencing_token)
    assert authority.view(1).state == CLAIMED  # 旧 token 的写入没有生效


def test_release_without_terminal_is_retryable_and_reclaimable(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    task = authority.release("issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token)
    assert task.state == RETRYABLE
    assert lease_refs(fake_github) == []
    again = claim(authority, agent="muse-3", instance="9f9f9f9f", attempt="bbbbbbbbbbbb")
    assert again.fencing_token == lease.fencing_token + 1


def test_release_with_terminal_state_converges_labels(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1, labels=("state:running",))
    authority = fencer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    authority.transition("issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
                         state=RUNNING)
    task = authority.release(
        "issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
        terminal_state="failed",
    )
    assert task.state == "failed" and task.terminal_status == "failed"
    assert fake_github.issue_labels(1) == ("state:blocked",)
    with pytest.raises(StaleFencing):
        authority.release("issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token)


def test_transition_state_machine_and_cancel_flow(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    with pytest.raises(InvalidTransition):
        authority.transition("issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
                             state="completed")
    authority.transition("issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token,
                         state=RUNNING)
    assert authority.is_cancel_requested("issue-1") is False
    task = authority.request_cancel("issue-1", reason="用户取消")
    assert task.state == CANCEL_REQUESTED
    assert authority.is_cancel_requested("issue-1") is True
    assert len(lease_refs(fake_github)) == 1  # 等执行者 ack，租约仍在
    acked = authority.ack_cancel("issue-1", attempt_id=ATTEMPT,
                                 fencing_token=lease.fencing_token)
    assert acked.state == "cancelled" and acked.is_terminal
    assert lease_refs(fake_github) == []
    assert authority.is_cancel_requested("issue-1") is False


def test_request_cancel_without_live_lease_cancels_directly(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1, labels=("state:claimed",))
    authority = fencer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    authority.release("issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token)
    task = authority.request_cancel("issue-1", reason="用户取消")
    assert task.state == "cancelled" and task.is_terminal
    assert fake_github.issue_labels(1) == ("state:todo",)


def test_ack_cancel_requires_cancel_requested(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    with pytest.raises(InvalidTransition):
        authority.ack_cancel("issue-1", attempt_id=ATTEMPT, fencing_token=lease.fencing_token)


def test_expired_lease_old_token_fenced_after_takeover(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock, lease_ttl=timedelta(seconds=60))
    first = claim(authority)
    ref_clock.advance(61)
    second = claim(authority, agent="muse-3", instance="9f9f9f9f", attempt="bbbbbbbbbbbb")
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(StaleFencing):
        authority.renew("issue-1", attempt_id=ATTEMPT, fencing_token=first.fencing_token)
    with pytest.raises(StaleFencing):
        authority.release("issue-1", attempt_id=ATTEMPT, fencing_token=first.fencing_token)
    assert authority.view(1).attempt_id == "bbbbbbbbbbbb"


def test_hwm_monotonic_across_release_and_reclaim(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock)
    tokens = []
    for index in range(3):
        lease = claim(authority, attempt=f"{index:012x}")
        tokens.append(lease.fencing_token)
        authority.release("issue-1", attempt_id=lease.attempt_id,
                          fencing_token=lease.fencing_token)
    assert tokens == [1, 2, 3]
    assert hwm_refs(fake_github) == [
        "refs/heads/agent-hwm/issue-1/00000001",
        "refs/heads/agent-hwm/issue-1/00000002",
        "refs/heads/agent-hwm/issue-1/00000003",
    ]

def test_release_from_blocked_lands_queued_and_removes_lease_ref(
    fake_github, ref_store, ref_transport, ref_clock
):
    """真实缺陷回归（sched-core PR-6 发现）：BLOCKED 释放只能落到 QUEUED，
    QUEUED 也必须删除 lease ref，否则 get_task 派生回 claimed，新实例 LeaseHeld，
    阻塞任务永远无法被重试。"""
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    authority.transition("issue-1", attempt_id=ATTEMPT,
                         fencing_token=lease.fencing_token, state=RUNNING)
    authority.transition("issue-1", attempt_id=ATTEMPT,
                         fencing_token=lease.fencing_token, state="blocked")
    assert len(lease_refs(fake_github)) == 1  # BLOCKED 期间仍持有租约
    task = authority.release("issue-1", attempt_id=ATTEMPT,
                             fencing_token=lease.fencing_token)
    assert task.state == "queued"
    assert lease_refs(fake_github) == []  # 修复点：QUEUED 也删 lease ref
    again = claim(authority, agent="muse-3", instance="9f9f9f9f", attempt="bbbbbbbbbbbb")
    assert again.fencing_token == lease.fencing_token + 1


def test_transition_from_blocked_to_queued_removes_lease_ref(
    fake_github, ref_store, ref_transport, ref_clock
):
    """transition(BLOCKED→QUEUED) 同样必须收尾租约（与 release 同一缺陷面）。"""
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock)
    lease = claim(authority)
    authority.transition("issue-1", attempt_id=ATTEMPT,
                         fencing_token=lease.fencing_token, state=RUNNING)
    authority.transition("issue-1", attempt_id=ATTEMPT,
                         fencing_token=lease.fencing_token, state="blocked")
    task = authority.transition("issue-1", attempt_id=ATTEMPT,
                                fencing_token=lease.fencing_token, state="queued")
    assert task.state == "queued"
    assert lease_refs(fake_github) == []


def test_old_token_writes_rejected_when_hwm_advanced_but_old_lease_ref_present(
    fake_github, ref_store, ref_transport, ref_clock
):
    """B1/R2 回归：HWM 已推进、但旧 lease ref 尚未删除的窗口必须拦住旧执行者。

    该窗口里 renew 的下一跳 CAS 不会触发；transition/release/ack_cancel 只能靠
    `_require_current` 的 `presented_token == max_token` 校验拦住旧 token。
    Muse 3 变异测试发现原测试对该窗口无效（去掉校验仍全绿），本用例专门覆盖它。
    """
    fake_github.add_issue(1)
    authority = fencer(ref_store, ref_transport, ref_clock)
    first = claim(authority)
    assert first.fencing_token == 1

    # 模拟另一实例/外部把 HWM 推进到 2，但尚未删除旧 lease ref（真实竞态窗口）
    anchor = ref_store.branch_sha("main")
    assert ref_store.create(ref_api.hwm_ref(1, 2), anchor) is True
    assert len(lease_refs(fake_github)) == 1  # 旧 lease ref 仍在
    assert [ref_api.parse_hwm_token(r) for r in hwm_refs(fake_github)] == [1, 2]

    for call in (
        lambda: authority.transition(
            "issue-1", attempt_id=ATTEMPT, fencing_token=1, state=RUNNING
        ),
        lambda: authority.release("issue-1", attempt_id=ATTEMPT, fencing_token=1),
        lambda: authority.release(
            "issue-1", attempt_id=ATTEMPT, fencing_token=1, terminal_state="failed"
        ),
    ):
        with pytest.raises(StaleFencing):
            call()

    # cancel 流程：先请求取消（进入 cancel_requested 状态），旧 token 的 ack 也必须被拒
    authority.request_cancel("issue-1", reason="B1/R2 窗口")
    with pytest.raises(StaleFencing):
        authority.ack_cancel("issue-1", attempt_id=ATTEMPT, fencing_token=1)
