"""真实 GitHub 原生权威层（GitHubRefScheduler + ref-CAS）的端到端集成。

PR-6 迁移：SQLite 权威实现已删除，这里改用
`factory.make_scheduler` → `GitHubRefScheduler` → `RestRefStore` ← HTTP →
`fake_github_http.FakeGitHubServer`（本地真实 TCP，服务端裁决 ref 唯一性），
验证 Worker 的认领 → 范围锁 → 执行 → 幂等完成 → 标签收敛整条链路。

覆盖：
1. 端到端完成 + 幂等重放（同一 attempt/幂等键不产生第二条完成评论）；
2. T23：崩溃/租约过期 → Reaper 回收 → 新实例重新认领（token 前进），
   旧执行者迟到 complete 被 StaleFencing 拒绝。

全部离线：只连本机 fake GitHub server，不访问真实网络。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import ex, gi, wk

BODY = (
    "## 目标\n写结果。\n## 改动文件\n- `result.txt`\n"
    "## 执行命令\n```json\n[\"python\", \"-c\", \"print('ok')\"]\n```\n"
)

_TESTS_DIR = Path(__file__).resolve().parent
# _TESTS_DIR.parents[0] = apps/agent-worker，[1] = apps
_CP_TESTS = _TESTS_DIR.parents[1] / "control-plane" / "backend" / "tests"


def _load_fake_github_http():
    """加载控制面测试目录下的 fake GitHub HTTP server（复用已导入模块）。"""
    name = "fake_github_http"
    if name in sys.modules:
        return sys.modules[name]
    path_text = str(_CP_TESTS)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)
    spec = importlib.util.spec_from_file_location(name, _CP_TESTS / "fake_github_http.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _runner_ok(path):
    (path / "result.txt").write_text("ok\n", encoding="utf-8")
    return 0


@pytest.fixture()
def ref_env(clock):
    """真实 ref 权威层 + 本地 fake GitHub HTTP server。"""
    sched = wk.scheduler_module()
    fake_http = _load_fake_github_http()
    with fake_http.FakeGitHubServer() as server:
        ref_store = sched.factory.make_ref_store(
            server.repo_full,
            token="test-token",
            api_base=server.base_url,
            sleep=lambda _seconds: None,
        )
        transport = gi.RestGitHubTransport(
            server.owner,
            server.repo,
            token="test-token",
            api_base=server.base_url,
            max_retries=1,
            sleep=lambda _seconds: None,
        )
        scheduler = sched.factory.make_scheduler(
            transport=transport,
            repo=server.repo_full,
            clock=clock,
            lease_ttl=timedelta(minutes=10),
            ref_store=ref_store,
        )
        yield server, transport, scheduler, sched


def _make_worker(transport, git_repo, clock, scheduler, *, runner=None, **overrides):
    """全部依赖可注入的 Worker（与 tests/conftest.py 的 make_worker 同构，但不带 mirror：
    GitHub 权威层由 GitHubRefScheduler 自己写评论/标签，没有本地 outbox）。"""
    agent_id = overrides.pop("agent_id", "muse-2")
    adapter = gi.GitHubIssueAdapter(
        transport, agent_id, clock=clock, lease_ttl=timedelta(hours=4)
    )
    cfg_kwargs = dict(
        agent_id=agent_id,
        repo="o/r",
        poll_interval_s=5,
        recovery_backoff_s=10,
        recovery_budget_s=900,
        scope_wait_timeout_s=120,
        cancel_poll_interval_s=0.02,
        lease_renew_interval_s=10**6,
    )
    cfg_kwargs.update(overrides)
    executor = ex.Executor(term_grace_s=0.2, kill_wait_s=2.0, poll_interval_s=0.02)
    return wk.AgentWorker(
        adapter,
        wk.WorktreeAllocator(git_repo),
        executor,
        wk.WorkerConfig(**cfg_kwargs),
        scheduler,
        clock=clock,
        sleep=lambda seconds: clock.advance(seconds),
        runner=runner,
    )


def test_github_ref_scheduler_end_to_end(clock, git_repo, ref_env):
    server, transport, scheduler, sched = ref_env
    server.add_issue(70, labels=("agent:muse-2", "state:todo"), body=BODY)
    worker = _make_worker(transport, git_repo, clock, scheduler, runner=_runner_ok)

    worker.register()
    workers = scheduler.list_workers()
    assert [(w.agent_id, w.instance_id) for w in workers] == [
        ("muse-2", worker.config.instance_id)
    ]
    assert workers[0].platform  # 平台标签来自 hostinfo，不得为空

    assert worker.run_once() == "completed"

    snap = scheduler.get_task("issue-70")
    assert snap.state == "completed"
    assert snap.attempt_id  # 真实 claim 写入了 attempt
    assert snap.lease_expire_at is None  # 终态不再持有租约

    # 完成评论 = 权威幂等标记；标签收敛到 state:in-review
    completed = [body for body in server.comment_bodies(70) if "TASK_COMPLETED" in body]
    assert len(completed) == 1 and "ref-completion-id" in completed[0]
    labels = server.issue_labels(70)
    assert "state:in-review" in labels and "state:todo" not in labels

    # 同一 attempt/幂等键重复 complete：created=False，无第二条评论
    replay = scheduler.complete(
        "issue-70",
        attempt_id=snap.attempt_id,
        fencing_token=snap.fencing_token,
        idempotency_key=snap.attempt_id,
        summary="重复回传",
    )
    assert replay.created is False
    assert len([b for b in server.comment_bodies(70) if "TASK_COMPLETED" in b]) == 1


def test_github_ref_scheduler_expired_lease_reaped_then_reclaimed(clock, git_repo, ref_env):
    """T23：崩溃/租约过期 → Reaper 回收 → 新实例重新认领，旧执行者被 fencing 拒绝。"""
    _server, _transport, scheduler, sched = ref_env
    _server.add_issue(72, labels=("agent:muse-2", "state:todo"), body=BODY)
    scheduler.upsert_task(
        "issue-72", 72, repo=_server.repo_full, title="崩溃任务", scopes=["result.txt"]
    )
    crashed = scheduler.claim(
        "issue-72", agent_id="muse-2", instance_id="a1", attempt_id="1111"
    )
    assert scheduler.get_task("issue-72").state == sched.CLAIMED

    clock.advance(11 * 60)  # 超过 10 分钟租约 TTL
    report = scheduler.reap_expired()
    assert "issue-72" in report.expired_leases
    assert "issue-72" in report.retryable
    assert scheduler.get_task("issue-72").lease_expire_at is None

    # 新实例重新认领：token 单调前进
    fresh = scheduler.claim(
        "issue-72", agent_id="muse-2", instance_id="b2", attempt_id="2222"
    )
    assert fresh.fencing_token == crashed.fencing_token + 1
    assert scheduler.get_task("issue-72").state == sched.CLAIMED

    # 旧执行者迟到 complete：StaleFencing（旧代次不能覆盖新执行者）
    with pytest.raises(sched.StaleFencing):
        scheduler.complete(
            "issue-72",
            attempt_id="1111",
            fencing_token=crashed.fencing_token,
            idempotency_key="1111",
            summary="迟到回传",
        )

    # 新执行者走完 running → complete，任务收敛，标签到位
    scheduler.transition(
        "issue-72", attempt_id="2222", fencing_token=fresh.fencing_token, state=sched.RUNNING
    )
    outcome = scheduler.complete(
        "issue-72",
        attempt_id="2222",
        fencing_token=fresh.fencing_token,
        idempotency_key="2222",
        summary="恢复后完成",
    )
    assert outcome.created is True
    assert scheduler.get_task("issue-72").state == sched.COMPLETED
    assert "state:in-review" in _server.issue_labels(72)
