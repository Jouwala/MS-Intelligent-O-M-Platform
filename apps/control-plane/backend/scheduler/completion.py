"""幂等完成 + outbox 生成（TASK-05 的调度侧）。

`complete()` 在**一个事务**里写终态、写 completion（幂等键）、生成 outbox 的三条
待重放操作（comment / labels / audit）、删除租约与范围锁。GitHub 写回不在事务里，
由 `mirror.py` 重放 outbox；中间失败只影响 outbox 状态，不影响任务终态。

过期租约语义（刻意如此，不是漏校验）
------------------------------------
`_require_lease(...)` 对 complete 默认 `allow_expired=True`：
- 租约**已过期但还没有被新实例接管**：旧执行者的结果仍然有效，允许完成
  （否则「执行完成前一刻网络抖动导致续租失败」会把已经做完的工作作废）；
- 租约过期且已被新实例 claim：fencing_token 已前进，旧代码提交时 token 不符，
  必然 `StaleFencing`，新执行者的状态不会被旧结果覆盖。
租约过期只决定「谁还能认领」，不单独作废一次已经完成的执行；权威判据始终是
`attempt_id + fencing_token`。两种路径都有测试覆盖（test_c05 / test_c13）。

幂等规则
--------
- 同 `idempotency_key` 重放：返回既有 `CompleteOutcome(created=False)`，不新增
  outbox，也不再改任何行（「评论已成功、标签失败后重试」走 mirror 重放，不走这里）。
- 同 `(task_id, attempt_id)` 但换了幂等键：同样视为重放（一次尝试只能完成一次）。
- 旧 attempt / 旧 fencing_token：`StaleFencing`，旧执行者不能把新执行者改成终态。
- 任务已是终态但幂等键不同：`SchedulerError`（拒绝用新键再次触发回写）。

新增消息类型
------------
`TASK_FAILED` / `TASK_CANCELLED` 是本轮扩展的评论机器可读头（`agent-protocol.md`
第八节已说明）；GitHub Issue 的权威状态仍是标签 + 调度器，扩展类型只影响注释。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .api import (
    CANCELLED,
    COMPLETED,
    CompleteOutcome,
    FAILED,
    OutboxOp,
    SchedulerError,
    TERMINAL_STATES,
    assert_transition,
    iso,
    to_epoch,
)
from .common import dumps_json, outbox_snapshot, require_task, write_audit
from .store import SQLiteStore  # noqa: F401

#: outbox 操作状态字面量（mirror 与测试共用，避免多处各写一套字符串）。
OUTBOX_PENDING = "pending"
OUTBOX_DONE = "done"
OUTBOX_ERROR = "error"

#: 终态 → GitHub 标签（mirror 会先去掉所有 state:* 再收敛到目标标签）。
#: 注意：`state:qa-failed` 专指 QA 冒烟失败；执行失败（命令非 0 / 超时）落到
#: `state:blocked`（协议允许从任何状态进入，需要人/协调者处理）。
TERMINAL_LABELS = {
    COMPLETED: "state:in-review",
    FAILED: "state:blocked",
    CANCELLED: "state:todo",
}

#: 终态 → 协议消息类型（评论机器可读头；TASK_FAILED/TASK_CANCELLED 为本轮扩展）。
TERMINAL_MSG_TYPES = {
    COMPLETED: "TASK_COMPLETED",
    FAILED: "TASK_FAILED",
    CANCELLED: "TASK_CANCELLED",
}


def completion_marker(idempotency_key: str) -> str:
    """评论幂等标记：mirror 发评论前先分页扫这个标记，避免重复评论。"""
    return f"<!-- completion-id: {idempotency_key} -->"


def build_comment_body(
    *,
    task_id: str,
    issue_number: int,
    agent_id: str,
    attempt_id: str,
    idempotency_key: str,
    summary: str,
    terminal_status: str,
    now: datetime,
) -> str:
    """生成 TASK_COMPLETED（或失败/取消）评论正文，第一行是机器可读头。"""
    msg_type = TERMINAL_MSG_TYPES.get(terminal_status, "TASK_COMPLETED")
    header = (
        f"<!-- agent-msg: {msg_type}, task: #{issue_number}, "
        f"from: {agent_id or 'scheduler'}, at: {iso(now)} -->"
    )
    lines = [
        header,
        completion_marker(idempotency_key),
        summary or "",
        f"attempt: {attempt_id}",
        f"task_id: {task_id}",
    ]
    return "\n".join(line for line in lines if line != "")


class CompletionMixin:
    """`SchedulerService` 的完成与 outbox 部分。"""

    store: "SQLiteStore"
    clock: object

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
        now = self._now()
        now_epoch = to_epoch(now)
        with self.store.transaction() as conn:
            task = require_task(conn, task_id)
            # 1) 幂等键重放：返回既有结果，零副作用
            existing = conn.execute(
                "SELECT * FROM completions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if existing["task_id"] != task_id:
                    raise SchedulerError(
                        f"幂等键 {idempotency_key} 已用于任务 {existing['task_id']}，"
                        f"不能复用到 {task_id}"
                    )
                write_audit(
                    conn,
                    now_epoch,
                    task_id=task_id,
                    event="complete_replay",
                    attempt_id=attempt_id,
                    fencing_token=int(fencing_token),
                    detail={"idempotency_key": idempotency_key, "terminal_status": existing["terminal_status"]},
                )
                return CompleteOutcome(
                    task_id=task_id,
                    terminal_status=existing["terminal_status"],
                    idempotency_key=idempotency_key,
                    created=False,
                    outbox_ops=0,
                )
            # 2) 同一尝试换幂等键重放：同样视为重复完成
            same_attempt = conn.execute(
                "SELECT * FROM completions WHERE task_id = ? AND attempt_id = ?",
                (task_id, attempt_id),
            ).fetchone()
            if same_attempt is not None:
                return CompleteOutcome(
                    task_id=task_id,
                    terminal_status=same_attempt["terminal_status"],
                    idempotency_key=same_attempt["idempotency_key"],
                    created=False,
                    outbox_ops=0,
                )
            # 3) 已是终态（键不同）——不再接受新的完成语义
            if task["state"] in TERMINAL_STATES:
                raise SchedulerError(
                    f"任务 {task_id} 已是终态 {task['state']}，拒绝用新幂等键重复完成"
                )
            # 4) fencing：旧执行者拒绝
            lease = self._require_lease(conn, task_id, attempt_id, fencing_token)
            current = task["state"]
            assert_transition(current, terminal_status)
            # 5) 终态 + completion（唯一约束是并发下的第二道保险）
            completed_at = now_epoch
            conn.execute(
                "INSERT INTO completions(idempotency_key, task_id, attempt_id,"
                " terminal_status, summary, created_at) VALUES (?,?,?,?,?,?)",
                (idempotency_key, task_id, attempt_id, terminal_status, summary, completed_at),
            )
            conn.execute(
                "UPDATE tasks SET state = ?, terminal_status = ?, completed_at = ?,"
                " idempotency_key = ?, error_code = ?, error_message = ?, updated_at = ?"
                " WHERE task_id = ?",
                (
                    terminal_status,
                    terminal_status,
                    completed_at,
                    idempotency_key,
                    "TERMINAL_FAILED" if terminal_status == FAILED else "",
                    summary if terminal_status == FAILED else "",
                    completed_at,
                    task_id,
                ),
            )
            self._purge_lease(conn, task_id)
            self._end_attempt(conn, lease["attempt_id"], terminal_status, now_epoch)
            outbox_ops = self._enqueue_completion_outbox(
                conn,
                task=task,
                lease=lease,
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
                summary=summary,
                terminal_status=terminal_status,
                now=now,
                now_epoch=now_epoch,
            )
            write_audit(
                conn,
                now_epoch,
                task_id=task_id,
                event="complete",
                agent_id=lease["agent_id"],
                instance_id=lease["instance_id"],
                attempt_id=attempt_id,
                fencing_token=int(fencing_token),
                detail={
                    "idempotency_key": idempotency_key,
                    "terminal_status": terminal_status,
                    "outbox_ops": outbox_ops,
                },
            )
        return CompleteOutcome(
            task_id=task_id,
            terminal_status=terminal_status,
            idempotency_key=idempotency_key,
            created=True,
            outbox_ops=outbox_ops,
        )

    def _enqueue_completion_outbox(
        self,
        conn: sqlite3.Connection,
        *,
        task: sqlite3.Row,
        lease: sqlite3.Row,
        attempt_id: str,
        idempotency_key: str,
        summary: str,
        terminal_status: str,
        now: datetime,
        now_epoch: float,
    ) -> int:
        """生成 comment / labels / audit 三条 outbox；与终态在同一事务内落盘。"""
        issue_number = int(task["issue_number"])
        body = build_comment_body(
            task_id=task["task_id"],
            issue_number=issue_number,
            agent_id=lease["agent_id"],
            attempt_id=attempt_id,
            idempotency_key=idempotency_key,
            summary=summary,
            terminal_status=terminal_status,
            now=now,
        )
        ops = [
            (
                "comment",
                {
                    "issue_number": issue_number,
                    "repo": task["repo"] or "",
                    "body": body,
                    "idempotency_key": idempotency_key,
                },
            ),
            (
                "labels",
                {
                    "issue_number": issue_number,
                    "target_label": TERMINAL_LABELS.get(terminal_status, "state:in-review"),
                    "idempotency_key": idempotency_key,
                },
            ),
            (
                "audit",
                {
                    "event": "task_terminal",
                    "task_id": task["task_id"],
                    "terminal_status": terminal_status,
                    "idempotency_key": idempotency_key,
                },
            ),
        ]
        for kind, payload in ops:
            conn.execute(
                "INSERT INTO outbox(task_id, kind, payload, status, attempts, last_error,"
                " created_at, updated_at) VALUES (?,?,?,?,0,'',?,?)",
                (task["task_id"], kind, dumps_json(payload), OUTBOX_PENDING, now_epoch, now_epoch),
            )
        return len(ops)

    # -- outbox 查询与状态回写 ------------------------------------------------
    def pending_outbox(self, limit: int = 100) -> list[OutboxOp]:
        if limit <= 0:
            return []
        rows = self.store.all(
            "SELECT * FROM outbox WHERE status IN (?, ?)"
            " ORDER BY op_id ASC LIMIT ?",
            (OUTBOX_PENDING, OUTBOX_ERROR, int(limit)),
        )
        return [outbox_snapshot(row) for row in rows]

    def mark_outbox(self, op_id: int, *, status: str, error: str = "") -> None:
        if status not in (OUTBOX_DONE, OUTBOX_ERROR, OUTBOX_PENDING):
            raise ValueError(f"非法 outbox 状态：{status}")
        now_epoch = self._now_epoch()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM outbox WHERE op_id = ?", (op_id,)).fetchone()
            if row is None:
                raise SchedulerError(f"outbox 操作不存在：{op_id}")
            # 每次调用记一次投递尝试：成功也算一次，便于观测重放次数
            conn.execute(
                "UPDATE outbox SET status = ?, attempts = attempts + 1,"
                " last_error = ?, updated_at = ? WHERE op_id = ?",
                (status, error, now_epoch, op_id),
            )
