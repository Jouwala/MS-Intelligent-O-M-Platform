"""GitHub 回写：outbox 重放（comment / labels / audit），幂等且可收敛（TASK-05）。

Worker 完成任务的顺序是：`scheduler.complete(...)` → `GitHubMirror(transport).drain(scheduler)`。
完成事务只写本地终态 + outbox；GitHub 是不可靠的远端，任何一步失败都只是把
outbox 行标成 `error` 保留待重试，绝不回滚本地终态，也绝不把异常抛给调用方。

幂等设计
--------
- **comment**：发之前先 `transport.list_comments()` 全量分页扫
  `<!-- completion-id: KEY -->`；已存在就只把 op 标 done。这覆盖「评论其实已发出去、
  但进程在 mark_outbox 之前崩溃」的场景，是「评论成功、标签失败后重试」不重复评论的关键。
- **labels**：读当前 Issue 标签 → 去掉全部 `state:*` → 收敛到目标状态标签；
  已经是目标集合就不发请求（幂等）。
- **audit**：纯本地事件，直接标记完成。

`drain` 返回本轮成功应用的 op 数；失败明细放在 `mirror.last_errors` 里便于 Worker 日志。
"""

from __future__ import annotations

from typing import Any

from .api import OutboxOp, utcnow
from .completion import (
    OUTBOX_DONE,
    OUTBOX_ERROR,
    completion_marker,
)

#: 需要被收敛的标签前缀（状态标签由调度器统一维护，避免多标签并存）。
_MANAGED_LABEL_PREFIX = "state:"


class GitHubMirror:
    """把调度器 outbox 重放到 GitHub。

    推荐用法（Worker 侧）::

        GitHubMirror(transport).drain(scheduler, limit=100)

    也支持先构造再 drain：``mirror = GitHubMirror(transport); mirror.drain(scheduler)``。
    参数可以在构造或调用时给，调用时给优先。
    """

    def __init__(
        self,
        transport: Any = None,
        scheduler: Any = None,
        *,
        clock=utcnow,
    ) -> None:
        self.transport = transport
        self.scheduler = scheduler
        self.clock = clock
        self.last_errors: list[str] = []

    # -- 主入口 --------------------------------------------------------------
    def drain(self, scheduler: Any = None, limit: int = 100) -> int:
        """重放最多 `limit` 条 outbox；返回本轮成功应用的条数，失败不抛异常。"""
        scheduler = scheduler if scheduler is not None else self.scheduler
        transport = self.transport
        if scheduler is None:
            raise ValueError("drain 需要 scheduler（构造参数或调用参数）")
        if transport is None:
            raise ValueError("drain 需要 transport（构造参数或调用参数）")
        self.last_errors = []
        applied = 0
        for op in scheduler.pending_outbox(limit):
            try:
                self._apply(transport, op)
            except Exception as exc:  # noqa: BLE001 - 远端任何失败都只记录、留待重试
                message = f"{type(exc).__name__}: {exc}"
                self.last_errors.append(f"op={op.op_id} kind={op.kind} {message}")
                scheduler.mark_outbox(op.op_id, status=OUTBOX_ERROR, error=message)
                continue
            scheduler.mark_outbox(op.op_id, status=OUTBOX_DONE)
            applied += 1
        return applied

    # -- 单条 op -------------------------------------------------------------
    def _apply(self, transport: Any, op: OutboxOp) -> None:
        payload = dict(op.payload or {})
        kind = op.kind
        if kind == "comment":
            self._apply_comment(transport, payload)
            return
        if kind == "labels":
            self._apply_labels(transport, payload)
            return
        if kind == "audit":
            # 审计事件不需要外部动作，标记完成即收敛
            return
        raise ValueError(f"未知 outbox 操作类型：{kind}")

    @staticmethod
    def _issue_number(payload: dict[str, Any]) -> int:
        raw = payload.get("issue_number")
        if raw is None:
            raise ValueError("outbox 缺少 issue_number")
        return int(raw)

    def _apply_comment(self, transport: Any, payload: dict[str, Any]) -> None:
        number = self._issue_number(payload)
        key = str(payload.get("idempotency_key") or payload.get("completion_id") or "")
        body = str(payload.get("body") or "")
        if not body:
            raise ValueError("comment outbox 缺少 body")
        if key:
            marker = completion_marker(key)
            # 先全量分页读评论再决定是否发送：重复投递时不会产生第二条评论
            for comment in transport.list_comments(number):
                if marker in (getattr(comment, "body", "") or ""):
                    return
        transport.add_comment(number, body)

    def _apply_labels(self, transport: Any, payload: dict[str, Any]) -> None:
        number = self._issue_number(payload)
        target = str(payload.get("target_label") or "")
        issue = transport.get_issue(number)
        current = tuple(getattr(issue, "labels", ()) or ())
        # 状态标签统一由调度器维护：先摘掉全部 state:*，再补目标标签
        desired = [label for label in current if not label.startswith(_MANAGED_LABEL_PREFIX)]
        if target and target not in desired:
            desired.append(target)
        if tuple(desired) == current:
            return  # 已经是目标状态，幂等跳过（不产生无意义的 API 写）
        transport.set_labels(number, desired)
