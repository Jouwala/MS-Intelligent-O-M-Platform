"""PR-3（回收）：过期判定 + 回收权 CAS（两个 Reaper 只有一个赢家）+ HWM 永不删。

断言落在 fake server 真实 refs 上：只有赢家删除 lease ref，输家记录 `outcome="skipped"`，
`agent-reap/...` 回收权 ref 只创建一个，HWM 全部保留（token 不复用）。
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from scheduler import ref_api
from scheduler.github_refs import RestRefStore
from scheduler.ref_claim import RefClaimer
from scheduler.ref_reap import (
    RefReapError,
    reap_claim_refs,
    reap_expired_leases,
    reaped_records,
)

AGENT = "muse-2"
INSTANCE = "1a2b3c4d"
ATTEMPT = "a1b2c3d4e5f6"
TTL_MS = 60_000


def claimer(ref_store, ref_transport, clock) -> RefClaimer:
    return RefClaimer(ref_store, ref_transport, repo="o/r", clock=clock)


def claim(authority, ttl=timedelta(seconds=60)):
    return authority.claim(
        "issue-1", agent_id=AGENT, instance_id=INSTANCE, attempt_id=ATTEMPT, lease_ttl=ttl
    )


def now_ms(clock) -> int:
    return int(clock().timestamp() * 1000)


def lease_refs(fake_github, issue=1) -> list[str]:
    prefix = f"refs/heads/agent-lease/issue-{issue}/"
    return sorted(ref for ref in fake_github.refs_snapshot() if ref.startswith(prefix))


def hwm_refs(fake_github, issue=1) -> list[str]:
    prefix = f"refs/heads/agent-hwm/issue-{issue}/"
    return sorted(ref for ref in fake_github.refs_snapshot() if ref.startswith(prefix))


def reap_refs(fake_github) -> list[str]:
    prefix = ref_api.REAP_ROOT + "/"
    return sorted(ref for ref in fake_github.refs_snapshot() if ref.startswith(prefix))


def test_reap_expired_deletes_lease_keeps_hwm_and_records_fields(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    claim(claimer(ref_store, ref_transport, ref_clock))
    at = now_ms(ref_clock) + TTL_MS + 1
    records = reap_expired_leases(ref_store, at, TTL_MS, reaper_instance="reaper-1")
    assert len(records) == 1
    record = records[0]
    assert record["issue_number"] == 1 and record["token"] == 1
    assert record["outcome"] == "reaped"
    assert record["ref"].startswith("refs/heads/agent-lease/issue-1/")
    assert record["instance_id"] == INSTANCE and record["agent_id"] == AGENT
    assert record["epoch_ms"] > 0
    assert lease_refs(fake_github) == []  # 过期 lease ref 被删
    assert hwm_refs(fake_github) == ["refs/heads/agent-hwm/issue-1/00000001"]  # HWM 保留
    expected_claim = ref_api.reap_ref(1, 1)
    assert reap_refs(fake_github) == [expected_claim]
    assert reap_claim_refs(ref_store) == [expected_claim]


def test_reap_leaves_live_leases_untouched(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    claim(claimer(ref_store, ref_transport, ref_clock))
    records = reap_expired_leases(
        ref_store, now_ms(ref_clock) + 1000, TTL_MS, reaper_instance="reaper-1"
    )
    assert records == []
    assert len(lease_refs(fake_github)) == 1
    assert reap_refs(fake_github) == []


def test_reap_is_idempotent(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    claim(claimer(ref_store, ref_transport, ref_clock))
    at = now_ms(ref_clock) + TTL_MS + 1
    assert len(reaped_records(reap_expired_leases(ref_store, at, TTL_MS, reaper_instance="r-1"))) == 1
    # 第二次：lease 已删，没有候选
    assert reap_expired_leases(ref_store, at, TTL_MS, reaper_instance="r-1") == []


def test_dry_run_reports_candidates_without_creating_or_deleting(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(1)
    claim(claimer(ref_store, ref_transport, ref_clock))
    at = now_ms(ref_clock) + TTL_MS + 1
    records = reap_expired_leases(
        ref_store, at, TTL_MS, reaper_instance="r-1", dry_run=True
    )
    assert len(records) == 1 and records[0]["outcome"] == "skipped"
    assert len(lease_refs(fake_github)) == 1  # 没删
    assert reap_refs(fake_github) == []  # 没创建回收权 ref


def test_reap_requires_safe_reaper_instance(ref_store):
    with pytest.raises(RefReapError):
        reap_expired_leases(ref_store, 0, TTL_MS, reaper_instance="Bad Instance")
    with pytest.raises(RefReapError):
        reap_expired_leases(ref_store, 0, TTL_MS, reaper_instance="")


def test_reap_refs_unavailable_raises_not_silent(fake_github, ref_store):
    fake_github.fail_next("/git/matching-refs", status=500, times=10)
    with pytest.raises(RefReapError):
        reap_expired_leases(ref_store, 0, TTL_MS, reaper_instance="r-1")


def test_reap_groups_multiple_lease_refs_of_same_token(
    fake_github, ref_store, ref_transport, ref_clock
):
    """同一 (issue, token) 可能残留多条 lease ref（历史/并发替身）：赢家一并删除。"""
    fake_github.add_issue(1)
    fake_github.seed_ref(ref_api.hwm_ref(1, 1))
    base_ms = now_ms(ref_clock)
    older = ref_api.lease_ref(1, 1, base_ms - 120_000, "muse-2", "11111111", "a1b2c3d4e5f6")
    newer = ref_api.lease_ref(1, 1, base_ms - 90_000, "muse-2", "22222222", "b1b2c3d4e5f6")
    fake_github.seed_ref(older)
    fake_github.seed_ref(newer)
    records = reap_expired_leases(ref_store, base_ms, TTL_MS, reaper_instance="r-1")
    assert len(records) == 2 and {record["outcome"] for record in records} == {"reaped"}
    assert lease_refs(fake_github) == []
    assert reap_refs(fake_github) == [ref_api.reap_ref(1, 1)]


class _BarrierOnReapCreate:
    """两个 Reaper 都列出过期租约后，同时抢同一个全局回收权 ref（真 CAS）。"""

    def __init__(self, inner, barrier: threading.Barrier):
        self.inner = inner
        self.barrier = barrier
        self.fired = False

    def create(self, ref: str, sha: str) -> bool:
        if ref.startswith(ref_api.REAP_ROOT + "/") and not self.fired:
            self.fired = True
            self.barrier.wait(timeout=30)  # 双方都到齐后同时 create，只有一个 201
        return self.inner.create(ref, sha)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def test_two_reapers_race_only_one_wins_claim(
    fake_github, ref_store, ref_transport, ref_clock
):
    """回收权 CAS：两个 Reaper 同时处理同一过期租约，只有一个真正删除。"""
    fake_github.add_issue(1)
    claim(claimer(ref_store, ref_transport, ref_clock))
    at = now_ms(ref_clock) + TTL_MS + 1
    barrier = threading.Barrier(2, timeout=30)
    results: dict[str, list] = {}
    errors: list[str] = []

    def run(name: str) -> None:
        store = _BarrierOnReapCreate(
            RestRefStore(
                "o/r", token="test-token", api_base=fake_github.base_url,
                max_retries=1, sleep=lambda _s: None,
            ),
            barrier,
        )
        try:
            results[name] = reap_expired_leases(
                store, at, TTL_MS, reaper_instance=name
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc!r}")

    threads = [threading.Thread(target=run, args=(f"reaper-{i}",)) for i in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors, errors
    winners = [
        name for name, records in results.items() if reaped_records(records)
    ]
    losers = [name for name, records in results.items() if not reaped_records(records)]
    assert len(winners) == 1, f"回收权只应一个赢家：{results}"
    assert len(losers) == 1
    assert all(record["outcome"] == "skipped" for record in results[losers[0]])
    assert lease_refs(fake_github) == []
    # 只创建了一个回收权 ref（赢家的），输家没有删除/回写
    assert len(reap_refs(fake_github)) == 1
    assert reaped_records(results[winners[0]])[0]["ref"].startswith("refs/heads/agent-lease/")


def test_reap_multiple_issues_sorted(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    fake_github.add_issue(2)
    authority = claimer(ref_store, ref_transport, ref_clock)
    claim(authority)
    lease2 = authority.claim(
        "issue-2", agent_id=AGENT, instance_id=INSTANCE, attempt_id="c0c0c0c0c0c0"
    )
    assert lease2.fencing_token == 1
    at = now_ms(ref_clock) + TTL_MS + 1
    records = reap_expired_leases(ref_store, at, TTL_MS, reaper_instance="r-1")
    assert [(r["issue_number"], r["token"]) for r in records] == [(1, 1), (2, 1)]
    assert all(r["outcome"] == "reaped" for r in records)
