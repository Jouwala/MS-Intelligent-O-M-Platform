"""PR-3：cron Reaper CLI（`dispatch/lease_reaper.py`）测试。

边界（ADR-7 §八 PR-3）：**过期判定 + ref-CAS 回收认领 + 删 lease ref 由权威层核心的
`scheduler/ref_reap.py` 实现**（冻结接口 `reap_expired_leases(store, now_ms, ttl_ms,
*, reaper_instance, dry_run=False) -> list[dict]`）；本 CLI 只负责编排：调 ref_reap →
清孤儿 scope → 可选标签对账 → 幂等/退出码。因此这里主要用**注入的 fake ref_reap 模块**
验证 CLI 的委托、参数、幂等与失败语义；真实 `ref_reap` 落地后由 `pytest.importorskip`
自动启用两条真集成（RestRefStore←HTTP→FakeGitHubServer、两个线程同时回收只有一个赢家）。

全部离线，不访问真实 GitHub。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from dispatch import lease_reaper
from scheduler import ref_api

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
    """ref_api.RefStore 的内存实现（唯一性 = GitHub POST 的 422 语义）。"""

    def __init__(self) -> None:
        self.refs: dict[str, str] = {}
        self.lock = threading.Lock()
        self.delete_calls: list[str] = []

    def create(self, ref: str, sha: str) -> bool:
        with self.lock:
            if ref in self.refs:
                return False
            self.refs[ref] = sha
            return True

    def get(self, ref: str):
        with self.lock:
            sha = self.refs.get(ref)
        return ref_api.RefInfo(ref=ref, sha=sha) if sha is not None else None

    def list(self, prefix: str):
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
        return "a" * 40

    def leases(self) -> list[str]:
        with self.lock:
            return [
                ref for ref in self.refs if ref.startswith(ref_api.LEASE_ROOT + "/")
            ]

    def scopes(self) -> list[str]:
        with self.lock:
            return [
                ref for ref in self.refs if ref.startswith(ref_api.SCOPE_ROOT + "/")
            ]

    def hwm(self) -> list[str]:
        with self.lock:
            return [ref for ref in self.refs if ref.startswith(ref_api.HWM_ROOT + "/")]


def seed_expired_lease(store: FakeRefStore, issue: int, token: int, now: datetime) -> str:
    """建 HWM + 一条已过期 lease ref；返回 lease ref 全名。"""
    store.create(ref_api.hwm_ref(issue, token), store.branch_sha())
    expired_epoch = _ms(now - timedelta(minutes=11))
    ref = ref_api.lease_ref(issue, token, expired_epoch, "muse-2", "a1", "b2")
    store.create(ref, store.branch_sha())
    return ref


def seed_orphan_scope(store: FakeRefStore, issue: int, token: int, path: str = "apps/x.py"):
    store.create(
        ref_api.scope_ref(ref_api.encode_scope_path(path), f"issue-{issue}", token),
        store.branch_sha(),
    )


def _row(ref: str, outcome: str = "reaped") -> dict:
    parsed = ref_api.parse_lease(ref)
    assert parsed is not None
    return {
        "issue_number": parsed.issue_number,
        "token": parsed.token,
        "epoch_ms": parsed.epoch_ms,
        "instance_id": parsed.instance_id,
        "agent_id": parsed.agent_id,
        "ref": ref,
        "outcome": outcome,
    }


class FakeReapModule:
    """按冻结 ref_reap 接口的最小替身：删 reaped 行，记录调用参数。"""

    def __init__(self, expired: list[str], *, skipped=(), error: Exception | None = None):
        self.expired = list(expired)
        self.skipped = [dict(row) for row in skipped]
        self.error = error
        self.calls: list[dict] = []

    def reap_expired_leases(self, ref_store, now_ms, ttl_ms, *, reaper_instance, dry_run):
        self.calls.append(
            {
                "now_ms": now_ms,
                "ttl_ms": ttl_ms,
                "reaper_instance": reaper_instance,
                "dry_run": dry_run,
            }
        )
        if self.error is not None:
            raise self.error
        rows = list(self.skipped)
        for ref in self.expired:
            if not dry_run:
                ref_store.delete(ref)
            rows.append(_row(ref, "reaped"))
        return rows


def _env() -> dict:
    return {"REPO": "o/r", "GH_TOKEN": "t"}


# ---------------------------------------------------------------------------
# 委托 ref_reap：参数 / 幂等 / 失败
# ---------------------------------------------------------------------------


def test_cli_delegates_to_ref_reap_and_cleans_orphan_scopes():
    pytest.importorskip("scheduler.ref_scope")
    store, clock = FakeRefStore(), FakeClock()
    expired = seed_expired_lease(store, 7, 1, clock())
    seed_orphan_scope(store, 7, 1)  # 任务无存活租约 → 孤儿
    fake = FakeReapModule([expired])
    code = lease_reaper.main(
        [], env=_env(), ref_store=store, ref_reap=fake, log=lambda *_a: None
    )
    assert code == 0
    call = fake.calls[0]
    assert call["reaper_instance"] and call["dry_run"] is False
    assert call["ttl_ms"] == ref_api.ttl_ms(lease_reaper.DEFAULT_TTL)
    assert isinstance(call["now_ms"], int) and call["now_ms"] > 0
    assert store.leases() == []  # 过期 lease 已删
    assert store.scopes() == []  # 孤儿 scope 已删（ref_scope.cleanup_orphans）
    assert store.hwm() == [ref_api.hwm_ref(7, 1)]  # HWM 保留


def test_cli_dry_run_does_not_delete_or_claim():
    store, clock = FakeRefStore(), FakeClock()
    expired = seed_expired_lease(store, 7, 1, clock())
    seed_orphan_scope(store, 7, 1)
    fake = FakeReapModule([expired])
    code = lease_reaper.main(
        ["--dry-run"], env=_env(), ref_store=store, ref_reap=fake, log=lambda *_a: None
    )
    assert code == 0
    assert fake.calls[0]["dry_run"] is True
    assert store.leases() == [expired]
    assert len(store.scopes()) == 1


def test_cli_is_idempotent():
    store, clock = FakeRefStore(), FakeClock()
    expired = seed_expired_lease(store, 7, 1, clock())
    first = FakeReapModule([expired])
    assert lease_reaper.main([], env=_env(), ref_store=store, ref_reap=first) == 0
    second = FakeReapModule([])  # 第二次：lease 已删，ref_reap 返回空
    assert lease_reaper.main([], env=_env(), ref_store=store, ref_reap=second) == 0
    assert store.leases() == []


def test_cli_writes_reaped_count_to_github_output(tmp_path):
    """P1-4：本轮释放的任务数写进 $GITHUB_OUTPUT，供 workflow 判断是否触发分派。"""
    import os

    out_file = tmp_path / "github_output.txt"
    env = dict(_env())
    env["GITHUB_OUTPUT"] = str(out_file)
    store, clock = FakeRefStore(), FakeClock()
    expired = seed_expired_lease(store, 7, 1, clock())
    seed_orphan_scope(store, 7, 1)
    fake = FakeReapModule([expired])
    code = lease_reaper.main([], env=env, ref_store=store, ref_reap=fake, log=lambda *_a: None)
    assert code == 0
    content = out_file.read_text(encoding="utf-8")
    # 1 条过期租约 + 1 条孤儿 scope = 2
    assert "reaped=2" in content


def test_cli_writes_zero_when_nothing_reaped(tmp_path):
    """无释放时 reaped=0，workflow 不触发分派（不空跑）。"""
    out_file = tmp_path / "github_output.txt"
    env = dict(_env())
    env["GITHUB_OUTPUT"] = str(out_file)
    store, clock = FakeRefStore(), FakeClock()
    fake = FakeReapModule([])
    code = lease_reaper.main([], env=env, ref_store=store, ref_reap=fake, log=lambda *_a: None)
    assert code == 0
    content = out_file.read_text(encoding="utf-8")
    assert "reaped=0" in content


def test_github_output_write_failure_logs_and_does_not_fail(capsys, tmp_path):
    """P1-4 审核发现 5：GITHUB_OUTPUT 写失败不抛错，但必须留日志。

    否则 reaped 输出为空，下游分派步静默跳过（"" > 0 为假），无从排查。
    """
    env = dict(_env())
    env["GITHUB_OUTPUT"] = str(tmp_path / "no-such-dir" / "github_output.txt")
    lease_reaper._write_github_output("reaped", "2", env)
    captured = capsys.readouterr()
    assert "写 GITHUB_OUTPUT 失败" in captured.out
    assert "reaped=2" in captured.out


def test_cli_failure_returns_nonzero_and_prints_reason(capsys):
    fake = FakeReapModule([], error=RuntimeError("模拟 refs API 500"))
    code = lease_reaper.main([], env=_env(), ref_store=FakeRefStore(), ref_reap=fake)
    assert code == 1
    assert "回收失败" in capsys.readouterr().err


def test_cli_missing_ref_reap_module_is_nonzero(capsys):
    code = lease_reaper.main([], env=_env(), ref_store=FakeRefStore(), ref_reap=object())
    assert code == 2
    assert "ref_reap" in capsys.readouterr().err


def test_cli_missing_env_returns_nonzero(capsys):
    assert lease_reaper.main([], env={}, ref_store=FakeRefStore()) == 2
    assert "REPO" in capsys.readouterr().err


def test_cli_missing_token_returns_nonzero(capsys):
    assert (
        lease_reaper.main([], env={"REPO": "o/r"}, ref_store=FakeRefStore()) == 2
    )
    assert "GH_TOKEN" in capsys.readouterr().err


def test_cli_invalid_ttl_env_returns_nonzero(capsys):
    env = {**_env(), lease_reaper.ENV_TTL_SECONDS: "abc"}
    assert lease_reaper.main([], env=env, ref_store=FakeRefStore()) == 2
    assert "正整数" in capsys.readouterr().err


def test_cli_reports_skipped_rows(capsys):
    """ref_reap 返回 skipped（未过期/被别的 Reaper 抢先）时 CLI 只统计，不重复删除。"""
    store, clock = FakeRefStore(), FakeClock()
    live = seed_expired_lease(store, 8, 1, clock())
    fake = FakeReapModule([], skipped=[_row(live, "skipped")])
    code = lease_reaper.main(
        [], env=_env(), ref_store=store, ref_reap=fake, log=lambda msg: print(msg)
    )
    assert code == 0
    assert "跳过 1 条" in capsys.readouterr().out
    assert store.leases() == [live]  # skipped 的 lease 未被 CLI 删除


# ---------------------------------------------------------------------------
# 可选标签对账（展示层，失败只告警）
# ---------------------------------------------------------------------------


class FakeLabelAdapter:
    def __init__(self, labels: list[str], comments: list[str] | None = None) -> None:
        self.labels = list(labels)
        self.comments = list(comments or [])
        self.fail = False

    def list_comment_bodies(self, issue_number: int) -> list[str]:
        return list(self.comments)

    def get_labels(self, issue_number: int) -> list[str]:
        return list(self.labels)

    def set_labels(self, issue_number: int, labels: list[str]) -> None:
        if self.fail:
            raise RuntimeError("模拟改标签失败")
        self.labels = list(labels)


def test_fix_labels_converges_claimed_to_todo():
    store, clock = FakeRefStore(), FakeClock()
    expired = seed_expired_lease(store, 7, 1, clock())
    adapter = FakeLabelAdapter(["state:claimed", "agent:muse-2"])
    report = lease_reaper.reap(
        store,
        label_adapter=adapter,
        clock=clock,
        fix_labels=True,
        ref_reap=FakeReapModule([expired]),
        log=lambda *_a: None,
    )
    assert report["label_warnings"] == []
    assert adapter.labels == ["agent:muse-2", "state:todo"]


def test_fix_labels_skips_completed_and_warns_on_failure():
    completed = FakeLabelAdapter(["state:claimed"], ["<!-- ref-completion-id: k1 -->"])
    assert lease_reaper.repair_labels(completed, [7], log=lambda *_a: None) == []
    assert completed.labels == ["state:claimed"]  # 已完成任务不动标签

    broken = FakeLabelAdapter(["state:claimed"])
    broken.fail = True
    warnings = lease_reaper.repair_labels(broken, [8], log=lambda *_a: None)
    assert warnings and "issue #8" in warnings[0]


# ---------------------------------------------------------------------------
# 真实 ref_reap 的集成（ref_reap.py 落地后自动启用）
# ---------------------------------------------------------------------------


def test_real_ref_reap_over_http_end_to_end():
    """RestRefStore ← HTTP → FakeGitHubServer；真实 ref_reap 删过期租约 + HWM 保留。"""
    pytest.importorskip("scheduler.ref_reap", reason="PR-3 的 ref_reap.py 尚未落地")
    pytest.importorskip("scheduler.factory", reason="make_ref_store 由 PR-5 提供")
    from fake_github_http import FakeGitHubServer
    from scheduler.factory import make_ref_store

    now = datetime.now(timezone.utc)
    with FakeGitHubServer() as srv:
        store = make_ref_store(srv.repo_full, token="t", api_base=srv.base_url)
        sha = store.branch_sha()
        expired = ref_api.lease_ref(
            7, 1, _ms(now - lease_reaper.DEFAULT_TTL - timedelta(minutes=5)),
            "muse-2", "a1", "b2",
        )
        live = ref_api.lease_ref(8, 1, _ms(now), "muse-3", "c3", "d4")
        hwm = ref_api.hwm_ref(7, 1)
        # 孤儿 scope：所属任务（issue-9）没有任何 lease ref → 应被 CLI 清理
        orphan_scope = ref_api.scope_ref(
            ref_api.encode_scope_path("apps/orphan.py"), "issue-9", 1
        )
        for ref in (expired, live, hwm, orphan_scope):
            assert store.create(ref, sha)
        env = {"REPO": srv.repo_full, "GH_TOKEN": "t", "GITHUB_API_URL": srv.base_url}
        assert lease_reaper.main([], env=env, log=lambda *_a: None) == 0
        refs = srv.refs_snapshot()
        assert expired not in refs
        assert live in refs  # 未过期不动
        assert hwm in refs  # HWM 永不删
        assert orphan_scope not in refs  # 孤儿 scope 被 ref_scope.cleanup_orphans 清掉
        assert any(ref.startswith(ref_api.REAP_ROOT + "/") for ref in refs)
        assert lease_reaper.main([], env=env, log=lambda *_a: None) == 0  # 幂等


def test_real_ref_reap_cas_different_instances_only_one_winner():
    """不同 reaper_instance 并发收同一过期租约：CAS 保证恰好 1 个 reaped。

    回收权 ref 按 (issue, token) 全局唯一（Lead 2026-11-14 修正：刻意不含
    reaper_instance），所以两个不同 Reaper 并发时只有一个 create 成功，另一个 422
    放弃；delete 只发生一次，另一次记录 skipped（或它 list 时已被删空）。
    """
    pytest.importorskip("scheduler.ref_reap", reason="PR-3 的 ref_reap.py 尚未落地")
    pytest.importorskip("scheduler.factory", reason="make_ref_store 由 PR-5 提供")
    from fake_github_http import FakeGitHubServer
    from scheduler.factory import make_ref_store

    now = datetime.now(timezone.utc)
    with FakeGitHubServer() as srv:
        store = make_ref_store(srv.repo_full, token="t", api_base=srv.base_url)
        sha = store.branch_sha()
        expired = ref_api.lease_ref(
            7, 1, _ms(now - lease_reaper.DEFAULT_TTL - timedelta(minutes=5)),
            "muse-2", "a1", "b2",
        )
        store.create(ref_api.hwm_ref(7, 1), sha)
        store.create(expired, sha)
        barrier = threading.Barrier(2, timeout=20)
        results: list[tuple[list[dict], tuple[str, ...]]] = []

        def contender(instance: str) -> None:
            barrier.wait()
            results.append(
                lease_reaper.reap_expired_lease_refs(store, reaper_instance=instance)
            )

        threads = [
            threading.Thread(target=contender, args=("aaaa11",)),
            threading.Thread(target=contender, args=("bbbb22",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert expired not in srv.refs_snapshot()
        reaped_total = sum(len(reaped) for _rows, reaped in results)
        assert reaped_total == 1, results  # 跨 instance 也只 1 个赢家
        # 回收权 ref 按 (issue, token) 唯一（不含 instance），且保留作审计
        claim_ref = ref_api.reap_ref(7, 1)
        assert claim_ref in srv.refs_snapshot()


def test_real_ref_reap_loser_422_skips_without_delete():
    """CAS 输家（回收权已存在）：整组记 skipped，不删 lease ref（留给赢家）。"""
    pytest.importorskip("scheduler.ref_reap", reason="PR-3 的 ref_reap.py 尚未落地")
    pytest.importorskip("scheduler.factory", reason="make_ref_store 由 PR-5 提供")
    from fake_github_http import FakeGitHubServer
    from scheduler.factory import make_ref_store

    now = datetime.now(timezone.utc)
    with FakeGitHubServer() as srv:
        store = make_ref_store(srv.repo_full, token="t", api_base=srv.base_url)
        sha = store.branch_sha()
        expired = ref_api.lease_ref(
            7, 1, _ms(now - lease_reaper.DEFAULT_TTL - timedelta(minutes=5)),
            "muse-2", "a1", "b2",
        )
        store.create(ref_api.hwm_ref(7, 1), sha)
        store.create(expired, sha)
        # 模拟「另一个 Reaper 已经抢到回收权」
        assert store.create(ref_api.reap_ref(7, 1), sha) is True

        rows, reaped = lease_reaper.reap_expired_lease_refs(
            store, reaper_instance="cccc33"
        )
        assert reaped == ()
        assert rows and all(row["outcome"] == "skipped" for row in rows)
        assert expired in srv.refs_snapshot()  # 输家不删


def test_missing_ref_scope_degrades_to_skip(monkeypatch, fake_github):
    """PR-4 未合入时（ref_scope 不可用）Reaper 降级为跳过孤儿清理，整体不失败。"""
    from scheduler.github_refs import RestRefStore

    store = RestRefStore(
        fake_github.repo_full,
        token="test-token",
        api_base=fake_github.base_url,
        max_retries=1,
        sleep=lambda _s: None,
    )
    monkeypatch.setattr(lease_reaper, "_load_cleanup_orphans", lambda: None)
    report = lease_reaper.reap(store, reaper_instance="reaper-test", log=lambda _m: None)
    assert report["orphan_scope_cleanup"] == "skipped"
    assert report["orphan_scopes"] == ()
