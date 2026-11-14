"""T04：租约绑定的文件范围锁（真实 SQLite、真实线程竞争）。

覆盖：重叠/不重叠、路径规范化（`./`、`..`、反斜杠、大小写、穿越拒绝）、
过期租约的锁自动失效、reap 回收释放锁、旧 token 不能释放新锁、两实例同范围只一个成功。
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

import sched_testkit as tk
from scheduler import (
    SchedulerService,
    ScopeConflict,
    StaleFencing,
)
from scheduler.scopes import normalize_scope, normalize_scopes, scopes_overlap


def _running(svc, task_id, *, issue_number, agent, instance, scopes=()):
    svc.upsert_task(task_id, issue_number, scopes=scopes)
    lease = tk.claim(svc, task_id, agent=agent, instance=instance, attempt=f"{task_id}-a")
    return lease


def _acquire(svc, task_id, lease, scopes):
    svc.acquire_scope(
        task_id,
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
        scopes=scopes,
    )


def test_s01_non_overlapping_scopes_both_allowed(svc):
    first = _running(svc, "issue-1", issue_number=1, agent="muse-2", instance="i-1")
    second = _running(svc, "issue-2", issue_number=2, agent="muse-3", instance="i-2")
    _acquire(svc, "issue-1", first, ["apps/a/"])
    _acquire(svc, "issue-2", second, ["apps/b/"])
    holders = svc.scope_holders()
    assert holders["issue-1"] == ("muse-2", ("apps/a",))
    assert holders["issue-2"] == ("muse-3", ("apps/b",))


def test_s02_overlapping_scope_conflict(svc):
    first = _running(svc, "issue-1", issue_number=1, agent="muse-2", instance="i-1")
    second = _running(svc, "issue-2", issue_number=2, agent="muse-3", instance="i-2")
    _acquire(svc, "issue-1", first, ["apps/control-plane/backend/scheduler/"])
    with pytest.raises(ScopeConflict) as info:
        _acquire(
            svc,
            "issue-2",
            second,
            ["apps/control-plane/backend/scheduler/service.py"],
        )
    assert info.value.holder_task_id == "issue-1"
    assert info.value.holder_agent == "muse-2"
    assert info.value.overlap


def test_s03_path_normalization_recognizes_overlap(svc):
    first = _running(svc, "issue-1", issue_number=1, agent="muse-2", instance="i-1")
    second = _running(svc, "issue-2", issue_number=2, agent="muse-3", instance="i-2")
    _acquire(svc, "issue-1", first, ["./apps//worker/../worker/worker.py"])
    with pytest.raises(ScopeConflict):
        _acquire(svc, "issue-2", second, ["apps\\worker\\worker.py"])


def test_s04_case_sensitive_by_default_but_configurable(svc, db_path):
    first = _running(svc, "issue-1", issue_number=1, agent="muse-2", instance="i-1")
    svc.upsert_task("issue-2", 2)
    second = tk.claim(svc, "issue-2", agent="muse-3", instance="i-2", attempt="issue-2-a")
    _acquire(svc, "issue-1", first, ["Apps/Worker/WORKER.py"])
    # ADR-4：默认大小写敏感，不同大小写不视为同一文件
    _acquire(svc, "issue-2", second, ["apps/worker/worker.py"])
    assert svc.release_scope(
        "issue-1", attempt_id=first.attempt_id, fencing_token=first.fencing_token
    )
    # 显式开启大小写不敏感策略（Windows 共享盘场景）则判定为冲突
    insensitive = SchedulerService(db_path, clock=lambda: tk.T0, case_insensitive_scopes=True)
    try:
        insensitive.upsert_task("issue-3", 3)
        third = tk.claim(insensitive, "issue-3", agent="muse-4", instance="i-3", attempt="a-3")
        insensitive.acquire_scope(
            "issue-3", attempt_id="a-3", fencing_token=third.fencing_token,
            scopes=["lib/Shared/Util.py"],
        )
        insensitive.upsert_task("issue-4", 4)
        fourth = tk.claim(insensitive, "issue-4", agent="muse-5", instance="i-4", attempt="a-4")
        with pytest.raises(ScopeConflict):
            insensitive.acquire_scope(
                "issue-4", attempt_id="a-4", fencing_token=fourth.fencing_token,
                scopes=["LIB/shared/util.py"],
            )
    finally:
        insensitive.close()


def test_s05_path_traversal_and_absolute_rejected(svc):
    lease = _running(svc, "issue-1", issue_number=1, agent="muse-2", instance="i-1")
    for bad in ("../etc/passwd", "apps/../../secret", "/etc/passwd", "C:/Windows/x"):
        with pytest.raises(ValueError):
            _acquire(svc, "issue-1", lease, [bad])
    assert normalize_scope("apps/./worker/worker.py") == "apps/worker/worker.py"
    # 默认大小写敏感；显式 casefold=True 时才合并大小写
    assert normalize_scopes(["a/b", "A/B", "./a/b/"]) == ("a/b", "A/B")
    assert normalize_scopes(["a/b", "A/B", "./a/b/"], casefold=True) == ("a/b",)
    assert scopes_overlap(["a/b"], ["a/b/c"]) == ["a/b"]


def test_s06_expired_lease_locks_auto_invalid_without_reap(svc, clock):
    first = _running(svc, "issue-1", issue_number=1, agent="muse-2", instance="i-1")
    _acquire(svc, "issue-1", first, ["apps/shared.py"])
    assert svc.scope_holders()  # 有效期内可见
    clock.advance(11 * 60)  # 默认租约 10 分钟，已过期
    assert svc.scope_holders() == {}  # 未 reap 也不再算有效锁
    svc.upsert_task("issue-2", 2)
    second = tk.claim(svc, "issue-2", agent="muse-3", instance="i-2", attempt="issue-2-a")
    _acquire(svc, "issue-2", second, ["apps/shared.py"])  # 不冲突：旧锁随过期租约失效


def test_s07_reap_releases_locks_and_is_idempotent(svc, clock):
    first = _running(svc, "issue-1", issue_number=1, agent="muse-2", instance="i-1")
    _acquire(svc, "issue-1", first, ["apps/shared.py"])
    clock.advance(11 * 60)
    report = svc.reap_expired()
    assert report.expired_leases == ("issue-1",)
    assert "issue-1" in report.retryable
    assert "issue-1-a" in report.orphan_attempts
    assert svc.store.one("SELECT * FROM scope_locks WHERE task_id = ?", ("issue-1",)) is None
    task = svc.get_task("issue-1")
    assert task.state in ("expired", "retryable", "queued")
    assert svc.reap_expired() == type(report)()  # 幂等：第二次没有可回收项
    events = [row["event"] for row in svc.list_audit("issue-1")]
    assert events.count("reap_expired") == 1


def test_s08_old_token_cannot_release_new_lock(svc):
    svc.upsert_task("issue-1", 1)
    first = tk.claim(svc, "issue-1", instance="i-1", attempt="a-1")
    _acquire(svc, "issue-1", first, ["apps/shared.py"])
    second = tk.claim(svc, "issue-1", instance="i-1", attempt="a-2")  # 同实例重认领
    _acquire(svc, "issue-1", second, ["apps/shared.py"])  # 新租约重新申请锁
    with pytest.raises(StaleFencing):
        svc.release_scope("issue-1", attempt_id="a-1", fencing_token=first.fencing_token)
    assert svc.release_scope(
        "issue-1", attempt_id="a-2", fencing_token=second.fencing_token
    ) is True  # 新锁还在，自己可以释放
    assert svc.scope_holders() == {}


def test_s09_two_instances_same_scope_only_one_wins(db_path):
    fixed = tk.T0
    setup = SchedulerService(db_path, clock=lambda: fixed)
    setup.upsert_task("issue-1", 1)
    setup.upsert_task("issue-2", 2)
    tk.claim(setup, "issue-1", agent="muse-2", instance="i-1", attempt="a-1")
    tk.claim(setup, "issue-2", agent="muse-3", instance="i-2", attempt="a-2")
    setup.close()

    results: list[str] = []
    barrier = threading.Barrier(2, timeout=20)

    def worker(task_id: str, agent: str, instance: str, attempt: str) -> None:
        service = SchedulerService(db_path, clock=lambda: fixed)
        try:
            barrier.wait()
            try:
                service.acquire_scope(
                    task_id,
                    attempt_id=attempt,
                    fencing_token=1,
                    scopes=["apps/shared/file.py"],
                )
                results.append(f"ok:{task_id}")
            except ScopeConflict:
                results.append(f"conflict:{task_id}")
        finally:
            service.close()

    threads = [
        threading.Thread(target=worker, args=("issue-1", "muse-2", "i-1", "a-1")),
        threading.Thread(target=worker, args=("issue-2", "muse-3", "i-2", "a-2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(results) == ["conflict:issue-2", "ok:issue-1"] or sorted(results) == [
        "conflict:issue-1",
        "ok:issue-2",
    ]
    check = SchedulerService(db_path, clock=lambda: fixed)
    try:
        assert len(check.scope_holders()) == 1
    finally:
        check.close()


def test_s10_release_scope_allows_other_task(svc):
    first = _running(svc, "issue-1", issue_number=1, agent="muse-2", instance="i-1")
    second = _running(svc, "issue-2", issue_number=2, agent="muse-3", instance="i-2")
    _acquire(svc, "issue-1", first, ["apps/shared.py"])
    assert svc.release_scope(
        "issue-1", attempt_id="issue-1-a", fencing_token=first.fencing_token
    )
    _acquire(svc, "issue-2", second, ["apps/shared.py"])  # 释放后可获取


def test_s11_complete_releases_scope_lock(svc):
    first = _running(svc, "issue-1", issue_number=1, agent="muse-2", instance="i-1")
    _acquire(svc, "issue-1", first, ["apps/shared.py"])
    svc.transition(
        "issue-1", attempt_id="issue-1-a", fencing_token=first.fencing_token, state="running"
    )
    svc.complete(
        "issue-1",
        attempt_id="issue-1-a",
        fencing_token=first.fencing_token,
        idempotency_key="k1",
        summary="完成",
    )
    assert svc.scope_holders() == {}
    assert svc.store.one("SELECT * FROM scope_locks WHERE task_id = ?", ("issue-1",)) is None


def test_s12_acquire_scope_requires_unexpired_lease(svc, clock):
    svc.upsert_task("issue-1", 1)
    lease = tk.claim(svc, lease_ttl=timedelta(seconds=30))
    clock.advance(31)
    with pytest.raises(StaleFencing):
        svc.acquire_scope(
            "issue-1", attempt_id="a-1", fencing_token=lease.fencing_token, scopes=["apps/x.py"]
        )
