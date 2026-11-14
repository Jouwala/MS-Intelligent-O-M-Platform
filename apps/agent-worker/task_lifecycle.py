"""任务生命周期写操作：调度器状态迁移 + 统一清理（TASK-06）。

`worker.py` 只负责编排（发现 → 认领 → 执行 → 回传），本模块收敛两类容易写错的
副作用，让「所有退出路径都走同一套清理」这件事只有一个实现：

- **状态迁移**：upsert / acquire_scope / transition / complete（幂等键
  attempt_id）/ ack_cancel / renew / is_cancel_requested / mirror.drain；
  全部带 attempt_id + fencing_token，收到 `StaleFencing` 一律视为「旧执行者，
  结果作废」，不重试、不覆盖新实例状态；
- **统一清理**：释放范围锁 → 释放租约（除非已终态）→ 子进程树确认退出后
  按策略处理 worktree → 清 `_active`；清理自身异常只收集到 `cleanup_errors`，
  **绝不覆盖原始异常**。

清理策略（与 worker.py 顶部说明一致）：成功保留 worktree；失败/超时/阻塞保留
诊断产物但不得残留租约/范围锁/_active；取消/内部异常确认子进程退出后强制回收。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional


@dataclass
class ActiveTask:
    """一次执行尝试的上下文（worker 填、lifecycle 读写、finally 清）。"""

    issue: int
    task_id: str
    slug: str
    worktree: Optional[Path] = None
    attempt_id: str = ""
    fencing_token: int = 0
    scope: tuple[str, ...] = ()
    scope_acquired: bool = False
    lease_held: bool = False
    terminal: bool = False
    aborted: bool = False                     # 租约/尝试已被判旧（StaleFencing）
    result_summary: str = ""
    commit_sha: Optional[str] = None
    executor_confirmed_exit: bool = True       # 子进程树确认退出才允许删 worktree
    cleanup_errors: list[str] = field(default_factory=list)


@dataclass
class ReclaimOutcome:
    """清理结果（测试与排障用；异常文本不抛出，只回报）。"""

    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class TaskLifecycle:
    def __init__(
        self,
        scheduler,
        allocator,
        config,
        *,
        mirror=None,
        clock: Callable[[], object] = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.scheduler = scheduler
        self.allocator = allocator
        self.config = config
        self.mirror = mirror
        self.clock = clock
        self.log = log

    # -- 登记 / 范围锁 ---------------------------------------------------------
    def upsert(self, issue, active) -> None:
        self.scheduler.upsert_task(
            active.task_id,
            issue.number,
            repo=self.config.repo,
            title=getattr(issue, "title", ""),
            scopes=list(active.scope),
        )

    def acquire_scope(self, active, S, sleep: Callable[[float], None]) -> bool:
        """等范围锁；超时标 BLOCKED 返回 False，租约被判旧（aborted）也返回 False。"""
        cfg = self.config
        waited = 0.0
        while True:
            try:
                self.scheduler.acquire_scope(
                    active.task_id,
                    attempt_id=active.attempt_id,
                    fencing_token=active.fencing_token,
                    scopes=list(active.scope),
                )
                active.scope_acquired = True
                return True
            except S.ScopeConflict as exc:
                if waited >= cfg.scope_wait_timeout_s:
                    self.mark_blocked(
                        active, "scope-timeout", f"文件范围被占用且等待超时，任务阻塞：{exc}", S
                    )
                    return False
                self.log(f"[worker] 范围冲突，串行化等待：{exc}")
                sleep(cfg.poll_interval_s)
                waited += cfg.poll_interval_s
            except S.StaleFencing as exc:
                self.log(f"[worker] 范围锁认领被拒（租约已旧）：{exc}")
                active.aborted = True
                return False

    # -- 状态迁移 ---------------------------------------------------------------
    def mark_running(self, active, S) -> bool:
        """认领（claimed）后、执行前必须显式进入 running，终态迁移才合法。"""
        return self.transition(active, S.RUNNING, "", "", S)

    def mark_blocked(self, active, code: str, message: str, S) -> bool:
        return self.transition(active, S.BLOCKED, code, message, S)

    def mark_retryable(self, active, code: str, message: str, S) -> bool:
        if not active.lease_held:
            return False
        return self.transition(active, S.RETRYABLE, code, message, S)

    def transition(self, active, state: str, code: str, message: str, S) -> bool:
        try:
            self.scheduler.transition(
                active.task_id,
                attempt_id=active.attempt_id,
                fencing_token=active.fencing_token,
                state=state,
                error_code=code,
                error_message=message,
            )
        except S.StaleFencing as exc:
            self.log(f"[worker] 状态迁移被拒（旧执行者）：{exc}")
            active.aborted = True
            # 尝试已旧 ⇒ 租约/范围锁已不属于我，清理阶段不再重复释放
            active.lease_held = False
            active.scope_acquired = False
            return False
        except S.InvalidTransition as exc:
            # 例如取消先到、任务已进入 cancel_requested：由调用方按取消/放弃处理
            self.log(f"[worker] 状态迁移不合法（{state}）：{exc}")
            return False
        if state in (S.RETRYABLE, S.EXPIRED, S.CANCELLED, S.COMPLETED, S.FAILED):
            # 调度器契约：这些迁移会一并释放租约与范围锁
            active.lease_held = False
            active.scope_acquired = False
        if state in (S.CANCELLED, S.COMPLETED, S.FAILED):
            active.terminal = True
        return True

    def complete(
        self, active, summary: str, S, *, terminal_status: str = ""
    ) -> bool:
        """幂等完成（idempotency_key=attempt_id）；成功后 drain 镜像。"""
        status = terminal_status or S.COMPLETED
        try:
            outcome = self.scheduler.complete(
                active.task_id,
                attempt_id=active.attempt_id,
                fencing_token=active.fencing_token,
                idempotency_key=active.attempt_id,
                summary=summary,
                terminal_status=status,
            )
        except S.StaleFencing as exc:
            self.log(f"[worker] 完成被拒（旧执行者，结果作废）：{exc}")
            active.aborted = True
            active.lease_held = False
            active.scope_acquired = False
            return False
        except S.InvalidTransition as exc:
            # 典型是取消先到（running → completed 非法）：绝不能改写成完成
            self.log(f"[worker] 完成前状态已变（{exc}），不再提交完成")
            active.aborted = True
            if self.cancel_requested(active.task_id, S):
                return self.ack_cancel(active, f"完成前状态已变（{exc}）", S)
            return False
        active.terminal = True
        active.lease_held = False
        active.scope_acquired = False  # 完成会一并释放范围锁（调度器契约）
        self.log(
            f"[worker] 任务 {active.task_id} 完成（{status}，"
            f"created={getattr(outcome, 'created', None)}）：{summary}"
        )
        self.drain_mirror()
        return True

    def complete_failure(self, active, code: str, summary: str, S) -> bool:
        return self.complete(active, f"{code}: {summary}", S, terminal_status=S.FAILED)

    def ack_cancel(self, active, summary: str, S) -> bool:
        if not active.lease_held and active.aborted:
            return False
        try:
            self.scheduler.ack_cancel(
                active.task_id,
                attempt_id=active.attempt_id,
                fencing_token=active.fencing_token,
            )
        except S.StaleFencing as exc:
            self.log(f"[worker] 取消确认被拒（旧执行者）：{exc}")
            active.aborted = True
            active.lease_held = False
            active.scope_acquired = False
            return False
        except S.InvalidTransition as exc:
            # 已是终态或被他人先一步取消：我方不再重复确认
            self.log(f"[worker] 取消确认遇到非法迁移（{exc}），按不作废处理")
            active.aborted = True
            return False
        active.terminal = True
        active.lease_held = False
        active.scope_acquired = False  # ack_cancel 会一并释放租约与范围锁
        self.log(f"[worker] 任务 {active.task_id} 已取消：{summary}")
        self.drain_mirror()
        return True

    def renew(self, active, S) -> bool:
        """执行期续租；返回 False 表示租约已丢失（调用方应停止执行）。"""
        if not active.lease_held:
            return False
        try:
            self.scheduler.renew(
                active.task_id,
                attempt_id=active.attempt_id,
                fencing_token=active.fencing_token,
                lease_ttl=timedelta(seconds=self.config.lease_ttl_s),
            )
            return True
        except S.StaleFencing as exc:
            self.log(f"[worker] 续租被拒（租约已丢失）：{exc}")
            active.lease_held = False
            active.aborted = True
            return False
        except Exception as exc:  # noqa: BLE001 - 单次续租失败下个周期再试
            self.log(f"[worker] 续租失败（下个周期重试）：{exc}")
            return True

    def cancel_requested(self, task_id: str, S) -> bool:
        try:
            return bool(self.scheduler.is_cancel_requested(task_id))
        except Exception as exc:  # noqa: BLE001 - 读不到就按未取消，执行完再复核
            self.log(f"[worker] 读取取消标志失败：{exc}")
            return False

    # -- 镜像 -------------------------------------------------------------------
    def drain_mirror(self) -> int:
        """重放 outbox 里的 GitHub 写操作；mirror 自己保证幂等、失败不抛。"""
        if self.mirror is None:
            return 0
        try:
            # 契约：GitHubMirror(transport).drain(scheduler, limit=100) -> int
            return int(self.mirror.drain(self.scheduler, limit=self.config.drain_limit) or 0)
        except Exception as exc:  # noqa: BLE001 - outbox 保留，下个周期继续
            self.log(f"[worker] GitHubMirror.drain 失败（outbox 保留待重放）：{exc}")
            return 0

    # -- 统一清理 ---------------------------------------------------------------
    @staticmethod
    def should_release_worktree(outcome: str) -> bool:
        # 取消 / 内部异常：放弃本轮产物，确认子进程退出后强制回收；
        # 成功保留（要 review/开 PR），失败/阻塞/被重派保留诊断产物。
        return outcome in ("cancelled", "error")

    def cleanup(
        self,
        active,
        outcome: str,
        *,
        original_exc: Optional[BaseException] = None,
        heartbeat=None,
    ) -> ReclaimOutcome:
        """覆盖所有退出路径的统一清理；返回收集到的问题（不抛异常）。"""
        result = ReclaimOutcome()

        def attempt(what: str, fn: Callable[[], object]) -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - 清理异常只记录，不覆盖原始异常
                result.errors.append(f"{what} 失败：{exc!r}")

        # 1) 范围锁（先于租约释放，避免新旧实例交错拿到同一范围）
        if active is not None and active.scope_acquired and active.attempt_id:
            attempt(
                "release_scope",
                lambda: self.scheduler.release_scope(
                    active.task_id,
                    attempt_id=active.attempt_id,
                    fencing_token=active.fencing_token,
                ),
            )
            active.scope_acquired = False

        # 2) 租约（已终态/已被迁移释放时不再释放）
        if active is not None and active.lease_held and not active.terminal and active.attempt_id:
            attempt(
                "release 租约",
                lambda: self.scheduler.release(
                    active.task_id,
                    attempt_id=active.attempt_id,
                    fencing_token=active.fencing_token,
                ),
            )
            active.lease_held = False

        # 3) worktree：子进程树确认退出后才允许删
        if active is not None and active.worktree is not None:
            if self.should_release_worktree(outcome):
                if not active.executor_confirmed_exit:
                    result.errors.append("子进程树未确认退出，跳过 worktree 回收")
                else:
                    attempt(
                        f"回收 worktree {active.slug}",
                        lambda: self.allocator.release(active.slug, force=True),
                    )

        # 4) 清心跳中的任务状态（后台线程由 Worker 关闭，_active 由调用方清理）
        if heartbeat is not None:
            heartbeat.update(task_id="", state="idle")
        if active is not None:
            active.cleanup_errors = result.errors
        for err in result.errors:
            self.log(f"[worker] 清理告警：{err}")
        if result.errors and original_exc is not None:
            self.log("[worker] 清理告警不覆盖原始异常（原始异常继续向上传播）")
        return result
