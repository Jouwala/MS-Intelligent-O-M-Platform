"""PR-5 完成：幂等完成（评论标记 + 标签），不使用本地 outbox/mirror。

ADR-7 修订（owner 2026-11-14）明确：完成幂等状态必须落在 GitHub，不得用本地
outbox 承载。因此本模块：

1. **完成评论是权威标记**：正文含 `<!-- ref-completion-id: KEY -->`（契约生成的
   body 还带 token/attempt/status）。写失败 = 完成失败，Worker 重试；重试时先分页
   扫标记，已有标记直接返回 `CompleteOutcome(created=False)`——**重复提交只会有
   一条完成评论**。
2. **标签是展示层**：完成后摘掉全部 `state:*` 并收敛到终态标签（completed →
   `state:in-review`、failed → `state:blocked`、cancelled → `state:todo`）。
   标签写失败有界重试（`label_retries`），仍失败只记录到 `label_errors`，**不失败
   完成语义**；由 Actions 对账/Reaper 收敛（最终一致）。
3. 完成前仍走 fencing（`_require_current`）与状态机校验；完成后删 lease ref、
   释放范围锁（`_end_lease`）。

同一 attempt 换幂等键重放也返回 `created=False`（与 SQLite 版语义一致）：一次执行
尝试只能完成一次；不同 attempt 改新键则视为真正的重复完成，显式 `SchedulerError`。
"""

from __future__ import annotations

from typing import Optional

from . import ref_api
from .api import (
    COMPLETED,
    FAILED,
    CANCELLED,
    TERMINAL_STATES,
    CompleteOutcome,
    SchedulerError,
    assert_transition,
    iso,
)
from .ref_fencing import RefFencer
from .ref_view import parse_completions

#: 终态 → GitHub 标签（展示层；与 SQLite 时代 TERMINAL_LABELS 语义一致，但这里独立
#: 定义，避免 ref 权威层依赖 SQLite 实现模块）。
COMPLETION_LABELS = {
    COMPLETED: "state:in-review",
    FAILED: "state:blocked",
    CANCELLED: "state:todo",
}


class RefCompleter(RefFencer):
    """幂等完成（PR-5）。"""

    def complete(
        self,
        task_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        idempotency_key: str,
        summary: str,
        terminal_status: str = COMPLETED,
    ) -> CompleteOutcome:
        if not idempotency_key:
            raise ValueError("complete 需要非空 idempotency_key")
        if terminal_status not in TERMINAL_STATES:
            raise ValueError(f"complete 的 terminal_status 必须是终态：{terminal_status}")
        issue = self._issue_of(task_id)
        # 1) 幂等重放判定必须在 fencing 之前：首次完成后 lease ref 已经被删掉了
        comments = self._comments(issue)
        completions = parse_completions(comments)
        presented_attempt = (attempt_id or "").lower()
        for record in completions:
            if record.idempotency_key == idempotency_key or (
                record.attempt_id and record.attempt_id == presented_attempt
            ):
                return CompleteOutcome(
                    task_id=task_id,
                    terminal_status=record.status or terminal_status,
                    idempotency_key=record.idempotency_key,
                    created=False,
                    outbox_ops=0,
                )
        if completions:
            raise SchedulerError(
                f"任务 {task_id} 已由幂等键 {completions[-1].idempotency_key} 完成，"
                f"拒绝用新键 {idempotency_key} 重复完成"
            )
        # 2) fencing + 状态机校验（旧执行者永远完不成）
        lease = self._require_current(task_id, issue, fencing_token, attempt_id)
        view = self.view(issue, task_id=task_id, comments=comments)
        assert_transition(view.state, terminal_status)
        # 3) 完成评论 = 唯一权威标记；写失败整体失败（重试时按幂等重放收敛）
        now = self.now()
        body = ref_api.completion_comment_body(
            issue_number=issue,
            token=int(fencing_token),
            attempt_id=attempt_id,
            terminal_status=terminal_status,
            idempotency_key=idempotency_key,
            summary=_summary(summary),
            timestamp=iso(now),
            agent_id=lease.agent_id,
        )
        self._comment(issue, body, lease.agent_id)
        # 4) 收尾：删 lease ref、释放范围锁、标签收敛（展示层最终一致）
        self._end_lease(issue, int(fencing_token), attempt_id, terminal_status)
        return CompleteOutcome(
            task_id=task_id,
            terminal_status=terminal_status,
            idempotency_key=idempotency_key,
            created=True,
            outbox_ops=0,
        )


def _summary(summary: Optional[str]) -> str:
    """完成评论摘要：保留原文（多行允许），但去掉会破坏首行机器头的标记前缀。"""
    text = str(summary or "").strip()
    return text


__all__ = ["COMPLETION_LABELS", "RefCompleter", "parse_completions"]
