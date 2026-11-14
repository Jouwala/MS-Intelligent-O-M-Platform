"""调度器服务：`Scheduler` 协议的唯一实现（组合 leasing / locking / completion）。

构造
----
``SchedulerService(db, *, clock=utcnow, lease_ttl=10min, heartbeat_ttl=90s)``

- ``db``：数据库文件路径 / `SQLiteStore` / ``None``（走 `store.default_db_path()`，
  即 `AGENT_SCHEDULER_DB` 或 `<repo>/TMP/scheduler/scheduler.db`）。
- ``clock``：可注入假时钟（测试），返回 aware datetime；内部统一按 UTC epoch 存。
- 并发模型：**一线程/一进程一个实例**；多实例连同一个数据库文件由 SQLite
  WAL + BEGIN IMMEDIATE 保证串行化。

职责划分
--------
| 方法组 | 所在文件 |
| --- | --- |
| 认领 / 续租 / 迁移 / 释放 / 回收 | `leasing.py`（LeaseMixin） |
| 范围锁 | `locking.py`（ScopeMixin） |
| 幂等完成 + outbox | `completion.py`（CompletionMixin） |
| 任务登记 / Worker 注册 / 取消 / 分派幂等 / 审计 | 本文件 |
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

from .api import (
    CANCEL_REQUESTED,
    CANCELLED,
    DEFAULT_HEARTBEAT_TTL,
    DEFAULT_LEASE_TTL,
    QUEUED,
    TERMINAL_STATES,
    TRANSITIONS,
    DispatchRecord,
    InvalidTransition,
    SchedulerError,
    TaskNotFound,
    TaskSnapshot,
    WorkerSnapshot,
    assert_transition,
    from_epoch,
    utcnow,
)
from .common import (
    audit_entry,
    dispatch_snapshot,
    dumps_json,
    require_task,
    task_snapshot,
    worker_snapshot,
    write_audit,
)
from .completion import CompletionMixin
from .leasing import LeaseMixin
from .locking import ScopeMixin
from .scopes import normalize_scopes
from .store import SQLiteStore


class SchedulerService(LeaseMixin, ScopeMixin, CompletionMixin):
    """中央调度器实现（满足 `api.Scheduler` 协议）。"""

    def __init__(
        self,
        db: Union[str, Path, SQLiteStore, None] = None,
        *,
        clock=utcnow,
        lease_ttl: timedelta = DEFAULT_LEASE_TTL,
        heartbeat_ttl: timedelta = DEFAULT_HEARTBEAT_TTL,
        case_insensitive_scopes: bool = False,
    ) -> None:
        if isinstance(db, SQLiteStore):
            self.store = db
            self._owns_store = False
        else:
            self.store = SQLiteStore(db)
            self._owns_store = True
        self.clock = clock
        self.lease_ttl = lease_ttl
        self.heartbeat_ttl = heartbeat_ttl
        # ADR-4：范围大小写默认敏感（仓库文件系统语义）；Windows 共享盘等场景可选关闭
        self.case_insensitive_scopes = case_insensitive_scopes

    # -- 生命周期 ------------------------------------------------------------
    def close(self) -> None:
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> "SchedulerService":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 任务登记 ------------------------------------------------------------
    def upsert_task(
        self,
        task_id: str,
        issue_number: int,
        repo: str = "",
        title: str = "",
        scopes: Sequence[str] = (),
    ) -> TaskSnapshot:
        """幂等登记任务。

        新任务建为 queued；**已存在任务只刷新元数据**，绝不重置 state /
        fencing_token / attempt / 租约（Worker 每次发现任务都会 upsert 一次）。
        `scopes` 为空时不覆盖既有范围（Issue 正文暂时没解析出文件不代表清空）。
        """
        if not task_id:
            raise ValueError("task_id 不能为空")
        normalized_scopes = (
            tuple(normalize_scopes(scopes, casefold=self.case_insensitive_scopes))
            if scopes
            else ()
        )
        now_epoch = self._now_epoch()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO tasks(task_id, issue_number, repo, title, state, scopes,"
                    " fencing_token, updated_at) VALUES (?,?,?,?,?,?,0,?)",
                    (
                        task_id,
                        int(issue_number),
                        repo,
                        title,
                        QUEUED,
                        dumps_json(list(normalized_scopes)),
                        now_epoch,
                    ),
                )
                event = "register_task"
            else:
                conn.execute(
                    "UPDATE tasks SET issue_number = ?, repo = ?, title = ?,"
                    " scopes = CASE WHEN ? = 1 THEN ? ELSE scopes END, updated_at = ?"
                    " WHERE task_id = ?",
                    (
                        int(issue_number),
                        repo or row["repo"],
                        title or row["title"],
                        1 if normalized_scopes else 0,
                        dumps_json(list(normalized_scopes)),
                        now_epoch,
                        task_id,
                    ),
                )
                event = "upsert_task"
            write_audit(
                conn,
                now_epoch,
                task_id=task_id,
                event=event,
                detail={"issue_number": int(issue_number), "repo": repo, "title": title},
            )
            row = require_task(conn, task_id)
        return task_snapshot(row)

    def get_task(self, task_id: str) -> TaskSnapshot:
        row = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if row is None:
            raise TaskNotFound(task_id)
        return task_snapshot(row)

    # -- Worker 注册与心跳 ----------------------------------------------------
    def register_worker(
        self,
        agent_id: str,
        instance_id: str,
        hostname: str = "",
        platform: str = "",
        capabilities: Sequence[str] = (),
    ) -> WorkerSnapshot:
        if not agent_id or not instance_id:
            raise ValueError("register_worker 需要 agent_id 与 instance_id")
        now_epoch = self._now_epoch()
        caps = dumps_json(list(capabilities))
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO workers(agent_id, instance_id, hostname, platform,"
                " capabilities, state, last_heartbeat_at, registered_at)"
                " VALUES (?,?,?,?,?,'active',?,?)"
                " ON CONFLICT(agent_id, instance_id) DO UPDATE SET"
                " hostname=excluded.hostname, platform=excluded.platform,"
                " capabilities=excluded.capabilities, state='active',"
                " last_heartbeat_at=excluded.last_heartbeat_at",
                (agent_id, instance_id, hostname, platform, caps, now_epoch, now_epoch),
            )
            row = conn.execute(
                "SELECT * FROM workers WHERE agent_id = ? AND instance_id = ?",
                (agent_id, instance_id),
            ).fetchone()
        assert row is not None
        return worker_snapshot(row)

    def heartbeat_worker(self, agent_id: str, instance_id: str) -> bool:
        now_epoch = self._now_epoch()
        with self.store.transaction() as conn:
            cursor = conn.execute(
                "UPDATE workers SET last_heartbeat_at = ?, state = 'active'"
                " WHERE agent_id = ? AND instance_id = ?",
                (now_epoch, agent_id, instance_id),
            )
            return cursor.rowcount > 0

    def list_workers(
        self, stale_after: timedelta = DEFAULT_HEARTBEAT_TTL
    ) -> list[WorkerSnapshot]:
        """列出全部已注册 Worker；`state` 按最后心跳计算为 active / stale。"""
        now = self._now()
        rows = self.store.all("SELECT * FROM workers ORDER BY agent_id, instance_id")
        result: list[WorkerSnapshot] = []
        for row in rows:
            last = from_epoch(row["last_heartbeat_at"])
            state = "unknown"
            if last is not None:
                state = "active" if now - last <= stale_after else "stale"
            result.append(worker_snapshot(row, state=state))
        return result

    # -- 取消 ----------------------------------------------------------------
    def request_cancel(self, task_id: str, *, reason: str = "") -> TaskSnapshot:
        """请求取消：有租约 → cancel_requested（等持有者 ack）；无租约 → 直接 cancelled。"""
        now_epoch = self._now_epoch()
        with self.store.transaction() as conn:
            task = require_task(conn, task_id)
            current = task["state"]
            if current in TERMINAL_STATES:
                return task_snapshot(task)  # 幂等：已结束就返回现状
            lease = self._load_lease(conn, task_id)
            target = None
            if lease is not None and CANCEL_REQUESTED in TRANSITIONS.get(
                current, frozenset()
            ):
                target = CANCEL_REQUESTED
            elif CANCELLED in TRANSITIONS.get(current, frozenset()):
                target = CANCELLED
            else:
                # EXPIRED 之类不允许直接取消：先回落 QUEUED 再取消（同事务两步）
                assert_transition(current, QUEUED)
                assert_transition(QUEUED, CANCELLED)
                target = CANCELLED
            terminal = target in TERMINAL_STATES
            conn.execute(
                "UPDATE tasks SET state = ?, terminal_status = ?, completed_at = ?,"
                " cancel_requested_at = COALESCE(cancel_requested_at, ?), updated_at = ?"
                " WHERE task_id = ?",
                (
                    target,
                    target if terminal else None,
                    now_epoch if terminal else task["completed_at"],
                    now_epoch,
                    now_epoch,
                    task_id,
                ),
            )
            if terminal:
                self._purge_lease(conn, task_id)
                self._end_attempt(conn, task["attempt_id"], target, now_epoch)
            write_audit(
                conn,
                now_epoch,
                task_id=task_id,
                event="request_cancel",
                detail={"from": current, "to": target, "reason": reason},
            )
            row = require_task(conn, task_id)
        return task_snapshot(row)

    def is_cancel_requested(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        return task.state == CANCEL_REQUESTED

    def ack_cancel(
        self, task_id: str, *, attempt_id: str, fencing_token: int
    ) -> TaskSnapshot:
        now_epoch = self._now_epoch()
        with self.store.transaction() as conn:
            lease = self._require_lease(conn, task_id, attempt_id, fencing_token)
            task = require_task(conn, task_id)
            current = task["state"]
            if current != CANCEL_REQUESTED:
                raise InvalidTransition(current, CANCELLED)
            assert_transition(current, CANCELLED)
            conn.execute(
                "UPDATE tasks SET state = ?, terminal_status = ?, completed_at = ?,"
                " updated_at = ? WHERE task_id = ?",
                (CANCELLED, CANCELLED, now_epoch, now_epoch, task_id),
            )
            self._purge_lease(conn, task_id)
            self._end_attempt(conn, lease["attempt_id"], CANCELLED, now_epoch)
            write_audit(
                conn,
                now_epoch,
                task_id=task_id,
                event="ack_cancel",
                agent_id=lease["agent_id"],
                instance_id=lease["instance_id"],
                attempt_id=attempt_id,
                fencing_token=int(fencing_token),
                detail={},
            )
            row = require_task(conn, task_id)
        return task_snapshot(row)

    # -- 分派幂等 ------------------------------------------------------------
    def register_dispatch(
        self,
        dispatch_key: str,
        *,
        issue_number: Optional[int] = None,
        pr_number: Optional[int] = None,
        merge_sha: str = "",
    ) -> bool:
        """原子登记一次分派；主键冲突即重复分派，返回 False。"""
        if not dispatch_key:
            raise ValueError("dispatch_key 不能为空")
        now_epoch = self._now_epoch()
        with self.store.transaction() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO dispatches(dispatch_key, issue_number, pr_number,"
                " merge_sha, status, comment_posted, created_at, updated_at)"
                " VALUES (?,?,?,?,'registered',0,?,?)",
                (dispatch_key, issue_number, pr_number, merge_sha, now_epoch, now_epoch),
            )
            return cursor.rowcount == 1

    def dispatch_state(self, dispatch_key: str) -> Optional[DispatchRecord]:
        row = self.store.one(
            "SELECT * FROM dispatches WHERE dispatch_key = ?", (dispatch_key,)
        )
        return dispatch_snapshot(row) if row is not None else None

    def set_dispatch_status(
        self, dispatch_key: str, status: str, *, comment_posted: Optional[bool] = None
    ) -> None:
        now_epoch = self._now_epoch()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM dispatches WHERE dispatch_key = ?", (dispatch_key,)
            ).fetchone()
            if row is None:
                raise SchedulerError(f"分派记录不存在：{dispatch_key}")
            posted = row["comment_posted"] if comment_posted is None else int(comment_posted)
            conn.execute(
                "UPDATE dispatches SET status = ?, comment_posted = ?, updated_at = ?"
                " WHERE dispatch_key = ?",
                (status, posted, now_epoch, dispatch_key),
            )

    # -- 审计 ----------------------------------------------------------------
    def list_audit(self, task_id: str, limit: int = 200) -> list[Mapping[str, Any]]:
        if limit <= 0:
            return []
        rows = self.store.all(
            "SELECT * FROM audit WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, int(limit)),
        )
        return [audit_entry(row) for row in rows]
