"""PR-2 fencing：代次校验、续租推进 token、旧 token 的写操作一律 StaleFencing。

Fencing 是 ref 权威层的安全核心：**任何状态写之前**都必须确认

1. `max_token(list(agent-hwm/issue-N/)) == 我提交的 token`；
2. 该 token 上仍有我这次 attempt 的 lease ref（租约被删=已释放/被回收）。

任一不满足抛 `StaleFencing`，旧执行者（旧 token）**永久**出局：即使它绕过 Worker
直接调 API，也改不了新执行者的状态。续租的语义是**推进 token**（`T → T+1`）：
先创建 `agent-hwm/.../T+1`（CAS，422 → 已被并发推进 → StaleFencing），再建新
token 的 lease ref，最后删旧 lease ref；Worker 必须用返回值更新 `fencing_token`。

本模块同时承载状态迁移（transition/release/取消）的**状态标记评论**写入：refs 是
权威，评论是派生 `get_task` 状态的输入（错误码/时间等）。范围锁释放委托
`ref_scope.release_locks`（PR-4，B2 实现），旧 token 会因代次校验被拒，删不到新锁。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from . import ref_api
from .api import (
    CANCEL_REQUESTED,
    CANCELLED,
    EXPIRED,
    QUEUED,
    RETRYABLE,
    TERMINAL_STATES,
    TRANSITIONS,
    InvalidTransition,
    Lease,
    SchedulerError,
    StaleFencing,
    TaskSnapshot,
    assert_transition,
    iso,
)
from .github_refs import GitHubRefError
from .ref_claim import REASON_STALE, RefClaimer, issue_number_for
from .ref_view import (
    parse_completions,
    parse_state_markers,
    state_comment_body,
    task_id_of,
    terminal_state_of,
)

#: 终态 → GitHub 标签（展示层；写失败只记录，Actions 对账收敛）。
TERMINAL_REF_LABELS = {
    "completed": "state:in-review",
    "failed": "state:blocked",
    "cancelled": "state:todo",
}


class RefFencer(RefClaimer):
    """代次校验 + 续租 + 迁移/释放/取消（PR-2）。"""

    # -- 代次校验（唯一实现，所有写操作前调用） -------------------------------
    def _require_current(
        self, task_id: str, issue_number: int, token: int, attempt_id: str
    ) -> ref_api.LeaseRef:
        max_token = self._max_token(issue_number)
        presented = (attempt_id or "").lower()
        matched: Optional[ref_api.LeaseRef] = None
        if int(token) >= 1:
            for lease in self._list_leases(issue_number, int(token)):
                if lease.attempt_id == presented:
                    matched = lease
                    break
        ref_api.require_current_token(
            task_id, attempt_id, int(token), max_token, matched is not None, REASON_STALE
        )
        assert matched is not None
        return matched

    @staticmethod
    def _find_lease_ref(refs: Sequence[ref_api.LeaseRef], token: int, attempt_id: str) -> str:
        presented = (attempt_id or "").lower()
        for lease in refs:
            if lease.token == int(token) and lease.attempt_id == presented:
                return lease.ref
        return ""

    def _ref_of(self, issue: int, token: int, attempt_id: str) -> str:
        return self._find_lease_ref(self._list_leases(issue, token), token, attempt_id)

    # -- 租约收尾（删 lease ref + 释放范围锁 + 标签收敛） ----------------------
    def _release_locks(self, issue: int, token: int) -> None:
        """委托 ref_scope.release_locks；未就绪/代次已旧时不放大成任务失败。"""
        try:
            from . import ref_scope  # noqa: PLC0415 - 延迟导入：PR-4 边界
        except ImportError:
            return
        try:
            ref_scope.release_locks(
                self.ref_store,
                self.transport,
                issue_number=issue,
                token=int(token),
                scopes=None,
                clock=self.clock,
                ttl=self.lease_ttl,
            )
        except (StaleFencing, InvalidTransition):
            return  # 旧代次/已无 lease：孤儿 scope 由 Reaper 的 cleanup_orphans 收拾

    def _converge_labels(self, issue_number: int, target_label: str) -> bool:
        """标签收敛（展示层，最终一致）：摘掉全部 state:* 再补目标标签。

        有界重试（`label_retries`）；仍失败只记录到 `label_errors`，**不失败**权威
        操作（完成/释放已由评论标记确定），由 Actions 对账/Reaper 收敛。
        """
        if not target_label:
            return True
        last_error = ""
        for attempt in range(self.label_retries + 1):
            try:
                issue = self.transport.get_issue(issue_number)
                current = tuple(getattr(issue, "labels", ()) or ())
                desired = [label for label in current if not label.startswith("state:")]
                if target_label not in desired:
                    desired.append(target_label)
                if tuple(desired) == current:
                    return True
                self.transport.set_labels(issue_number, desired)
                return True
            except Exception as exc:  # noqa: BLE001 - 标签不是权威
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= self.label_retries:
                    break
        self.label_errors.append(
            f"issue={issue_number} target={target_label} attempt={self.label_retries + 1} {last_error}"
        )
        return False

    def _end_lease(self, issue: int, token: int, attempt_id: str, state: str) -> None:
        """租约生命周期结束时的收尾。

        **QUEUED 也必须删租约 ref**（真实缺陷修复，sched-core 在 PR-6 发现）：
        `release()` 从 BLOCKED 回落时状态机只能走 BLOCKED→QUEUED；若 QUEUED 不删
        lease ref，`get_task` 会按 epoch 派生回 claimed（lease_expire_at 非空），
        新实例因此 LeaseHeld → 阻塞任务永远无法被重试。凡「状态已回落、本实例不再
        持有执行权」的迁移（queued/retryable/expired/终态）都必须删租约 + 释放范围锁。
        """
        if state in (QUEUED, RETRYABLE, EXPIRED):
            self._delete_quietly(self._ref_of(issue, token, attempt_id))
            self._release_locks(issue, token)
            return
        if state in TERMINAL_STATES:
            self._delete_quietly(self._ref_of(issue, token, attempt_id))
            self._release_locks(issue, token)
            self._converge_labels(issue, TERMINAL_REF_LABELS.get(state, "state:in-review"))

    # -- 续租（推进 token；Worker 必须采纳返回的 fencing_token） --------------
    def renew(
        self,
        task_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        lease_ttl: Optional[timedelta] = None,
    ) -> Lease:
        issue = issue_number_for(task_id)
        ttl = lease_ttl or self.lease_ttl
        current = self._require_current(task_id, issue, fencing_token, attempt_id)
        anchor = self.anchor()
        candidate = int(fencing_token) + 1
        try:
            won = bool(self.ref_store.create(ref_api.hwm_ref(issue, candidate), anchor))
        except GitHubRefError:
            won = False  # 结果不确定/不可用：按旧执行者处理（安全侧）
        if not won:
            raise StaleFencing(
                task_id,
                attempt_id,
                int(fencing_token),
                current.attempt_id,
                candidate,
                "续租推进 token 失败（已被并发推进或 refs 不可用）",
            )
        now = self.now()
        now_ms = int(now.timestamp() * 1000)
        lease_name = ref_api.lease_ref(
            issue, candidate, now_ms, current.agent_id, current.instance_id, current.attempt_id
        )
        try:
            created = bool(self.ref_store.create(lease_name, anchor))
        except GitHubRefError as exc:
            raise SchedulerError(
                f"续租 token={candidate} 已推进，但新租约 ref 创建失败"
                f"（下次续租会按旧执行者停止）：{exc}"
            ) from exc
        if not created:
            raise StaleFencing(
                task_id,
                attempt_id,
                int(fencing_token),
                current.attempt_id,
                candidate,
                "续租新租约 ref 已存在",
            )
        self._delete_quietly(current.ref)
        self._comment_best_effort(
            issue,
            ref_api.renew_comment_body(
                issue_number=issue,
                token=candidate,
                attempt_id=current.attempt_id,
                timestamp=iso(now),
                agent_id=current.agent_id,
            ),
            current.agent_id,
        )
        claimed_at = datetime.fromtimestamp(current.epoch_ms / 1000, timezone.utc)
        return Lease(
            task_id=task_id,
            attempt_id=current.attempt_id,
            agent_id=current.agent_id,
            instance_id=current.instance_id,
            fencing_token=candidate,
            claimed_at=claimed_at,
            expires_at=now + ttl,
            heartbeat_at=now,
        )

    # -- 状态迁移 ------------------------------------------------------------
    def transition(
        self,
        task_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        state: str,
        error_code: str = "",
        error_message: str = "",
    ) -> TaskSnapshot:
        issue = issue_number_for(task_id)
        lease = self._require_current(task_id, issue, fencing_token, attempt_id)
        view = self.view(issue, task_id=task_id)
        assert_transition(view.state, state)
        now = self.now()
        # 先写状态标记（派生状态的权威输入），成功后再做租约收尾
        self._comment(
            issue,
            state_comment_body(
                issue_number=issue,
                token=int(fencing_token),
                state=state,
                timestamp=now,
                error_code=error_code,
                error_message=error_message,
            ),
            lease.agent_id,
        )
        self._end_lease(issue, int(fencing_token), attempt_id, state)
        return self.snapshot(task_id, issue_number=issue, repo=view.repo, title=view.title)

    # -- 释放 ----------------------------------------------------------------
    def release(
        self,
        task_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        terminal_state: Optional[str] = None,
    ) -> TaskSnapshot:
        if terminal_state is not None and terminal_state not in TERMINAL_STATES:
            raise ValueError(f"release 的 terminal_state 必须是终态：{terminal_state}")
        issue = issue_number_for(task_id)
        lease = self._require_current(task_id, issue, fencing_token, attempt_id)
        view = self.view(issue, task_id=task_id)
        if terminal_state is not None:
            assert_transition(view.state, terminal_state)
            target = terminal_state
        else:
            # 主动放弃：按状态机优先 RETRYABLE，其次 QUEUED（与 SQLite 版语义一致）
            target = view.state
            for candidate in (RETRYABLE, QUEUED):
                try:
                    assert_transition(view.state, candidate)
                except InvalidTransition:
                    continue
                target = candidate
                break
        now = self.now()
        self._comment(
            issue,
            state_comment_body(
                issue_number=issue,
                token=int(fencing_token),
                state=target,
                timestamp=now,
                reason="主动释放租约",
            ),
            lease.agent_id,
        )
        self._end_lease(issue, int(fencing_token), attempt_id, target)
        return self.snapshot(task_id, issue_number=issue, repo=view.repo, title=view.title)

    # -- 取消 ----------------------------------------------------------------
    def request_cancel(self, task_id: str, *, reason: str = "") -> TaskSnapshot:
        issue = issue_number_for(task_id)
        view = self.view(issue, task_id=task_id)
        if view.state in TERMINAL_STATES:
            return self.snapshot(task_id, issue_number=issue, repo=view.repo, title=view.title)
        target = CANCEL_REQUESTED if view.live is not None else CANCELLED
        if target == CANCEL_REQUESTED and CANCEL_REQUESTED not in TRANSITIONS.get(
            view.state, frozenset()
        ):
            target = CANCELLED
        now = self.now()
        self._comment(
            issue,
            state_comment_body(
                issue_number=issue,
                token=view.max_token,
                state=target,
                timestamp=now,
                reason=f"取消请求：{reason}" if reason else "取消请求",
            ),
        )
        if target == CANCELLED:
            self._release_locks(issue, view.max_token)
            self._converge_labels(issue, "state:todo")
        return self.snapshot(task_id, issue_number=issue, repo=view.repo, title=view.title)

    def ack_cancel(self, task_id: str, *, attempt_id: str, fencing_token: int) -> TaskSnapshot:
        issue = issue_number_for(task_id)
        lease = self._require_current(task_id, issue, fencing_token, attempt_id)
        view = self.view(issue, task_id=task_id)
        if view.state != CANCEL_REQUESTED:
            raise InvalidTransition(view.state, CANCELLED)
        assert_transition(view.state, CANCELLED)
        now = self.now()
        self._comment(
            issue,
            state_comment_body(
                issue_number=issue,
                token=int(fencing_token),
                state=CANCELLED,
                timestamp=now,
                reason="执行者确认取消",
            ),
            lease.agent_id,
        )
        self._end_lease(issue, int(fencing_token), attempt_id, CANCELLED)
        return self.snapshot(task_id, issue_number=issue, repo=view.repo, title=view.title)

    def is_cancel_requested(self, task_id: str) -> bool:
        issue = issue_number_for(task_id)
        return self.view(issue, task_id=task_id).state == CANCEL_REQUESTED


__all__ = [
    "RefFencer",
    "TERMINAL_REF_LABELS",
    "parse_completions",
    "parse_state_markers",
    "task_id_of",
    "terminal_state_of",
]
