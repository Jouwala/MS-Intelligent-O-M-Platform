"""PR-1（认领）并发：多线程 + 两个真实子进程连同一 fake GitHub，只允许一个赢家。

为什么一定要真实子进程：多 VM 部署时每台机器各跑一个 Worker 进程，「同进程多线程」
共享内存/GIL 可能掩盖竞态；这里让两个独立操作系统进程通过 TCP 打同一个 fake
GitHub server，服务端在锁内裁决 ref 唯一性（与真实 GitHub 语义一致）。

另含 HWM CAS 已知交错窗口的**确定性复现**（见最后一个用例）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from scheduler import ref_api
from scheduler.api import LeaseHeld, StaleFencing
from scheduler.github_refs import RestRefStore
from scheduler.ref_claim import RefClaimer

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[3]
RACE_HELPER = TESTS_DIR / "_ref_race_helper.py"


def _authority(base_url: str, clock, *, agent="muse-2", store=None) -> RefClaimer:
    import github_issue as gi  # conftest 已把适配器模块放进 sys.modules

    store = store or RestRefStore(
        "o/r", token="test-token", api_base=base_url, max_retries=1, sleep=lambda _s: None
    )
    transport = gi.RestGitHubTransport(
        "o", "r", token="test-token", api_base=base_url, max_retries=1, sleep=lambda _s: None
    )
    return RefClaimer(store, transport, repo="o/r", clock=clock)


def _live(store: RestRefStore, issue: int = 1, *, now_ms: int = 0) -> list[ref_api.LeaseRef]:
    if not now_ms:
        import datetime as dt

        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    parsed = [
        ref_api.parse_lease(info.ref)
        for info in store.list(ref_api.lease_prefix(issue))
    ]
    return [
        lease
        for lease in parsed
        if lease is not None and lease.is_live(now_ms, 10 * 60 * 1000)
    ]


def test_threads_claim_same_task_exactly_one_wins(fake_github, ref_clock):
    fake_github.add_issue(1)
    winners: list[tuple[int, object]] = []
    losers: list[int] = []
    barrier = threading.Barrier(8, timeout=30)

    def worker(index: int) -> None:
        authority = _authority(fake_github.base_url, ref_clock)
        barrier.wait()
        try:
            lease = authority.claim(
                "issue-1",
                agent_id="muse-2",
                instance_id=f"{index:08x}",
                attempt_id=f"{index:012x}",
            )
            winners.append((index, lease))
        except LeaseHeld:
            losers.append(index)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in threads)

    assert len(winners) == 1, f"恰好一个赢家，实际 {winners}"
    assert len(losers) == 7
    winner_instance = f"{winners[0][0]:08x}"
    # 真实 refs 状态：只有一条存活租约，且属于赢家
    store = RestRefStore(
        "o/r", token="test-token", api_base=fake_github.base_url, max_retries=1,
        sleep=lambda _s: None,
    )
    live = _live(store, now_ms=int(ref_clock().timestamp() * 1000))
    assert len(live) == 1, f"只应有一条存活租约：{live}"
    assert live[0].instance_id == winner_instance
    assert live[0].token == winners[0][1].fencing_token


def test_two_processes_claim_same_task_exactly_one_wins(fake_github):
    fake_github.add_issue(1)
    start_at = time.time() + 1.0
    commands = []
    for index in (1, 2):
        commands.append(
            [
                sys.executable,
                str(RACE_HELPER),
                fake_github.base_url,
                "issue-1",
                "muse-2",
                f"{index:08x}",
                f"{index:012x}",
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
        stdout, stderr = proc.communicate(timeout=120)
        assert proc.returncode == 0, f"helper 失败：{stderr}"
        lines = [line for line in stdout.splitlines() if line.strip()]
        assert lines, f"helper 无输出：{stderr}"
        results.append(json.loads(lines[-1]))

    ok = [item for item in results if item["ok"]]
    held = [item for item in results if not item["ok"]]
    assert len(ok) == 1, f"多进程恰好一个成功，实际 {results}"
    assert len(held) == 1 and held[0]["error"] == "LeaseHeld"
    # 真实 refs 状态：一条存活租约（属于赢家），HWM 与租约的 token 一致
    store = RestRefStore(
        "o/r", token="test-token", api_base=fake_github.base_url, max_retries=1,
        sleep=lambda _s: None,
    )
    live = _live(store)
    assert len(live) == 1
    assert live[0].instance_id == ok[0]["instance"]
    assert live[0].token == ok[0]["token"]


class _BeforeLeaseCreate:
    """在赢家 A「建自己的 lease ref」之前插入另一个进程 B 的完整认领（确定性交错）。"""

    def __init__(self, inner, on_prefix: str, hook):
        self.inner = inner
        self.on_prefix = on_prefix
        self.hook = hook
        self.fired = False

    def create(self, ref: str, sha: str) -> bool:
        if not self.fired and ref.startswith(self.on_prefix):
            self.fired = True
            self.hook()
        return self.inner.create(ref, sha)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def test_interleaving_winner_lease_gap_never_yields_two_winners(fake_github, ref_clock):
    """ADR-7 §3.3 未覆盖窗口的确定性复现：

    A 赢得 HWM token1 但**还没建 lease ref** 时，B 读到「HWM 前进、无租约」，抢到
    token2 并成功返回；A 随后建 lease 时复核到 B 的存活租约 → 删除自己租约并
    LeaseHeld。最终只有一个赢家（B）、一条存活租约。
    """
    fake_github.add_issue(1)
    inner_store = RestRefStore(
        "o/r", token="test-token", api_base=fake_github.base_url, max_retries=1,
        sleep=lambda _s: None,
    )
    outcome: dict[str, object] = {}

    def run_b() -> None:
        authority_b = _authority(fake_github.base_url, ref_clock)
        try:
            outcome["b"] = authority_b.claim(
                "issue-1", agent_id="muse-2", instance_id="bbbbbbbb", attempt_id="b0b0b0b0b0b0"
            )
        except Exception as exc:  # noqa: BLE001 - 断言用
            outcome["b_error"] = type(exc).__name__

    hooked = _BeforeLeaseCreate(
        inner_store, ref_api.lease_prefix(1, 1), run_b
    )
    authority_a = _authority(fake_github.base_url, ref_clock, store=hooked)
    with pytest.raises(LeaseHeld):
        authority_a.claim(
            "issue-1", agent_id="muse-2", instance_id="aaaaaaaa", attempt_id="a0a0a0a0a0a0"
        )
    assert "b" in outcome, f"B 必须先成功（{outcome}）"
    lease_b = outcome["b"]
    live = _live(inner_store, now_ms=int(ref_clock().timestamp() * 1000))
    assert len(live) == 1 and live[0].instance_id == "bbbbbbbb"
    assert live[0].token == lease_b.fencing_token
    # A 的 lease ref 已被自己删除（没有任何残留的第二把租约）
    assert len(
        [ref for ref in fake_github.refs_snapshot() if "/agent-lease/" in ref]
    ) == 1
