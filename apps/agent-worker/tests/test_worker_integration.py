"""真实 SQLiteStore + SchedulerService + GitHubMirror 的端到端集成（Lead 验收要求）。

不复用 FakeScheduler：临时 SQLite 文件库、真实事务/fencing/outbox，
GitHub 侧用内存传输替身（不访问网络），验证 Worker 的认领 → 执行 →
幂等完成 → 镜像回写 → 标签收敛整条链路。
"""

from __future__ import annotations

from datetime import timedelta

from conftest import ex, gi, wk

BODY = (
    "## 目标\n写结果。\n## 改动文件\n- `result.txt`\n"
    "## 执行命令\n```json\n[\"python\", \"-c\", \"print('ok')\"]\n```\n"
)


def _runner_ok(path):
    (path / "result.txt").write_text("ok\n", encoding="utf-8")
    return 0


def _completed_comments(transport, number: int) -> list[dict]:
    return [
        header
        for header in (gi.parse_header(c.body) for c in transport.list_comments(number))
        if (header or {}).get("type") == "TASK_COMPLETED"
    ]


def test_real_scheduler_end_to_end(tmp_path, transport, clock, git_repo):
    sched = wk.scheduler_module()
    db_path = tmp_path / "scheduler.db"
    scheduler = sched.SchedulerService(
        sched.SQLiteStore(db_path), clock=clock, lease_ttl=timedelta(minutes=10)
    )
    mirror = sched.GitHubMirror(transport)
    issue = transport.create_issue(
        70, "真实调度器集成", BODY, labels=("agent:muse-2", "state:todo")
    )
    adapter = gi.GitHubIssueAdapter(
        transport, "muse-2", clock=clock, lease_ttl=timedelta(hours=4)
    )
    executor = ex.Executor(term_grace_s=0.2, kill_wait_s=2.0, poll_interval_s=0.02)
    worker = wk.AgentWorker(
        adapter,
        wk.WorktreeAllocator(git_repo),
        executor,
        wk.WorkerConfig(
            agent_id="muse-2",
            repo="Jouwala/noc-soc-intelligent-platform",
            cancel_poll_interval_s=0.02,
            lease_renew_interval_s=10**6,
        ),
        scheduler,
        mirror=mirror,
        clock=clock,
        sleep=lambda seconds: clock.advance(seconds),
        runner=_runner_ok,
    )

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

    # 真实 outbox → GitHubMirror 回写：一条 TASK_COMPLETED 评论 + state:in-review
    completed = _completed_comments(transport, 70)
    assert len(completed) == 1
    labels = transport.get_issue(70).labels
    assert "state:in-review" in labels and "state:todo" not in labels

    # 再 drain 一次：outbox 已清，幂等重放不产生第二条评论
    assert mirror.drain(scheduler) == 0
    assert len(_completed_comments(transport, 70)) == 1

    # 同一 attempt/幂等键重复 complete：created=False，无新副作用
    replay = scheduler.complete(
        "issue-70",
        attempt_id=snap.attempt_id,
        fencing_token=snap.fencing_token,
        idempotency_key=snap.attempt_id,
        summary="重复回传",
    )
    assert replay.created is False
    assert len(_completed_comments(transport, 70)) == 1

    # 换一个 service 实例读同一数据库：终态已持久化
    reopened = sched.SchedulerService(sched.SQLiteStore(db_path), clock=clock)
    assert reopened.get_task("issue-70").state == "completed"


def test_real_scheduler_blocks_then_retry_recovers(tmp_path, transport, clock, git_repo):
    """无执行命令 → 阻塞释放租约；补齐命令后同任务可再次被认领执行。"""
    sched = wk.scheduler_module()
    scheduler = sched.SchedulerService(
        sched.SQLiteStore(tmp_path / "scheduler.db"), clock=clock
    )
    mirror = sched.GitHubMirror(transport)
    issue = transport.create_issue(
        71, "先阻塞后重试", "## 改动文件\n- `result.txt`\n",
        labels=("agent:muse-2", "state:todo"),
    )
    adapter = gi.GitHubIssueAdapter(transport, "muse-2", clock=clock)
    executor = ex.Executor(term_grace_s=0.2, kill_wait_s=2.0, poll_interval_s=0.02)
    common = dict(
        agent_id="muse-2",
        cancel_poll_interval_s=0.02,
        lease_renew_interval_s=10**6,
    )
    blocked_worker = wk.AgentWorker(
        adapter,
        wk.WorktreeAllocator(git_repo),
        executor,
        wk.WorkerConfig(**common),
        scheduler,
        mirror=mirror,
        clock=clock,
        sleep=lambda seconds: clock.advance(seconds),
    )
    assert blocked_worker.handle_task(issue) == "blocked-no-command"
    blocked = scheduler.get_task("issue-71")
    # 阻塞后释放租约：BLOCKED（保留租约）→ release 落 RETRYABLE，不可则 QUEUED
    assert blocked.state in (sched.BLOCKED, sched.RETRYABLE, sched.QUEUED)
    assert blocked.lease_expire_at is None  # 阻塞后不得留租约

    retry_worker = wk.AgentWorker(
        adapter,
        wk.WorktreeAllocator(git_repo),
        executor,
        wk.WorkerConfig(**common),
        scheduler,
        mirror=mirror,
        clock=clock,
        sleep=lambda seconds: clock.advance(seconds),
        runner=_runner_ok,
    )
    assert retry_worker.handle_task(issue) == "completed"
    assert scheduler.get_task("issue-71").state == "completed"
