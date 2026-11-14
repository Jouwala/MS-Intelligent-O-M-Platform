"""T02：实例级原子认领、fencing、续租、释放、取消（真实 SQLite + 真实并发）。

覆盖验收矩阵里的：同/不同 agent、同 agent 不同 instance、多线程竞争、多进程竞争、
过期后重新认领、旧执行者被 StaleFencing 拒绝、取消状态机。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

import sched_testkit as tk
from scheduler import (
    AlreadyTerminal,
    InvalidTransition,
    LeaseHeld,
    SchedulerService,
    StaleFencing,
    TaskNotFound,
)
from scheduler.api import CANCEL_REQUESTED, CLAIMED, COMPLETED, RETRYABLE, RUNNING

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[3]
RACE_HELPER = TESTS_DIR / "_claim_race_helper.py"


def test_t01_claim_basic_lease_and_attempt(svc, clock):
    svc.upsert_task("issue-1", 1, repo="o/r", title="任务")
    lease = tk.claim(svc)
    assert lease.task_id == "issue-1"
    assert lease.agent_id == "muse-2"
    assert lease.instance_id == "i-1"
    assert lease.attempt_id == "a-1"
    assert lease.fencing_token == 1
    assert lease.is_live(clock())
    task = svc.get_task("issue-1")
    assert task.state == CLAIMED
    assert task.attempt_id == "a-1"
    assert task.fencing_token == 1
    rows = svc.store.all("SELECT * FROM attempts WHERE task_id = ?", ("issue-1",))
    assert len(rows) == 1 and rows[0]["attempt_id"] == "a-1"


def test_t02_threads_same_agent_different_instance_exactly_one_wins(db_path):
    fixed = tk.T0
    SchedulerService(db_path, clock=lambda: fixed).close()  # 先建表，避免并发初始化噪声
    winner, losers = [], []
    barrier = threading.Barrier(8, timeout=20)

    def worker(index: int) -> None:
        service = SchedulerService(db_path, clock=lambda: fixed)
        try:
            barrier.wait()
            try:
                lease = service.claim(
                    "issue-1",
                    agent_id="muse-2",
                    instance_id=f"i-{index}",
                    attempt_id=f"a-{index}",
                )
                winner.append((index, lease))
            except LeaseHeld:
                losers.append(index)
        finally:
            service.close()

    setup = SchedulerService(db_path, clock=lambda: fixed)
    setup.upsert_task("issue-1", 1)
    setup.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)

    assert len(winner) == 1, f"恰好一个成功，实际 {winner}"
    assert len(losers) == 7
    check = SchedulerService(db_path, clock=lambda: fixed)
    try:
        lease = check.store.one("SELECT * FROM leases WHERE task_id = ?", ("issue-1",))
        attempts = check.store.all("SELECT * FROM attempts WHERE task_id = ?", ("issue-1",))
        assert lease is not None
        assert int(lease["fencing_token"]) == 1
        assert [row["attempt_id"] for row in attempts] == [f"a-{winner[0][0]}"]
        audits = check.list_audit("issue-1")
        assert [a["event"] for a in audits].count("claim") == 1
    finally:
        check.close()


def test_t03_processes_file_db_exactly_one_wins(db_path):
    setup = SchedulerService(db_path)
    setup.upsert_task("issue-1", 1)
    setup.close()
    start_at = time.time() + 0.8
    commands = []
    for index in (1, 2):
        commands.append(
            [
                sys.executable,
                str(RACE_HELPER),
                str(db_path),
                "issue-1",
                "muse-2",
                f"proc-{index}",
                f"p-{index}",
                str(start_at),
            ]
        )
    procs = [
        subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
        )
        for command in commands
    ]
    results = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=90)
        assert proc.returncode == 0, f"helper 失败：{stderr}"
        lines = [line for line in stdout.splitlines() if line.strip()]
        assert lines, f"helper 无输出：{stderr}"
        results.append(json.loads(lines[-1]))

    ok = [item for item in results if item["ok"]]
    held = [item for item in results if not item["ok"]]
    assert len(ok) == 1, f"多进程恰好一个成功，实际 {results}"
    assert len(held) == 1 and held[0]["error"] == "LeaseHeld"
    assert ok[0]["token"] == 1

    check = SchedulerService(db_path)
    try:
        lease = check.store.one("SELECT * FROM leases WHERE task_id = ?", ("issue-1",))
        assert lease is not None and lease["instance_id"] == ok[0]["instance"]
        assert len(check.store.all("SELECT * FROM attempts WHERE task_id = ?", ("issue-1",))) == 1
    finally:
        check.close()


def test_t04_same_agent_different_instance_lease_held(svc):
    svc.upsert_task("issue-1", 1)
    tk.claim(svc, instance="i-1", attempt="a-1")
    with pytest.raises(LeaseHeld) as info:
        tk.claim(svc, instance="i-2", attempt="a-2")
    assert info.value.instance == "i-1"
    assert info.value.holder == "muse-2"


def test_t05_different_agent_lease_held(svc):
    svc.upsert_task("issue-1", 1)
    tk.claim(svc, agent="muse-2", instance="i-1", attempt="a-1")
    with pytest.raises(LeaseHeld):
        tk.claim(svc, agent="muse-3", instance="i-9", attempt="a-9")


def test_t06_same_instance_reclaim_bumps_token_and_fences_old_attempt(svc):
    svc.upsert_task("issue-1", 1)
    first = tk.claim(svc, instance="i-1", attempt="a-1")
    second = tk.claim(svc, instance="i-1", attempt="a-2")
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(StaleFencing):
        svc.transition(
            "issue-1",
            attempt_id="a-1",
            fencing_token=first.fencing_token,
            state=RUNNING,
        )


def test_t07_expired_lease_can_be_reclaimed_and_old_worker_fenced(svc, clock):
    svc.upsert_task("issue-1", 1)
    first = tk.claim(svc, instance="i-1", attempt="a-1", lease_ttl=timedelta(seconds=60))
    clock.advance(61)  # 租约过期
    second = tk.claim(svc, agent="muse-3", instance="i-2", attempt="a-2")
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(StaleFencing):
        svc.complete(
            "issue-1",
            attempt_id="a-1",
            fencing_token=first.fencing_token,
            idempotency_key="late",
            summary="迟到完成",
        )
    task = svc.get_task("issue-1")
    assert task.state == CLAIMED and task.attempt_id == "a-2"


def test_t08_claim_terminal_task_rejected(svc):
    lease = tk.prepare_running(svc)
    svc.complete(
        "issue-1",
        attempt_id="a-1",
        fencing_token=lease.fencing_token,
        idempotency_key="k1",
        summary="完成",
    )
    with pytest.raises(AlreadyTerminal):
        tk.claim(svc, attempt="a-2")


def test_t09_claim_unknown_task(svc):
    with pytest.raises(TaskNotFound):
        tk.claim(svc, task_id="issue-404")


def test_t10_renew_requires_current_token_and_extends_lease(svc, clock):
    svc.upsert_task("issue-1", 1)
    lease = tk.claim(svc, lease_ttl=timedelta(seconds=60))
    renewed = svc.renew(
        "issue-1", attempt_id="a-1", fencing_token=lease.fencing_token,
        lease_ttl=timedelta(seconds=120),
    )
    assert renewed.expires_at > lease.expires_at
    with pytest.raises(StaleFencing):
        svc.renew("issue-1", attempt_id="a-1", fencing_token=lease.fencing_token + 1)
    with pytest.raises(StaleFencing):
        svc.renew("issue-1", attempt_id="a-other", fencing_token=lease.fencing_token)


def test_t11_release_without_terminal_goes_retryable(svc):
    svc.upsert_task("issue-1", 1)
    lease = tk.claim(svc)
    task = svc.release("issue-1", attempt_id="a-1", fencing_token=lease.fencing_token)
    assert task.state == RETRYABLE
    assert task.lease_expire_at is None
    assert svc.store.one("SELECT * FROM leases WHERE task_id = ?", ("issue-1",)) is None
    # 释放后可被重新认领，代次继续递增
    again = tk.claim(svc, agent="muse-3", instance="i-9", attempt="a-9")
    assert again.fencing_token == lease.fencing_token + 1


def test_t12_release_with_terminal_state(svc):
    lease = tk.prepare_running(svc)
    task = svc.release(
        "issue-1", attempt_id="a-1", fencing_token=lease.fencing_token, terminal_state="failed"
    )
    assert task.state == "failed" and task.terminal_status == "failed"
    with pytest.raises(StaleFencing):
        svc.release("issue-1", attempt_id="a-1", fencing_token=lease.fencing_token)


def test_t13_release_rejects_stale_token(svc):
    svc.upsert_task("issue-1", 1)
    first = tk.claim(svc, instance="i-1", attempt="a-1")
    tk.claim(svc, instance="i-1", attempt="a-2")  # 同实例重认领，代次 +1
    with pytest.raises(StaleFencing):
        svc.release("issue-1", attempt_id="a-1", fencing_token=first.fencing_token)


def test_t14_transition_invalid_rejected(svc):
    svc.upsert_task("issue-1", 1)
    lease = tk.claim(svc)
    with pytest.raises(InvalidTransition):
        svc.transition(
            "issue-1", attempt_id="a-1", fencing_token=lease.fencing_token, state=COMPLETED
        )
    # claimed → running 合法
    task = svc.transition(
        "issue-1", attempt_id="a-1", fencing_token=lease.fencing_token, state=RUNNING
    )
    assert task.state == RUNNING
    # running → claimed 非法
    with pytest.raises(InvalidTransition):
        svc.transition(
            "issue-1", attempt_id="a-1", fencing_token=lease.fencing_token, state=CLAIMED
        )


def test_t15_transition_failed_releases_lease(svc):
    lease = tk.prepare_running(svc)
    task = svc.transition(
        "issue-1",
        attempt_id="a-1",
        fencing_token=lease.fencing_token,
        state="failed",
        error_code="E_TEST",
        error_message="测试失败",
    )
    assert task.state == "failed"
    assert task.error_code == "E_TEST"
    assert svc.store.one("SELECT * FROM leases WHERE task_id = ?", ("issue-1",)) is None


def test_t16_cancel_request_ack_and_stale_ack(svc):
    svc.upsert_task("issue-1", 1)
    lease = tk.claim(svc)
    task = svc.request_cancel("issue-1", reason="用户取消")
    assert task.state == CANCEL_REQUESTED
    assert svc.is_cancel_requested("issue-1") is True
    with pytest.raises(StaleFencing):
        svc.ack_cancel(
            "issue-1", attempt_id="a-1", fencing_token=lease.fencing_token + 5
        )
    acked = svc.ack_cancel(
        "issue-1", attempt_id="a-1", fencing_token=lease.fencing_token
    )
    assert acked.state == "cancelled" and acked.is_terminal
    assert svc.is_cancel_requested("issue-1") is False


def test_t17_cancel_terminal_is_idempotent(svc):
    lease = tk.prepare_running(svc)
    svc.complete(
        "issue-1",
        attempt_id="a-1",
        fencing_token=lease.fencing_token,
        idempotency_key="k1",
        summary="完成",
    )
    before = svc.get_task("issue-1")
    after = svc.request_cancel("issue-1")
    assert after.state == before.state == COMPLETED


def test_t17b_cancel_requested_expired_reaps_to_cancelled(svc, clock):
    """已请求取消但持有者失联：回收时直接收敛 cancelled，不能被新实例复活。"""
    svc.upsert_task("issue-1", 1)
    tk.claim(svc, lease_ttl=timedelta(seconds=60))
    svc.request_cancel("issue-1", reason="用户取消")
    clock.advance(61)
    report = svc.reap_expired()
    assert report.expired_leases == ("issue-1",)
    assert "issue-1" not in report.retryable
    task = svc.get_task("issue-1")
    assert task.state == "cancelled" and task.is_terminal
    assert svc.store.one("SELECT * FROM leases WHERE task_id = ?", ("issue-1",)) is None
    with pytest.raises(AlreadyTerminal):
        tk.claim(svc, attempt="a-2")


def test_t18_worker_registry_and_staleness(svc, clock):
    worker = svc.register_worker(
        "muse-2", "i-1", hostname="host-a", platform="linux", capabilities=("python", "git")
    )
    assert worker.hostname == "host-a"
    assert svc.heartbeat_worker("muse-2", "i-1") is True
    assert svc.heartbeat_worker("muse-2", "i-missing") is False
    fresh = svc.list_workers(stale_after=timedelta(seconds=90))
    assert [w.instance_id for w in fresh] == ["i-1"]
    assert fresh[0].state == "active"
    clock.advance(3600)
    stale = svc.list_workers(stale_after=timedelta(seconds=90))
    assert stale[0].state == "stale"


def test_t19_audit_records_order_and_limit(svc):
    svc.upsert_task("issue-1", 1)
    lease = tk.claim(svc, task_id="issue-1")
    svc.renew("issue-1", attempt_id="a-1", fencing_token=lease.fencing_token)
    events = [row["event"] for row in svc.list_audit("issue-1")]
    assert events[:3] == ["renew", "claim", "register_task"]
    assert len(svc.list_audit("issue-1", limit=1)) == 1


def test_t20_renew_prevents_reap(svc, clock):
    svc.upsert_task("issue-1", 1)
    lease = tk.claim(svc, lease_ttl=timedelta(seconds=60))
    clock.advance(50)
    svc.renew("issue-1", attempt_id="a-1", fencing_token=lease.fencing_token,
              lease_ttl=timedelta(seconds=60))
    clock.advance(20)  # 距最初认领已 70s，但续租后续期到 110s
    assert svc.reap_expired().expired_leases == ()
    assert svc.get_task("issue-1").state == CLAIMED
