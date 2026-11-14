"""行 → 不可变快照的转换、审计写入等跨模块小工具。

`store.py` 只管连接与事务，`api.py` 只管契约类型；本模块把 SQLite Row 转成
契约里的 frozen dataclass，并提供审计落库函数，供 leasing/locking/completion/
service 共用，避免四份相似的转换代码。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Optional

from .api import (
    DispatchRecord,
    Lease,
    OutboxOp,
    TaskNotFound,
    TaskSnapshot,
    WorkerSnapshot,
    from_epoch,
)


def loads_scopes(raw: Any) -> tuple[str, ...]:
    """把库里的 JSON 文本还原为范围元组；坏数据按空范围处理（不让一条坏行废掉查询）。"""
    if not raw:
        return ()
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def loads_json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def task_snapshot(row: sqlite3.Row) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=row["task_id"],
        issue_number=int(row["issue_number"]),
        repo=row["repo"] or "",
        title=row["title"] or "",
        state=row["state"],
        terminal_status=row["terminal_status"],
        fencing_token=int(row["fencing_token"] or 0),
        attempt_id=row["attempt_id"] or "",
        agent_id=row["agent_id"] or "",
        instance_id=row["instance_id"] or "",
        lease_expire_at=from_epoch(row["lease_expire_at"]),
        heartbeat_at=from_epoch(row["heartbeat_at"]),
        started_at=from_epoch(row["started_at"]),
        completed_at=from_epoch(row["completed_at"]),
        error_code=row["error_code"] or "",
        error_message=row["error_message"] or "",
        idempotency_key=row["idempotency_key"] or "",
        scopes=loads_scopes(row["scopes"]),
        cancel_requested_at=from_epoch(row["cancel_requested_at"]),
        updated_at=from_epoch(row["updated_at"]),
    )


def lease_snapshot(row: sqlite3.Row) -> Lease:
    return Lease(
        task_id=row["task_id"],
        attempt_id=row["attempt_id"],
        agent_id=row["agent_id"],
        instance_id=row["instance_id"],
        fencing_token=int(row["fencing_token"]),
        claimed_at=from_epoch(row["claimed_at"]) or from_epoch(0),
        expires_at=from_epoch(row["expires_at"]) or from_epoch(0),
        heartbeat_at=from_epoch(row["heartbeat_at"]) or from_epoch(0),
    )


def worker_snapshot(row: sqlite3.Row, *, state: Optional[str] = None) -> WorkerSnapshot:
    return WorkerSnapshot(
        agent_id=row["agent_id"],
        instance_id=row["instance_id"],
        hostname=row["hostname"] or "",
        platform=row["platform"] or "",
        capabilities=loads_scopes(row["capabilities"]),
        state=state if state is not None else (row["state"] or "active"),
        last_heartbeat_at=from_epoch(row["last_heartbeat_at"]),
        registered_at=from_epoch(row["registered_at"]),
    )


def outbox_snapshot(row: sqlite3.Row) -> OutboxOp:
    return OutboxOp(
        op_id=int(row["op_id"]),
        task_id=row["task_id"],
        kind=row["kind"],
        payload=loads_json(row["payload"]),
        status=row["status"],
        attempts=int(row["attempts"] or 0),
        last_error=row["last_error"] or "",
    )


def dispatch_snapshot(row: sqlite3.Row) -> DispatchRecord:
    return DispatchRecord(
        dispatch_key=row["dispatch_key"],
        issue_number=row["issue_number"],
        pr_number=row["pr_number"],
        merge_sha=row["merge_sha"] or "",
        status=row["status"] or "registered",
        comment_posted=bool(row["comment_posted"]),
        created_at=from_epoch(row["created_at"]),
    )


def require_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskNotFound(task_id)
    return row


def write_audit(
    conn: sqlite3.Connection,
    now_epoch: float,
    *,
    task_id: str,
    event: str,
    agent_id: str = "",
    instance_id: str = "",
    attempt_id: str = "",
    fencing_token: int = 0,
    detail: Optional[Mapping[str, Any]] = None,
) -> None:
    """写一条审计流水（与业务写操作同一事务，事务回滚则审计也不留）。"""
    conn.execute(
        "INSERT INTO audit(task_id, event, agent_id, instance_id, attempt_id,"
        " fencing_token, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            task_id,
            event,
            agent_id,
            instance_id,
            attempt_id,
            int(fencing_token),
            dumps_json(dict(detail or {})),
            now_epoch,
        ),
    )


def audit_entry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "task_id": row["task_id"],
        "event": row["event"],
        "agent_id": row["agent_id"],
        "instance_id": row["instance_id"],
        "attempt_id": row["attempt_id"],
        "fencing_token": int(row["fencing_token"] or 0),
        "detail": loads_json(row["detail"]),
        "created_at": from_epoch(row["created_at"]),
    }

