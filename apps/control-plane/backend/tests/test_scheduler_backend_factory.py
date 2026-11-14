"""PR-5：GitHub 权威调度器 factory + Worker 接线的小单元测试。

覆盖：
- `make_scheduler` 只返回 `GitHubRefScheduler`（ADR-7 §零：**无 sqlite 回退**）；
- 参数校验：缺 transport / 缺 repo 给出清晰 ValueError；repo 可从 transport 推导；
- 实现层缺失时抛带「PR-5」提示的 RuntimeError（不抛裸 ImportError）；
- fake HTTP 端到端：factory → GitHubRefScheduler → RestRefStore ← HTTP → fake server；
- `task_lifecycle.renew()` 采纳调度器返回值里推进的 `fencing_token`（ADR-7 §3.4）。

`ref_service.py` 未就绪时相关用例 `pytest.importorskip` 跳过并注明；其余用例始终执行。
全部离线，不访问真实 GitHub。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import sched_testkit as tk
from scheduler import StaleFencing, api, factory, ref_api

TESTS_DIR = Path(__file__).resolve().parent
WORKER_DIR = TESTS_DIR.parents[2] / "agent-worker"


def _load_worker_module(name: str):
    """按文件路径加载 apps/agent-worker/<name>.py（目录名带连字符，不能包导入）。"""
    key = f"agent_worker_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, WORKER_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 参数校验 / 构造
# ---------------------------------------------------------------------------


def test_requires_transport():
    with pytest.raises(ValueError) as info:
        factory.make_scheduler()
    assert "transport" in str(info.value)


def test_requires_repo_when_transport_has_none():
    with pytest.raises(ValueError) as info:
        factory.make_scheduler(transport=tk.RecordingTransport())
    assert "repo" in str(info.value)


def test_repo_can_derive_from_transport():
    transport = SimpleNamespace(owner="Jouwala", repo="noc-soc-platform")
    assert factory._repo_from_transport(transport) == "Jouwala/noc-soc-platform"


def test_make_ref_store_rejects_bad_repo():
    with pytest.raises(ValueError) as info:
        factory.make_ref_store("not-a-repo")
    assert "owner/name" in str(info.value)


def test_missing_implementation_gives_actionable_error(monkeypatch):
    """实现层缺失不能抛裸 ImportError：要告诉运维这是 PR-5 的实现层。"""
    monkeypatch.setitem(sys.modules, "scheduler.ref_service", None)
    with pytest.raises(RuntimeError) as info:
        factory.make_scheduler(
            transport=SimpleNamespace(owner="o", repo="r"),
            repo="o/r",
            ref_store=SimpleNamespace(),
        )
    message = str(info.value)
    assert "ref_service.py" in message
    assert "PR-5" in message


def test_returns_github_ref_scheduler(clock):
    """唯一后端：FakeRefStore + 记录型 transport，不联网。"""
    pytest.importorskip("scheduler.ref_service", reason="PR-5 的 ref_service.py 尚未落地")
    from scheduler.ref_service import GitHubRefScheduler

    scheduler = factory.make_scheduler(
        transport=tk.RecordingTransport(),
        repo="o/r",
        clock=clock,
        ref_store=SimpleNamespace(),
    )
    assert isinstance(scheduler, GitHubRefScheduler)
    for name in ("claim", "renew", "acquire_scope", "release_scope", "complete"):
        assert callable(getattr(scheduler, name))


def test_factory_has_no_sqlite_branch():
    """ADR-7 §零：不得保留 sqlite 第二权威。构造参数里没有 backend/db_path。"""
    import inspect

    params = inspect.signature(factory.make_scheduler).parameters
    assert "backend" not in params
    assert "db_path" not in params
    assert "transport" in params and "repo" in params


# ---------------------------------------------------------------------------
# fake HTTP 端到端
# ---------------------------------------------------------------------------


def test_github_backend_end_to_end_over_fake_http(clock):
    """端到端：factory → GitHubRefScheduler → RestRefStore ← HTTP → fake GitHub。

    证明「切换 + 范围锁」拼得起来：认领、重叠范围冲突、释放后另一个任务可拿同范围；
    github 权威层**不写本地数据库**。
    """
    pytest.importorskip("scheduler.ref_service", reason="ref_service.py 尚未落地")
    from fake_github_http import FakeGitHubServer

    with FakeGitHubServer() as srv:
        # transport 用内存实现（评论/标签不是权威，不需过 HTTP）；refs 走真实 HTTP
        transport = tk.RecordingTransport()
        transport.add_issue(1)
        transport.add_issue(2, labels=("agent:muse-3", "state:todo"))
        ref_store = factory.make_ref_store(
            srv.repo_full, token="t", api_base=srv.base_url
        )
        scheduler = factory.make_scheduler(
            transport=transport,
            repo=srv.repo_full,
            clock=clock,
            ref_store=ref_store,
        )
        scheduler.upsert_task("issue-1", 1, title="a", scopes=["apps/shared.py"])
        scheduler.upsert_task("issue-2", 2, title="b", scopes=["apps/shared.py"])
        lease1 = scheduler.claim(
            "issue-1", agent_id="muse-2", instance_id="a1", attempt_id="b2"
        )
        lease2 = scheduler.claim(
            "issue-2", agent_id="muse-3", instance_id="c3", attempt_id="d4"
        )
        scheduler.acquire_scope(
            "issue-1",
            attempt_id="b2",
            fencing_token=lease1.fencing_token,
            scopes=["apps/shared.py"],
        )
        with pytest.raises(api.ScopeConflict):
            scheduler.acquire_scope(
                "issue-2",
                attempt_id="d4",
                fencing_token=lease2.fencing_token,
                scopes=["apps/shared.py"],
            )
        assert set(scheduler.scope_holders()) == {"issue-1"}
        assert scheduler.release_scope(
            "issue-1", attempt_id="b2", fencing_token=lease1.fencing_token
        ) is True
        scheduler.acquire_scope(
            "issue-2",
            attempt_id="d4",
            fencing_token=lease2.fencing_token,
            scopes=["apps/shared.py"],
        )
        assert set(scheduler.scope_holders()) == {"issue-2"}
        # 权威层真实写到了 fake GitHub 的 refs 上（不是本地状态）
        refs = set(srv.refs_snapshot())
        assert any(ref.startswith(ref_api.SCOPE_ROOT + "/") for ref in refs)
        assert any(ref.startswith(ref_api.HWM_ROOT + "/") for ref in refs)


# ---------------------------------------------------------------------------
# Worker 接线：renew 采纳推进后的 fencing_token
# ---------------------------------------------------------------------------


class _FakeRenewScheduler:
    """只实现 renew 的假调度器，返回带新 token 的 Lease。"""

    def __init__(self, next_token: int, *, stale: bool = False) -> None:
        self.next_token = next_token
        self.stale = stale
        self.calls: list[dict] = []

    def renew(self, task_id, *, attempt_id, fencing_token, lease_ttl=None):
        self.calls.append(
            {"task_id": task_id, "token": fencing_token, "ttl": lease_ttl}
        )
        if self.stale:
            raise StaleFencing(task_id, attempt_id, fencing_token, "", 99, "租约已丢")
        return SimpleNamespace(fencing_token=self.next_token)


def _lifecycle(scheduler):
    lifecycle_module = _load_worker_module("task_lifecycle")
    config = SimpleNamespace(lease_ttl_s=600)
    return lifecycle_module, lifecycle_module.TaskLifecycle(
        scheduler, None, config, log=lambda *_a: None
    )


def _active(lifecycle_module, token: int):
    return lifecycle_module.ActiveTask(
        issue=1, task_id="issue-1", slug="task-1", attempt_id="a1",
        fencing_token=token, lease_held=True,
    )


def test_worker_renew_adopts_advanced_token():
    lifecycle_module, lifecycle = _lifecycle(_FakeRenewScheduler(next_token=8))
    active = _active(lifecycle_module, 7)
    assert lifecycle.renew(active, api) is True
    assert active.fencing_token == 8  # 后续 complete/release 必须用新代次
    assert active.lease_held is True


def test_worker_renew_keeps_token_when_backend_does_not_advance():
    """返回同 token 时采纳返回值不改变行为。"""
    lifecycle_module, lifecycle = _lifecycle(_FakeRenewScheduler(next_token=7))
    active = _active(lifecycle_module, 7)
    assert lifecycle.renew(active, api) is True
    assert active.fencing_token == 7


def test_worker_renew_stale_stops_execution():
    lifecycle_module, lifecycle = _lifecycle(_FakeRenewScheduler(0, stale=True))
    active = _active(lifecycle_module, 3)
    assert lifecycle.renew(active, api) is False
    assert active.lease_held is False
    assert active.aborted is True
