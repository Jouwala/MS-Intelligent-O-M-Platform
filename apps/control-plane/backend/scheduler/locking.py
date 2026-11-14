"""范围锁：与租约绑定、过期自动失效、旧代次不能释放新锁（TASK-04）。

- `acquire_scope` 先校验租约**有效且未过期**，再与其它「有效租约」持有的范围比对；
  过期租约的锁即使还没被 reap 也不参与冲突（`JOIN leases ... expires_at > now`），
  这样 Worker 崩溃后不需要等回收进程就能继续。
- 锁行主键是 task_id（每任务至多一把锁），并通过 attempt_id + fencing_token 与
  当前租约绑定；claim 新代次 / release / reap 都会同步删除旧锁。
- 路径规范化与重叠判定见 `scopes.py`（反斜杠、`.`/`..`、大小写、路径穿越）。
"""

from __future__ import annotations

from typing import Sequence

from .api import ScopeConflict
from .common import dumps_json, loads_scopes, require_task, write_audit
from .scopes import normalize_scopes, scopes_overlap
from .store import SQLiteStore  # noqa: F401


class ScopeMixin:
    """`SchedulerService` 的范围锁部分。"""

    store: "SQLiteStore"
    clock: object
    case_insensitive_scopes: bool

    def acquire_scope(
        self,
        task_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        scopes: Sequence[str],
    ) -> None:
        fold = self.case_insensitive_scopes
        normalized = normalize_scopes(scopes, casefold=fold)  # 非法路径在这里直接 ValueError
        if not normalized:
            raise ValueError("acquire_scope 需要至少一个文件范围")
        now_epoch = self._now_epoch()
        with self.store.transaction() as conn:
            # 过期租约不允许再申请锁（它随时可能被别人重新认领）
            lease = self._require_lease(
                conn, task_id, attempt_id, fencing_token, allow_expired=False
            )
            task = require_task(conn, task_id)
            holders = conn.execute(
                "SELECT l.task_id, l.agent_id, l.scopes FROM scope_locks l"
                " JOIN leases le ON le.task_id = l.task_id"
                " WHERE l.task_id != ? AND le.expires_at > ?"
                " ORDER BY l.task_id",
                (task_id, now_epoch),
            ).fetchall()
            for holder in holders:
                overlap = scopes_overlap(
                    normalized, loads_scopes(holder["scopes"]), casefold=fold
                )
                if overlap:
                    raise ScopeConflict(
                        task_id, holder["task_id"], holder["agent_id"], overlap
                    )
            conn.execute(
                "INSERT INTO scope_locks(task_id, attempt_id, agent_id, instance_id,"
                " fencing_token, scopes, acquired_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(task_id) DO UPDATE SET attempt_id=excluded.attempt_id,"
                " agent_id=excluded.agent_id, instance_id=excluded.instance_id,"
                " fencing_token=excluded.fencing_token, scopes=excluded.scopes,"
                " acquired_at=excluded.acquired_at",
                (
                    task_id,
                    attempt_id,
                    lease["agent_id"],
                    lease["instance_id"],
                    int(fencing_token),
                    dumps_json(list(normalized)),
                    now_epoch,
                ),
            )
            # 任务登记的范围也刷新成最新一次申请（供调度侧查重/展示）
            conn.execute(
                "UPDATE tasks SET scopes = ?, updated_at = ? WHERE task_id = ?",
                (dumps_json(list(normalized)), now_epoch, task_id),
            )
            write_audit(
                conn,
                now_epoch,
                task_id=task_id,
                event="acquire_scope",
                agent_id=lease["agent_id"],
                instance_id=lease["instance_id"],
                attempt_id=attempt_id,
                fencing_token=int(fencing_token),
                detail={"scopes": list(normalized), "task_state": task["state"]},
            )

    def release_scope(
        self, task_id: str, *, attempt_id: str, fencing_token: int
    ) -> bool:
        now_epoch = self._now_epoch()
        with self.store.transaction() as conn:
            lease = self._require_lease(conn, task_id, attempt_id, fencing_token)
            cursor = conn.execute("DELETE FROM scope_locks WHERE task_id = ?", (task_id,))
            removed = cursor.rowcount > 0
            if removed:
                write_audit(
                    conn,
                    now_epoch,
                    task_id=task_id,
                    event="release_scope",
                    agent_id=lease["agent_id"],
                    instance_id=lease["instance_id"],
                    attempt_id=attempt_id,
                    fencing_token=int(fencing_token),
                    detail={},
                )
            return removed

    def scope_holders(self) -> dict[str, tuple[str, tuple[str, ...]]]:
        """当前有效范围锁：task_id -> (agent_id, scopes)。过期租约的锁不返回。"""
        now_epoch = self._now_epoch()
        rows = self.store.all(
            "SELECT l.task_id, l.agent_id, l.scopes FROM scope_locks l"
            " JOIN leases le ON le.task_id = l.task_id"
            " WHERE le.expires_at > ? ORDER BY l.task_id",
            (now_epoch,),
        )
        return {
            row["task_id"]: (row["agent_id"], loads_scopes(row["scopes"])) for row in rows
        }
