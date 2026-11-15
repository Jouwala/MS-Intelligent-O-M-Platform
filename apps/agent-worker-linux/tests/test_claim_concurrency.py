"""ref-CAS 并发认领验收（Issue #73 第 1 条）：并发同一 Issue 只能一个赢家。"""

from __future__ import annotations

import threading

from _linux_fixtures import (  # noqa: F401 - pytest 夹具（含依赖链）
    fake_github,
    make_config,
    make_worker,
    runtime,
)
from _linux_testkit import make_body


def _refs(server, prefix: str) -> list[str]:
    return [ref for ref in server.refs_snapshot() if ref.startswith(prefix)]


def _prepare_issue(server, number: int = 1, *, agent="muse-1-linux") -> None:
    server.add_issue(
        number,
        labels=(f"agent:{agent}", "state:todo"),
        title="并发认领验收",
        body=make_body(command=["true"]),
    )


def test_two_workers_concurrent_claim_exactly_one_wins(fake_github, make_worker):
    _prepare_issue(fake_github)
    worker_a = make_worker("muse-1-linux")
    worker_b = make_worker("muse-2-linux")
    barrier = threading.Barrier(2, timeout=30)
    results: dict[str, dict] = {}

    def probe(name: str, worker) -> None:
        barrier.wait()
        results[name] = worker.probe_claim(1)

    threads = [
        threading.Thread(target=probe, args=("A", worker_a)),
        threading.Thread(target=probe, args=("B", worker_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in threads)

    outcomes = sorted(payload["result"] for payload in results.values())
    assert outcomes == ["LeaseHeld", "claimed"], results
    winner = next(payload for payload in results.values() if payload["result"] == "claimed")
    loser = next(payload for payload in results.values() if payload["result"] == "LeaseHeld")

    # 真实 fake GitHub 上的 refs：恰好 1 条 HWM + 1 条 lease（没有任何第二把租约）
    hwm = _refs(fake_github, "refs/heads/agent-hwm/issue-1/")
    lease = _refs(fake_github, "refs/heads/agent-lease/issue-1/")
    assert len(hwm) == 1, hwm
    assert len(lease) == 1, lease
    assert hwm[0] in winner["hwm_refs"]
    assert lease[0] in winner["lease_refs"]
    assert winner["token"] == 1
    assert loser["refs_error"] == ""
    assert loser["token"] is None  # 输家没有拿到执行权/代次

    # 赢家身份与 lease ref 一致（instance 已按契约映射成十六进制）
    assert winner["ref_instance"] in lease[0]
    assert winner["worker_id"] in (worker_a.config.worker_id, worker_b.config.worker_id)


def test_sequential_second_claim_is_lease_held_then_release_cleans_ref(
    fake_github, make_worker
):
    _prepare_issue(fake_github)
    winner_worker = make_worker("muse-1-linux")
    other_worker = make_worker("muse-2-linux")

    won = winner_worker.probe_claim(1)
    assert won["result"] == "claimed"
    held = other_worker.probe_claim(1)
    assert held["result"] == "LeaseHeld"

    # 主动释放赢家租约后，lease ref 必须消失（HWM 只增不删）
    winner_worker.scheduler.release(
        "issue-1",
        attempt_id=won["attempt"],
        fencing_token=int(won["token"]),
    )
    assert _refs(fake_github, "refs/heads/agent-lease/issue-1/") == []
    assert len(_refs(fake_github, "refs/heads/agent-hwm/issue-1/")) == 1
