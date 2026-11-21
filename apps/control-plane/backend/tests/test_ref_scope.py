"""PR-4 / ADR-7 §四：ref 权威层的文件范围锁（内存 FakeRefStore + 真实线程竞争）。

对应验收矩阵（ADR-7 §九「在新层上重跑」）：T11（租约过期后范围锁可回收）、
T12（两 Worker 抢重叠范围只一个成功）；另覆盖：

- 不重叠并发都可获取；重叠时**创建后校验**让并发者至少一个退让（只 1 个成功）；
- 冲突回退：失败者删除自己刚建的全部 ref，不留半把锁；
- 祖先 / 后代路径重叠双向识别；兄弟目录不算重叠；
- 代次：旧 token 不能释放新锁、过期租约不能申请锁；
- 续租推进 token 后，用新 token 释放能把旧 token 名字的锁一起清掉；
- 孤儿 scope 清理（持有者无存活租约）、dry-run、幂等；
- scope_holders 与 `api.Scheduler.scope_holders()` 同口径（过期租约的锁不返回）。

全部离线：FakeRefStore 在内存里**强制 ref 唯一性**（模拟 GitHub POST 的 422 语义），
时钟可注入；不访问真实 GitHub。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from scheduler import api, ref_api, ref_scope

T0 = datetime(2026, 11, 14, 2, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


class FakeRefStore:
    """ref_api.RefStore 的内存实现：强制唯一性（create 撞名返回 False=422）。"""

    def __init__(self, sha: str = "a" * 40) -> None:
        self.refs: dict[str, str] = {}
        self.sha = sha
        self.lock = threading.Lock()
        self.create_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.fail_create: set[str] = set()
        self.fail_list = False

    # -- RefStore 协议 ------------------------------------------------------
    def create(self, ref: str, sha: str) -> bool:
        with self.lock:
            self.create_calls.append(ref)
            if ref in self.fail_create:
                raise RuntimeError(f"模拟创建失败：{ref}")
            if ref in self.refs:
                return False
            self.refs[ref] = sha or self.sha
            return True

    def get(self, ref: str):
        with self.lock:
            sha = self.refs.get(ref)
        return ref_api.RefInfo(ref=ref, sha=sha) if sha is not None else None

    def list(self, prefix: str):
        if self.fail_list:
            raise RuntimeError("模拟 list 失败")
        with self.lock:
            return [
                ref_api.RefInfo(ref=ref, sha=sha)
                for ref, sha in sorted(self.refs.items())
                if ref.startswith(prefix)
            ]

    def delete(self, ref: str) -> bool:
        with self.lock:
            self.delete_calls.append(ref)
            if ref in self.refs:
                del self.refs[ref]
                return True
            return False

    def branch_sha(self, name: str = "main") -> str:
        return self.sha

    # -- 断言辅助 -----------------------------------------------------------
    def scope_refs(self) -> list[str]:
        with self.lock:
            return [ref for ref in self.refs if ref.startswith(ref_api.SCOPE_ROOT + "/")]

    def hwm_refs(self) -> list[str]:
        with self.lock:
            return [ref for ref in self.refs if ref.startswith(ref_api.HWM_ROOT + "/")]

    def lease_refs(self) -> list[str]:
        with self.lock:
            return [ref for ref in self.refs if ref.startswith(ref_api.LEASE_ROOT + "/")]


def seed_claim(
    store: FakeRefStore,
    clock: FakeClock,
    issue: int,
    token: int = 1,
    *,
    agent: str = "muse-2",
    instance: str = "a1",
    attempt: str = "b2",
) -> int:
    """模拟 claim 的结果：HWM token + 该 token 的 lease ref（epoch=now）。"""
    store.create(ref_api.hwm_ref(issue, token), store.sha)
    epoch = _ms(clock()) if isinstance(clock, FakeClock) else int(clock)
    store.create(
        ref_api.lease_ref(issue, token, epoch, agent, instance, attempt), store.sha
    )
    return epoch


def release_claim(store: FakeRefStore, issue: int, token: int) -> None:
    for item in store.list(ref_api.lease_prefix(issue, token)):
        store.delete(item.ref)


# ---------------------------------------------------------------------------
# 获取：不重叠 / 重叠 / 回退
# ---------------------------------------------------------------------------


def test_non_overlapping_scopes_both_acquired():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1, agent="muse-2")
    seed_claim(store, clock, 2, agent="muse-3")
    ref_scope.acquire_locks(store, None, 1, 1, ["apps/a"], clock=clock)
    ref_scope.acquire_locks(store, None, 2, 1, ["apps/b"], clock=clock)
    holders = ref_scope.scope_holders(store, clock=clock)
    assert holders["issue-1"] == ("muse-2", ("apps/a",))
    assert holders["issue-2"] == ("muse-3", ("apps/b",))


def test_overlapping_scope_conflict_rolls_back_all_refs():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    seed_claim(store, clock, 2, agent="muse-3")
    ref_scope.acquire_locks(store, None, 1, 1, ["apps/shared"], clock=clock)
    with pytest.raises(api.ScopeConflict) as info:
        ref_scope.acquire_locks(
            store, None, 2, 1, ["apps/shared/service.py", "apps/other.py"], clock=clock
        )
    conflict = info.value
    assert conflict.task_id == "issue-2"
    assert conflict.holder_task_id == "issue-1"
    assert conflict.holder_agent == "muse-2"
    assert conflict.overlap
    # 创建后校验失败必须删除**自己刚建的全部** ref（含不重叠那条），不留半把锁
    assert not [ref for ref in store.scope_refs() if "issue-2-" in ref]
    assert len([ref for ref in store.scope_refs() if "issue-1-" in ref]) == 1


def test_ancestor_holder_blocks_descendant_and_vice_versa():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    seed_claim(store, clock, 2, agent="muse-3")
    ref_scope.acquire_locks(store, None, 1, 1, ["apps/a"], clock=clock)
    # 后代路径必须发现祖先持有者
    with pytest.raises(api.ScopeConflict):
        ref_scope.acquire_locks(store, None, 2, 1, ["apps/a/b/c.py"], clock=clock)

    store2, clock2 = FakeRefStore(), FakeClock()
    seed_claim(store2, clock2, 1)
    seed_claim(store2, clock2, 2, agent="muse-3")
    ref_scope.acquire_locks(store2, None, 1, 1, ["apps/a/b/c.py"], clock=clock2)
    # 祖先路径必须发现后代持有者
    with pytest.raises(api.ScopeConflict):
        ref_scope.acquire_locks(store2, None, 2, 1, ["apps/a"], clock=clock2)


def test_sibling_paths_do_not_overlap():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    seed_claim(store, clock, 2, agent="muse-3")
    ref_scope.acquire_locks(store, None, 1, 1, ["apps/a/x.py"], clock=clock)
    # 祖先前缀会顺带列出兄弟目录的 ref，但重叠判定要精确排除
    ref_scope.acquire_locks(store, None, 2, 1, ["apps/b/y.py"], clock=clock)
    assert len(ref_scope.scope_holders(store, clock=clock)) == 2


def test_stale_holder_lease_does_not_block():
    """持有者租约已过期（未 reap）时，新任务可以直接获取（与 SQLite 同口径）。"""
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    ref_scope.acquire_locks(store, None, 1, 1, ["apps/shared.py"], clock=clock)
    clock.advance(11 * 60)  # 默认 TTL 10 分钟
    seed_claim(store, clock, 2, agent="muse-3")
    ref_scope.acquire_locks(store, None, 2, 1, ["apps/shared.py"], clock=clock)
    holders = ref_scope.scope_holders(store, clock=clock)
    assert "issue-1" not in holders  # 过期锁不算有效持有
    assert "issue-2" in holders


def test_path_normalization_detects_overlap():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    seed_claim(store, clock, 2, agent="muse-3")
    ref_scope.acquire_locks(store, None, 1, 1, ["./apps//worker/../worker/w.py"], clock=clock)
    with pytest.raises(api.ScopeConflict):
        ref_scope.acquire_locks(store, None, 2, 1, ["apps\\worker\\w.py"], clock=clock)


def test_invalid_and_empty_scopes_rejected():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    with pytest.raises(ValueError):
        ref_scope.acquire_locks(store, None, 1, 1, [], clock=clock)
    for bad in ("../etc/passwd", "apps/../../x", "/etc/passwd", "C:/Windows/x"):
        with pytest.raises(ValueError):
            ref_scope.acquire_locks(store, None, 1, 1, [bad], clock=clock)
    assert store.scope_refs() == []


# ---------------------------------------------------------------------------
# 代次 / 租约
# ---------------------------------------------------------------------------


def test_acquire_requires_live_lease():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    clock.advance(601)  # 超过默认 600s TTL
    with pytest.raises(api.StaleFencing):
        ref_scope.acquire_locks(store, None, 1, 1, ["apps/x.py"], clock=clock)
    assert store.scope_refs() == []


def test_acquire_rejects_old_token():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1, token=2)  # 当前 HWM = 2
    with pytest.raises(api.StaleFencing):
        ref_scope.acquire_locks(store, None, 1, 1, ["apps/x.py"], clock=clock)


def test_old_token_cannot_release_new_lock():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1, token=1)
    ref_scope.acquire_locks(store, None, 1, 1, ["apps/shared.py"], clock=clock)
    # 新赢家推进到 token=2（同一任务由另一实例重新认领）
    seed_claim(store, clock, 1, token=2, agent="muse-3", instance="c3", attempt="d4")
    with pytest.raises(api.StaleFencing):
        ref_scope.release_locks(store, None, issue_number=1, token=1, clock=clock)
    assert any("issue-1" in ref for ref in store.scope_refs())  # 新代次的锁还在


def test_release_after_renew_cleans_old_token_refs():
    """续租推进 token 后，scope ref 仍是旧 token 名字；新 token 释放要能清掉它。"""
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1, token=1)
    ref_scope.acquire_locks(store, None, 1, 1, ["apps/shared.py"], clock=clock)
    release_claim(store, 1, 1)  # renew：删旧 lease ref、建新 token 的 HWM+lease
    seed_claim(store, clock, 1, token=2, attempt="e5")
    removed = ref_scope.release_locks(store, None, issue_number=1, token=2, clock=clock)
    assert removed == 1
    assert store.scope_refs() == []
    # 幂等：再释放一次返回 0
    assert ref_scope.release_locks(store, None, issue_number=1, token=2) == 0


def test_release_only_touches_own_task():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    seed_claim(store, clock, 2, agent="muse-3")
    ref_scope.acquire_locks(store, None, 1, 1, ["apps/a.py"], clock=clock)
    ref_scope.acquire_locks(store, None, 2, 1, ["apps/b.py"], clock=clock)
    assert ref_scope.release_locks(store, None, issue_number=1, token=1) == 1
    remaining = store.scope_refs()
    assert len(remaining) == 1 and "issue-2-" in remaining[0]


# ---------------------------------------------------------------------------
# 并发：重叠只 1 个成功；不重叠都可成功
# ---------------------------------------------------------------------------


def test_concurrent_overlapping_only_one_winner():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    seed_claim(store, clock, 2, agent="muse-3")
    barrier = threading.Barrier(2, timeout=20)
    results: dict[int, str] = {}

    def contender(issue: int) -> None:
        barrier.wait()
        for _ in range(200):
            try:
                ref_scope.acquire_locks(store, None, issue, 1, ["apps/shared.py"], clock=clock)
                results[issue] = "ok"
                return
            except api.ScopeConflict:
                time.sleep(0.001)
        results[issue] = "conflict"

    threads = [
        threading.Thread(target=contender, args=(1,)),
        threading.Thread(target=contender, args=(2,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    winners = [issue for issue, outcome in results.items() if outcome == "ok"]
    assert len(winners) == 1, results
    # 赢家的锁真实存在，失败者没有留下半把锁
    holder_tasks = set(ref_scope.scope_holders(store, clock=clock))
    assert holder_tasks == {f"issue-{winners[0]}"}
    assert len(store.scope_refs()) == 1


def test_concurrent_non_overlapping_both_win():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    seed_claim(store, clock, 2, agent="muse-3")
    barrier = threading.Barrier(2, timeout=20)
    outcomes: list[str] = []

    def contender(issue: int, path: str) -> None:
        barrier.wait()
        ref_scope.acquire_locks(store, None, issue, 1, [path], clock=clock)
        outcomes.append(f"ok:{issue}")

    threads = [
        threading.Thread(target=contender, args=(1, "apps/a/x.py")),
        threading.Thread(target=contender, args=(2, "apps/b/y.py")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ["ok:1", "ok:2"]
    assert len(ref_scope.scope_holders(store, clock=clock)) == 2


# ---------------------------------------------------------------------------
# 孤儿清理（Reaper）
# ---------------------------------------------------------------------------


def test_cleanup_orphans_removes_only_dead_holder_and_keeps_hwm():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    seed_claim(store, clock, 2, agent="muse-3")
    ref_scope.acquire_locks(store, None, 1, 1, ["apps/live.py"], clock=clock)
    ref_scope.acquire_locks(store, None, 2, 1, ["apps/dead.py"], clock=clock)
    hwm_before = set(store.hwm_refs())
    clock.advance(11 * 60)  # 两个租约都过期
    # 只给 issue-1 续一个新的存活租约（模拟它还在跑）
    release_claim(store, 1, 1)
    seed_claim(store, clock, 1, token=2, agent="muse-2", attempt="f6")

    removed = ref_scope.cleanup_orphans(store, clock=clock)
    assert len(removed) == 1 and "issue-2-" in removed[0]
    assert any("issue-1-" in ref for ref in store.scope_refs())
    assert hwm_before <= set(store.hwm_refs())  # HWM 永不删（新代次只会追加）
    assert ref_scope.cleanup_orphans(store, clock=clock) == []  # 幂等


def test_cleanup_orphans_dry_run_keeps_refs():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    ref_scope.acquire_locks(store, None, 1, 1, ["apps/x.py"], clock=clock)
    clock.advance(11 * 60)
    pending = ref_scope.cleanup_orphans(store, clock=clock, dry_run=True)
    assert len(pending) == 1
    assert store.scope_refs() == pending  # 未实际删除


def test_scope_holders_excludes_expired_lease():
    store, clock = FakeRefStore(), FakeClock()
    seed_claim(store, clock, 1)
    ref_scope.acquire_locks(store, None, 1, 1, ["apps/x.py"], clock=clock)
    assert "issue-1" in ref_scope.scope_holders(store, clock=clock)
    clock.advance(11 * 60)
    assert ref_scope.scope_holders(store, clock=clock) == {}
