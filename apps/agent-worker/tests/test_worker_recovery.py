"""断线恢复验收（原 Issue #18 语义，改由中央调度器 + outbox 镜像承载）。

- 断网期间：GitHub 回写（mirror.drain）失败只保留 outbox，不重复执行；
  重连后 drain 重放，全程恰好一次完成；
- 租约过期且任务被重派：旧实例的完成被 fencing 拒绝（StaleFencing），
  新实例只完成一次，零重复结果。
"""

from __future__ import annotations

from datetime import timedelta

from conftest import gi, make_worker, wk

BODY = "## 目标\n写结果文件。\n## 改动文件\n- `result.txt`\n## 执行命令\n```\necho ok\n```\n"


def _issue(transport, number: int):
    return transport.create_issue(
        number, "断线恢复", BODY, labels=("agent:muse-2", "state:todo")
    )


def _runner_ok(wt_path):
    (wt_path / "result.txt").write_text("done\n", encoding="utf-8")
    return 0


def test_disconnect_5min_mirror_outbox_resumes_without_duplicate(
    transport, clock, git_repo, scheduler, mirror
):
    """断网 5 分钟：执行与完成只发生一次；GitHub 回写延后到重连后的 drain。"""
    issue = _issue(transport, 61)
    worker = make_worker(transport, git_repo, clock, scheduler, mirror, runner=_runner_ok)
    worker.register()
    mirror.fail_next = 1  # 执行完成后的 GitHub 回写时断网

    assert worker.handle_task(issue) == "completed"
    assert len(scheduler.completions) == 1
    assert scheduler.outbox and scheduler.outbox[0]["mirrored"] is False
    assert scheduler.leases.get("issue-61") is None

    clock.advance(300)  # 断网 5 分钟
    # 重连后的周期先 drain：标签可能还没收敛（本替身不改 GitHub 标签），
    # 于是会再遇到 AlreadyTerminal —— 关键是没有第二条结果、outbox 已被重放。
    assert worker.run_once() in ("idle", "claim-conflict")
    assert scheduler.outbox[0]["mirrored"] is True
    assert len(scheduler.completions) == 1  # 没有第二条结果


def test_lease_expired_reassigned_old_attempt_rejected(
    transport, clock, git_repo, scheduler, mirror
):
    """租约过期被重派：旧实例的完成被 StaleFencing 拒绝，全程恰好一次结果。"""
    issue = _issue(transport, 62)

    def runner_old(wt_path):
        (wt_path / "result.txt").write_text("A 的结果\n", encoding="utf-8")
        # 执行期间租约过期（120s），另一实例 B 接手并完成
        clock.advance(130)
        lease_b = scheduler.claim(
            "issue-62",
            agent_id="muse-2",
            instance_id="inst-b",
            attempt_id="b-attempt",
            lease_ttl=timedelta(minutes=10),
        )
        scheduler.transition(
            "issue-62",
            attempt_id="b-attempt",
            fencing_token=lease_b.fencing_token,
            state=scheduler.S.RUNNING,
        )
        scheduler.complete(
            "issue-62",
            attempt_id="b-attempt",
            fencing_token=lease_b.fencing_token,
            idempotency_key="b-attempt",
            summary="重派后由新实例完成。",
        )
        return 0

    worker = make_worker(
        transport,
        git_repo,
        clock,
        scheduler,
        mirror,
        runner=runner_old,
        lease_ttl_s=120,
    )
    assert worker.handle_task(issue) == "aborted-reassigned"
    assert len(scheduler.completions) == 1  # 只有 B 的一次
    assert worker.active is None
    assert scheduler.leases.get("issue-62") is None
    # 旧实例不再乱发 GitHub 回写（outbox 里只有 B 的一条）
    assert [op["completion_id"] for op in scheduler.outbox] == ["b-attempt"]


def test_failure_inside_runner_keeps_original_exception_after_cleanup(
    transport, clock, git_repo, scheduler, mirror
):
    """内部异常：清理后原样抛出；租约/范围锁/_active 不残留。"""
    issue = _issue(transport, 63)

    def runner_boom(wt_path):
        raise ValueError("原始异常")

    worker = make_worker(transport, git_repo, clock, scheduler, mirror, runner=runner_boom)
    try:
        worker.handle_task(issue)
    except ValueError as exc:
        assert "原始异常" in str(exc)
    else:  # pragma: no cover - 明确要求异常必须传播
        raise AssertionError("内部异常必须向上传播，不能被清理吞掉")

    assert worker.active is None
    assert scheduler.leases.get("issue-63") is None
    assert scheduler.scope_locks.get("issue-63") is None
    # 执行器自身异常 → 子进程树未确认退出，工作区保留（不删）
    assert any("未确认退出" in err for err in worker.cleanup_errors)


def test_scheduler_module_task_id_convention():
    assert wk.task_id_for_issue(42) == "issue-42"
    assert wk.task_id_for_issue(0) == "issue-0"
