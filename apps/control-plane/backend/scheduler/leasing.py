"""租约 / fencing / 状态迁移 / 过期回收（TASK-02，兼 TASK-06 的调度侧）。

核心安全规则
------------
- `claim` 在**一个** `BEGIN IMMEDIATE` 事务内完成「检查租约 → 代次 +1 → 写租约 →
  记尝试 → 改状态」；两个并发实例只可能有一个看到「无有效租约/租约过期」。
- 其余写操作（renew / transition / release / release_scope / ack_cancel / complete）
  都必须提交 `attempt_id + fencing_token`，与 `leases` 行不符一律 `StaleFencing`：
  旧执行者既覆盖不了也释放不了新执行者的状态与锁。
- `reap_expired` 是补偿路径：租约过期后由任意一方调用，状态回落到合法可认领态、
  同步删除租约与范围锁，并写审计；重复调用幂等。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from .api import (
    CANCEL_REQUESTED,
    CANCELLED,
    CLAIMABLE_STATES,
    CLAIMED,
    BLOCKED,
    EXPIRED,
    QUEUED,
    RETRYABLE,
    TERMINAL_STATES,
    TRANSITIONS,
    AlreadyTerminal,
    InvalidTransition,
    Lease,
    LeaseHeld,
    ReapReport,
    StaleFencing,
    TaskSnapshot,
    assert_transition,
    from_epoch,
    to_epoch,
)
from .common import lease_snapshot, require_task, task_snapshot, write_audit
from .store import SQLiteStore  # noqa: F401  （仅类型说明用，避免循环导入）


def _as_utc(dt: datetime) -> datetime:
    """clock 返回值统一成 aware UTC（测试里的 FakeClock 也应给 aware，这里兜底）。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class LeaseMixin:
    """`SchedulerService` 的租约/迁移部分；依赖 self.store / self.clock / self.lease_ttl。"""

    # 由 SchedulerService.__init__ 注入
    store: "SQLiteStore"
    clock: object
    lease_ttl: timedelta

    # -- 时间与行读取 --------------------------------------------------------
    def _now(self) -> datetime:
        return _as_utc(self.clock())  # type: ignore[operator]

    def _now_epoch(self) -> float:
        return to_epoch(self._now())

    @staticmethod
    def _load_lease(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
        return conn.execute("SELECT * FROM leases WHERE task_id = ?", (task_id,)).fetchone()

    def _require_lease(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        attempt_id: str,
        fencing_token: int,
        *,
        allow_expired: bool = True,
        reason: str = "",
    ) -> sqlite3.Row:
        """校验执行者身份：租约存在、attempt 与代次都与当前行一致。

        不符抛 `StaleFencing`（带上当前值，便于 Worker 记日志/放弃）。
        `allow_expired=False` 时额外要求租约未过期（范围锁用：过期锁自动失效）。
        """
        task = require_task(conn, task_id)
        lease = self._load_lease(conn, task_id)
        if lease is None:
            raise StaleFencing(
                task_id,
                attempt_id,
                int(fencing_token),
                task["attempt_id"] or "",
                int(task["fencing_token"] or 0),
                reason or "没有有效租约（已过期回收或已完成）",
            )
        if lease["attempt_id"] != attempt_id or int(lease["fencing_token"]) != int(fencing_token):
            raise StaleFencing(
                task_id,
                attempt_id,
                int(fencing_token),
                lease["attempt_id"],
                int(lease["fencing_token"]),
                reason,
            )
        if not allow_expired and self._now() >= (from_epoch(lease["expires_at"]) or self._now()):
            raise StaleFencing(
                task_id,
                attempt_id,
                int(fencing_token),
                lease["attempt_id"],
                int(lease["fencing_token"]),
                reason or "租约已过期",
            )
        return lease

    @staticmethod
    def _purge_lease(conn: sqlite3.Connection, task_id: str) -> None:
        """删除租约并同步释放它的范围锁（锁与租约同生共死）。

        同时清空 `tasks` 上的租约摘要字段：快照里「终态/已释放」的任务不应再显示
        `lease_expire_at`（调用方据此判断是否还持有租约）。
        """
        conn.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM scope_locks WHERE task_id = ?", (task_id,))
        conn.execute(
            "UPDATE tasks SET lease_expire_at = NULL, heartbeat_at = NULL WHERE task_id = ?",
            (task_id,),
        )

    @staticmethod
    def _end_attempt(
        conn: sqlite3.Connection, attempt_id: str, state: str, now_epoch: float
    ) -> None:
        if not attempt_id:
            return
        conn.execute(
            "UPDATE attempts SET state = ?, ended_at = ?"
            " WHERE attempt_id = ? AND ended_at IS NULL",
            (state, now_epoch, attempt_id),
        )

    # -- 认领 ----------------------------------------------------------------
    def claim(
        self,
        task_id: str,
        *,
        agent_id: str,
        instance_id: str,
        attempt_id: str,
        lease_ttl: Optional[timedelta] = None,
    ) -> Lease:
        if not agent_id or not instance_id or not attempt_id:
            raise ValueError("claim 需要 agent_id / instance_id / attempt_id 均非空")
        ttl = lease_ttl or self.lease_ttl
        if ttl.total_seconds() <= 0:
            raise ValueError("lease_ttl 必须为正")
        now = self._now()
        now_epoch = to_epoch(now)
        with self.store.transaction() as conn:
            task = require_task(conn, task_id)
            if task["state"] in TERMINAL_STATES:
                raise AlreadyTerminal(task_id, task["state"])
            lease = self._load_lease(conn, task_id)
            expired = False
            if lease is not None:
                expires_at = from_epoch(lease["expires_at"])
                if expires_at is not None and now < expires_at:
                    same_instance = (
                        lease["agent_id"] == agent_id
                        and lease["instance_id"] == instance_id
                    )
                    if not same_instance:
                        raise LeaseHeld(task_id, lease["agent_id"], lease["instance_id"], expires_at)
                else:
                    expired = True  # 租约已过期：允许接管（状态回落到 claimed）
            current = task["state"]
            # 已请求取消但还没被 ack 的任务不允许被旧租约过期后的新实例「复活」
            if current == CANCEL_REQUESTED and expired:
                raise InvalidTransition(current, CLAIMED)
            # 没有过期租约可接管时，只允许从可认领状态进入 claimed。
            # 有已过期租约时按契约直接接管（崩溃/失联后不必等 reap 也能重新分派）。
            if not expired and current not in CLAIMABLE_STATES and current != CLAIMED:
                assert_transition(current, CLAIMED)

            token = int(task["fencing_token"] or 0) + 1
            expires_epoch = to_epoch(now + ttl)
            if lease is not None:
                # 旧尝试作废（同实例重认领 / 过期后重认领）
                self._end_attempt(conn, lease["attempt_id"], "superseded", now_epoch)
            # 旧租约的范围锁随旧租约失效
            conn.execute("DELETE FROM scope_locks WHERE task_id = ?", (task_id,))
            conn.execute(
                "INSERT INTO leases(task_id, attempt_id, agent_id, instance_id, fencing_token,"
                " claimed_at, expires_at, heartbeat_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(task_id) DO UPDATE SET attempt_id=excluded.attempt_id,"
                " agent_id=excluded.agent_id, instance_id=excluded.instance_id,"
                " fencing_token=excluded.fencing_token, claimed_at=excluded.claimed_at,"
                " expires_at=excluded.expires_at, heartbeat_at=excluded.heartbeat_at",
                (
                    task_id,
                    attempt_id,
                    agent_id,
                    instance_id,
                    token,
                    now_epoch,
                    expires_epoch,
                    now_epoch,
                ),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id, task_id, agent_id, instance_id,"
                " fencing_token, state, started_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(attempt_id) DO UPDATE SET task_id=excluded.task_id,"
                " agent_id=excluded.agent_id, instance_id=excluded.instance_id,"
                " fencing_token=excluded.fencing_token, state=excluded.state,"
                " started_at=excluded.started_at, ended_at=NULL",
                (attempt_id, task_id, agent_id, instance_id, token, "running", now_epoch),
            )
            conn.execute(
                "UPDATE tasks SET state = ?, fencing_token = ?, attempt_id = ?, agent_id = ?,"
                " instance_id = ?, lease_expire_at = ?, heartbeat_at = ?,"
                " started_at = COALESCE(started_at, ?), updated_at = ? WHERE task_id = ?",
                (
                    CLAIMED,
                    token,
                    attempt_id,
                    agent_id,
                    instance_id,
                    expires_epoch,
                    now_epoch,
                    now_epoch,
                    now_epoch,
                    task_id,
                ),
            )
            write_audit(
                conn,
                now_epoch,
                task_id=task_id,
                event="claim",
                agent_id=agent_id,
                instance_id=instance_id,
                attempt_id=attempt_id,
                fencing_token=token,
                detail={"lease_expires_at": expires_epoch, "lease_ttl_s": ttl.total_seconds()},
            )
            row = self._load_lease(conn, task_id)
        assert row is not None
        return lease_snapshot(row)

    # -- 续租 ----------------------------------------------------------------
    def renew(
        self,
        task_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        lease_ttl: Optional[timedelta] = None,
    ) -> Lease:
        ttl = lease_ttl or self.lease_ttl
        now = self._now()
        now_epoch = to_epoch(now)
        with self.store.transaction() as conn:
            self._require_lease(conn, task_id, attempt_id, fencing_token)
            conn.execute(
                "UPDATE leases SET expires_at = ?, heartbeat_at = ? WHERE task_id = ?",
                (to_epoch(now + ttl), now_epoch, task_id),
            )
            conn.execute(
                "UPDATE tasks SET lease_expire_at = ?, heartbeat_at = ?, updated_at = ?"
                " WHERE task_id = ?",
                (to_epoch(now + ttl), now_epoch, now_epoch, task_id),
            )
            write_audit(
                conn,
                now_epoch,
                task_id=task_id,
                event="renew",
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                detail={"lease_ttl_s": ttl.total_seconds()},
            )
            row = self._load_lease(conn, task_id)
        assert row is not None
        return lease_snapshot(row)

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
        now_epoch = self._now_epoch()
        with self.store.transaction() as conn:
            lease = self._require_lease(conn, task_id, attempt_id, fencing_token)
            task = require_task(conn, task_id)
            current = task["state"]
            assert_transition(current, state)
            terminal = state in TERMINAL_STATES
            completed_at = now_epoch if terminal else task["completed_at"]
            cancel_at = (
                now_epoch
                if state == CANCEL_REQUESTED and task["cancel_requested_at"] is None
                else task["cancel_requested_at"]
            )
            conn.execute(
                "UPDATE tasks SET state = ?, terminal_status = ?, completed_at = ?,"
                " error_code = ?, error_message = ?, cancel_requested_at = ?, updated_at = ?"
                " WHERE task_id = ?",
                (
                    state,
                    state if terminal else None,
                    completed_at,
                    error_code,
                    error_message,
                    cancel_at,
                    now_epoch,
                    task_id,
                ),
            )
            # BLOCKED 保留租约（等依赖期间仍归当前执行者）；其余非 claim 态释放租约
            if state != CLAIMED and state != CANCEL_REQUESTED and state != BLOCKED:
                if terminal or state in (RETRYABLE, EXPIRED):
                    self._purge_lease(conn, task_id)
                    self._end_attempt(conn, lease["attempt_id"], state, now_epoch)
            write_audit(
                conn,
                now_epoch,
                task_id=task_id,
                event="transition",
                agent_id=lease["agent_id"],
                instance_id=lease["instance_id"],
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                detail={"from": current, "to": state, "error_code": error_code},
            )
            row = require_task(conn, task_id)
        return task_snapshot(row)

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
        now_epoch = self._now_epoch()
        with self.store.transaction() as conn:
            lease = self._require_lease(conn, task_id, attempt_id, fencing_token)
            task = require_task(conn, task_id)
            current = task["state"]
            if terminal_state is not None:
                assert_transition(current, terminal_state)
                target = terminal_state
                terminal = True
            else:
                # 主动放弃：回落到可被再次认领的状态。按状态机优先 RETRYABLE，其次 QUEUED。
                target = current
                for candidate in (RETRYABLE, QUEUED):
                    try:
                        assert_transition(current, candidate)
                    except InvalidTransition:
                        continue
                    target = candidate
                    break
                terminal = target in TERMINAL_STATES
            conn.execute(
                "UPDATE tasks SET state = ?, terminal_status = ?, completed_at = ?,"
                " updated_at = ? WHERE task_id = ?",
                (
                    target,
                    target if terminal else None,
                    now_epoch if terminal else task["completed_at"],
                    now_epoch,
                    task_id,
                ),
            )
            self._purge_lease(conn, task_id)
            self._end_attempt(conn, lease["attempt_id"], target, now_epoch)
            write_audit(
                conn,
                now_epoch,
                task_id=task_id,
                event="release",
                agent_id=lease["agent_id"],
                instance_id=lease["instance_id"],
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                detail={"from": current, "to": target, "terminal": terminal},
            )
            row = require_task(conn, task_id)
        return task_snapshot(row)

    # -- 过期回收 ------------------------------------------------------------
    @staticmethod
    def _reap_target(current: str) -> Optional[str]:
        for candidate in (EXPIRED, RETRYABLE, QUEUED):
            if current == candidate or candidate in TRANSITIONS.get(current, frozenset()):
                return candidate
        return None

    def reap_expired(self, now: Optional[datetime] = None) -> ReapReport:
        now_dt = _as_utc(now) if now is not None else self._now()
        now_epoch = to_epoch(now_dt)
        expired: list[str] = []
        retryable: list[str] = []
        released: list[str] = []
        orphans: list[str] = []
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT l.*, t.state AS task_state, t.terminal_status AS terminal_status"
                " FROM leases l JOIN tasks t ON t.task_id = l.task_id"
                " WHERE l.expires_at <= ? ORDER BY l.task_id",
                (now_epoch,),
            ).fetchall()
            for lease in rows:
                task_id = lease["task_id"]
                attempt = lease["attempt_id"]
                orphans.append(attempt)
                if lease["task_state"] in TERMINAL_STATES:
                    # 终态任务残留租约：只清理，不动状态（complete 事务异常的历史数据）
                    self._purge_lease(conn, task_id)
                    released.append(task_id)
                    write_audit(
                        conn,
                        now_epoch,
                        task_id=task_id,
                        event="reap_orphan_terminal",
                        attempt_id=attempt,
                        fencing_token=int(lease["fencing_token"]),
                        detail={"state": lease["task_state"]},
                    )
                    continue
                target = self._reap_target(lease["task_state"]) or lease["task_state"]
                # 已请求取消且租约过期的任务：持有者已失联，直接收敛到 cancelled，
                # 不能再回落到可认领态（否则取消请求会被新执行者「复活」）。
                if lease["task_state"] == CANCEL_REQUESTED:
                    target = CANCELLED
                terminal = target in TERMINAL_STATES
                self._purge_lease(conn, task_id)
                self._end_attempt(conn, attempt, "expired", now_epoch)
                conn.execute(
                    "UPDATE tasks SET state = ?, terminal_status = ?, completed_at = ?,"
                    " updated_at = ? WHERE task_id = ?",
                    (
                        target,
                        target if terminal else None,
                        now_epoch if terminal else None,
                        now_epoch,
                        task_id,
                    ),
                )
                expired.append(task_id)
                if not terminal:
                    retryable.append(task_id)
                released.append(task_id)
                write_audit(
                    conn,
                    now_epoch,
                    task_id=task_id,
                    event="reap_expired",
                    agent_id=lease["agent_id"],
                    instance_id=lease["instance_id"],
                    attempt_id=attempt,
                    fencing_token=int(lease["fencing_token"]),
                    detail={"from": lease["task_state"], "to": target},
                )
            # 清理「没有租约却仍带范围锁」的脏数据（异常中断的残留）
            stale_locks = conn.execute(
                "SELECT l.task_id FROM scope_locks l"
                " LEFT JOIN leases le ON le.task_id = l.task_id"
                " WHERE le.task_id IS NULL"
            ).fetchall()
            for row in stale_locks:
                conn.execute("DELETE FROM scope_locks WHERE task_id = ?", (row["task_id"],))
                if row["task_id"] not in released:
                    released.append(row["task_id"])
        return ReapReport(
            expired_leases=tuple(expired),
            released_locks=tuple(released),
            retryable=tuple(retryable),
            orphan_attempts=tuple(orphans),
        )
