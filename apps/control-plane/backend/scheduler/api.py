"""中央调度器对外契约（冻结层）。

本文件是 `scheduler` 包的**唯一接口真相源**：类型、错误、状态机、服务协议。
`ref_service.py`（GitHub 原生权威实现）等（历史 SQLite 实现已于 PR-6 删除）
以及 `apps/agent-worker/` 里的 Worker 只依赖这里声明的名字，不互相 import。

设计依据：`docs/workspace/multi-worker-scheduler-design.md`（ADR-2026-11-14）。
关键约束：

- **原子性**：认领 / 续租 / 范围锁 / 完成 / 分派登记全部在数据库事务里完成
  （SQLite `BEGIN IMMEDIATE` + 唯一约束），不存在「先读后写」的检查时-执行时窗口。
- **实例身份**：`agent_id` 只是逻辑身份，`instance_id` 才是运行实例身份；
  `attempt_id` 是一次执行尝试；`fencing_token` 是任务级单调递增执行代次。
- **旧执行者防护**：任何状态修改都必须带上 `attempt_id + fencing_token`，
  代次不符一律 `StaleFencing`，旧实例不能覆盖或释放新实例的状态/锁。
- **幂等**：`complete()` 以 `idempotency_key` 去重，重复提交返回既有结果；
  中间步骤失败走 GitHub 侧有界重试 + Actions 对账，最终收敛到正确状态（无本地 outbox）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Protocol, Sequence

# ---------------------------------------------------------------------------
# 时间工具（全部 aware UTC；数据库存 epoch 秒，避免字符串比较歧义）
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_epoch(dt: datetime) -> float:
    return dt.astimezone(timezone.utc).timestamp()


def from_epoch(value: Optional[float]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 状态机：唯一真相源（service 校验、文档、测试共用）
# ---------------------------------------------------------------------------

QUEUED = "queued"
CLAIMED = "claimed"
RUNNING = "running"
CANCEL_REQUESTED = "cancel_requested"
COMPLETED = "completed"
FAILED = "failed"
BLOCKED = "blocked"
CANCELLED = "cancelled"
RETRYABLE = "retryable"
EXPIRED = "expired"

#: 终态：不再接受认领，也不会被「重试」改写。
TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELLED})

#: 合法状态迁移表。**任何未列出的迁移都必须被拒绝**（InvalidTransition）。
TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({CLAIMED, CANCELLED}),
    CLAIMED: frozenset({RUNNING, BLOCKED, RETRYABLE, EXPIRED, CANCELLED, CANCEL_REQUESTED}),
    RUNNING: frozenset(
        {COMPLETED, FAILED, BLOCKED, CANCEL_REQUESTED, RETRYABLE, EXPIRED}
    ),
    CANCEL_REQUESTED: frozenset({CANCELLED, FAILED, RETRYABLE}),
    BLOCKED: frozenset({CLAIMED, QUEUED, CANCELLED}),
    RETRYABLE: frozenset({CLAIMED, QUEUED, EXPIRED, CANCELLED}),
    EXPIRED: frozenset({CLAIMED, RETRYABLE, QUEUED}),
    # 终态：唯一允许的「重放」是幂等 complete 的重复提交，由 service 特判，
    # 不在这里开放迁移边；completed 之后再取消属于非法操作。
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}

#: 允许被重新认领的状态（过期 / 阻塞 / 可重试 / 已排队）。
CLAIMABLE_STATES = frozenset({QUEUED, RETRYABLE, EXPIRED, BLOCKED})


def assert_transition(current: str, target: str) -> None:
    """状态迁移合法性断言；非法迁移抛 InvalidTransition。"""
    if current == target:
        return
    allowed = TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise InvalidTransition(current, target)


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class SchedulerError(Exception):
    """调度器错误基类。"""


class TaskNotFound(SchedulerError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"任务不存在：{task_id}")
        self.task_id = task_id


class AlreadyTerminal(SchedulerError):
    def __init__(self, task_id: str, state: str) -> None:
        super().__init__(f"任务 {task_id} 已终止（{state}），不能再认领")
        self.task_id = task_id
        self.state = state


class LeaseHeld(SchedulerError):
    """任务已被另一个有效实例持有且租约未过期。"""

    def __init__(self, task_id: str, holder: str, instance: str, expires_at: datetime):
        super().__init__(
            f"任务 {task_id} 已被 {holder}/{instance} 持有，租约至 {iso(expires_at)}"
        )
        self.task_id = task_id
        self.holder = holder
        self.instance = instance
        self.expires_at = expires_at


class StaleFencing(SchedulerError):
    """执行代次（fencing token）或尝试与当前有效代次不符——旧执行者被拒绝。"""

    def __init__(
        self,
        task_id: str,
        presented_attempt: str,
        presented_token: int,
        current_attempt: str,
        current_token: int,
        reason: str = "",
    ) -> None:
        detail = f"（{reason}）" if reason else ""
        super().__init__(
            f"任务 {task_id} 拒绝旧执行者{detail}："
            f"提交 attempt={presented_attempt} token={presented_token}，"
            f"当前 attempt={current_attempt} token={current_token}"
        )
        self.task_id = task_id
        self.presented_attempt = presented_attempt
        self.presented_token = presented_token
        self.current_attempt = current_attempt
        self.current_token = current_token


class InvalidTransition(SchedulerError):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"非法状态迁移：{current} → {target}")
        self.current = current
        self.target = target


class ScopeConflict(SchedulerError):
    """文件范围与另一在途任务重叠。"""

    def __init__(self, task_id: str, holder_task_id: str, holder_agent: str, overlap: Sequence[str]):
        super().__init__(
            f"任务 {task_id} 的文件范围与 {holder_task_id}（{holder_agent}）重叠："
            f"{', '.join(overlap)}"
        )
        self.task_id = task_id
        self.holder_task_id = holder_task_id
        self.holder_agent = holder_agent
        self.overlap = tuple(overlap)


# ---------------------------------------------------------------------------
# 数据快照（不可变；service/store 只返回这些，不外泄 ORM/连接）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lease:
    task_id: str
    attempt_id: str
    agent_id: str
    instance_id: str
    fencing_token: int
    claimed_at: datetime
    expires_at: datetime
    heartbeat_at: datetime

    def is_live(self, now: datetime) -> bool:
        return now < self.expires_at


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    issue_number: int
    repo: str
    title: str = ""
    state: str = QUEUED
    terminal_status: Optional[str] = None
    fencing_token: int = 0
    attempt_id: str = ""
    agent_id: str = ""
    instance_id: str = ""
    lease_expire_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_code: str = ""
    error_message: str = ""
    idempotency_key: str = ""
    scopes: tuple[str, ...] = ()
    cancel_requested_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass(frozen=True)
class CompleteOutcome:
    """一次完成操作的结果；`created=False` 表示幂等重放，未产生新副作用。"""

    task_id: str
    terminal_status: str
    idempotency_key: str
    created: bool
    outbox_ops: int = 0


@dataclass(frozen=True)
class DispatchRecord:
    dispatch_key: str
    issue_number: Optional[int] = None
    pr_number: Optional[int] = None
    merge_sha: str = ""
    status: str = "registered"
    comment_posted: bool = False
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class ReapReport:
    expired_leases: tuple[str, ...] = ()
    released_locks: tuple[str, ...] = ()
    retryable: tuple[str, ...] = ()
    orphan_attempts: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutboxOp:
    op_id: int
    task_id: str
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    status: str = "pending"
    attempts: int = 0
    last_error: str = ""


@dataclass(frozen=True)
class WorkerSnapshot:
    agent_id: str
    instance_id: str
    hostname: str = ""
    platform: str = ""
    capabilities: tuple[str, ...] = ()
    state: str = "active"
    last_heartbeat_at: Optional[datetime] = None
    registered_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# 调度器服务协议（实现：ref_service.GitHubRefScheduler）
# ---------------------------------------------------------------------------

DEFAULT_LEASE_TTL = timedelta(minutes=10)
DEFAULT_HEARTBEAT_TTL = timedelta(seconds=90)


class Scheduler(Protocol):
    """中央调度器接口。所有写操作在一个数据库事务内完成。"""

    # -- 任务登记 -------------------------------------------------------------
    def upsert_task(
        self,
        task_id: str,
        issue_number: int,
        repo: str = "",
        title: str = "",
        scopes: Sequence[str] = (),
    ) -> TaskSnapshot:
        ...

    def get_task(self, task_id: str) -> TaskSnapshot:
        ...

    # -- Worker 注册与心跳（与任务租约是两条独立职责） --------------------------
    def register_worker(
        self,
        agent_id: str,
        instance_id: str,
        hostname: str = "",
        platform: str = "",
        capabilities: Sequence[str] = (),
    ) -> WorkerSnapshot:
        ...

    def heartbeat_worker(self, agent_id: str, instance_id: str) -> bool:
        ...

    def list_workers(self, stale_after: timedelta = DEFAULT_HEARTBEAT_TTL) -> list[WorkerSnapshot]:
        ...

    # -- 认领 / 续租 / 心跳 ----------------------------------------------------
    def claim(
        self,
        task_id: str,
        *,
        agent_id: str,
        instance_id: str,
        attempt_id: str,
        lease_ttl: Optional[timedelta] = None,
    ) -> Lease:
        ...

    def renew(
        self,
        task_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        lease_ttl: Optional[timedelta] = None,
    ) -> Lease:
        ...

    # -- 状态迁移 -------------------------------------------------------------
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
        ...

    def release(
        self,
        task_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        terminal_state: Optional[str] = None,
    ) -> TaskSnapshot:
        """释放租约（不产生完成事件）；terminal_state 非空时同时落到终态。"""
        ...

    # -- 文件范围锁 -----------------------------------------------------------
    def acquire_scope(
        self,
        task_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        scopes: Sequence[str],
    ) -> None:
        ...

    def release_scope(
        self, task_id: str, *, attempt_id: str, fencing_token: int
    ) -> bool:
        ...

    def scope_holders(self) -> dict[str, tuple[str, tuple[str, ...]]]:
        """当前有效范围锁：task_id -> (agent_id, scopes)。"""
        ...

    # -- 完成（幂等 + outbox） -------------------------------------------------
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
        ...

    # -- 取消 -----------------------------------------------------------------
    def request_cancel(self, task_id: str, *, reason: str = "") -> TaskSnapshot:
        ...

    def is_cancel_requested(self, task_id: str) -> bool:
        ...

    def ack_cancel(
        self, task_id: str, *, attempt_id: str, fencing_token: int
    ) -> TaskSnapshot:
        ...

    # -- 回收 / 对账 ----------------------------------------------------------
    def reap_expired(self, now: Optional[datetime] = None) -> ReapReport:
        ...

    def pending_outbox(self, limit: int = 100) -> list[OutboxOp]:
        ...

    def mark_outbox(self, op_id: int, *, status: str, error: str = "") -> None:
        ...

    # -- 分派幂等（auto-dispatch / QA 触发共用） --------------------------------
    def register_dispatch(
        self,
        dispatch_key: str,
        *,
        issue_number: Optional[int] = None,
        pr_number: Optional[int] = None,
        merge_sha: str = "",
    ) -> bool:
        """原子登记一次分派；返回 False 表示该 dispatch_key 已登记（重复分派）。"""
        ...

    def dispatch_state(self, dispatch_key: str) -> Optional[DispatchRecord]:
        ...

    def set_dispatch_status(
        self, dispatch_key: str, status: str, *, comment_posted: Optional[bool] = None
    ) -> None:
        ...

    # -- 审计 ----------------------------------------------------------------
    def list_audit(self, task_id: str, limit: int = 200) -> list[Mapping[str, Any]]:
        ...
