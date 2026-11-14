"""TASK-07 调度侧：分派登记原子幂等（唯一键）+ 状态回写。

auto-dispatch / QA 触发的重复投递必须只登记一次；这里用 N 个真实线程各自独立
连接同时登记同一个 dispatch_key，验证只有一个 True。
"""

from __future__ import annotations

import threading

import pytest

from scheduler import SchedulerError, SchedulerService


def test_d01_register_dispatch_idempotent(svc):
    assert svc.register_dispatch("repo#42@sha-abc", issue_number=42, merge_sha="sha-abc") is True
    assert svc.register_dispatch("repo#42@sha-abc", issue_number=42, merge_sha="sha-abc") is False
    record = svc.dispatch_state("repo#42@sha-abc")
    assert record is not None
    assert record.issue_number == 42
    assert record.merge_sha == "sha-abc"
    assert record.status == "registered"
    assert record.comment_posted is False
    assert svc.dispatch_state("missing") is None


def test_d02_register_dispatch_concurrent_threads_exactly_one(db_path):
    SchedulerService(db_path).close()  # 先建表
    results: list[bool] = []
    barrier = threading.Barrier(8, timeout=20)

    def worker() -> None:
        service = SchedulerService(db_path)
        try:
            barrier.wait()
            results.append(service.register_dispatch("dispatch-key-1", issue_number=1))
        finally:
            service.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert results.count(True) == 1
    assert results.count(False) == 7
    check = SchedulerService(db_path)
    try:
        assert len(check.store.all("SELECT * FROM dispatches")) == 1
    finally:
        check.close()


def test_d03_set_dispatch_status_and_comment_flag(svc):
    assert svc.register_dispatch("k-1", pr_number=7) is True
    svc.set_dispatch_status("k-1", "assigned", comment_posted=True)
    record = svc.dispatch_state("k-1")
    assert record.status == "assigned"
    assert record.comment_posted is True
    svc.set_dispatch_status("k-1", "done")  # 不改 comment_posted
    assert svc.dispatch_state("k-1").comment_posted is True
    with pytest.raises(SchedulerError):
        svc.set_dispatch_status("missing", "done")


def test_d04_register_dispatch_validates_key(svc):
    with pytest.raises(ValueError):
        svc.register_dispatch("")


def test_d05_register_dispatch_different_keys_all_succeed(svc):
    keys = [f"repo#1@sha-{index}" for index in range(5)]
    assert all(svc.register_dispatch(key) for key in keys)
    assert len(svc.store.all("SELECT * FROM dispatches")) == 5
