"""中央调度器包（NOC-SOC 多 Worker 统一修复，ADR-2026-11-14）。

目录说明（每个文件 < 500 行，职责单一）：

| 文件 | 职责 |
| --- | --- |
| `api.py` | 冻结契约：类型、错误、状态机、Scheduler 协议（**唯一接口真相源**） |
| `store.py` | SQLite 持久化：schema、事务原语（`BEGIN IMMEDIATE`）、唯一约束 |
| `service.py` | 业务规则：原子认领、租约、fencing、范围锁、幂等完成、回收 |
| `mirror.py` | GitHub 回写：outbox 重放（评论 / 标签 / 审计），幂等且可收敛 |
| `__init__.py` | 导出上述公开名字 |

导入方式：`apps/agent-worker/_bootstrap.py` 的 `load_scheduler()` 负责把
`apps/control-plane/backend` 放入 `sys.path` 后按标准包导入（本包目录名合法，
只有上级 `control-plane` 带连字符，不能作为包路径）。
"""

from __future__ import annotations

from .api import (
    BLOCKED,
    CANCEL_REQUESTED,
    CANCELLED,
    CLAIMABLE_STATES,
    CLAIMED,
    COMPLETED,
    DEFAULT_HEARTBEAT_TTL,
    DEFAULT_LEASE_TTL,
    EXPIRED,
    FAILED,
    QUEUED,
    RETRYABLE,
    RUNNING,
    TERMINAL_STATES,
    TRANSITIONS,
    AlreadyTerminal,
    CompleteOutcome,
    DispatchRecord,
    InvalidTransition,
    Lease,
    LeaseHeld,
    OutboxOp,
    ReapReport,
    Scheduler,
    SchedulerError,
    ScopeConflict,
    StaleFencing,
    TaskNotFound,
    TaskSnapshot,
    WorkerSnapshot,
    assert_transition,
    from_epoch,
    iso,
    parse_iso,
    to_epoch,
    utcnow,
)

# 实现层按需导入，避免 `import scheduler` 时强依赖尚未完成的模块。
try:  # pragma: no cover - 实现层就绪后恒成立
    from .service import SchedulerService
except ImportError:  # pragma: no cover
    SchedulerService = None  # type: ignore[assignment]

try:  # pragma: no cover
    from .store import SQLiteStore
except ImportError:  # pragma: no cover
    SQLiteStore = None  # type: ignore[assignment]

try:  # pragma: no cover
    from .mirror import GitHubMirror
except ImportError:  # pragma: no cover
    GitHubMirror = None  # type: ignore[assignment]

__all__ = [
    "BLOCKED",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "CLAIMABLE_STATES",
    "CLAIMED",
    "COMPLETED",
    "DEFAULT_HEARTBEAT_TTL",
    "DEFAULT_LEASE_TTL",
    "EXPIRED",
    "FAILED",
    "GitHubMirror",
    "QUEUED",
    "RETRYABLE",
    "RUNNING",
    "SQLiteStore",
    "SchedulerService",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "AlreadyTerminal",
    "CompleteOutcome",
    "DispatchRecord",
    "InvalidTransition",
    "Lease",
    "LeaseHeld",
    "OutboxOp",
    "ReapReport",
    "Scheduler",
    "SchedulerError",
    "ScopeConflict",
    "StaleFencing",
    "TaskNotFound",
    "TaskSnapshot",
    "WorkerSnapshot",
    "assert_transition",
    "from_epoch",
    "iso",
    "parse_iso",
    "to_epoch",
    "utcnow",
]
