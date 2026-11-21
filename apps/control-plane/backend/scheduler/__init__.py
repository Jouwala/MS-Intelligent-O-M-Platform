"""中央调度器包（NOC-SOC 多 Worker 统一修复；ADR-7 GitHub 原生权威层）。

**权威唯一**（ADR-7 §零，owner 2026-11-14 定）：GitHub refs（ref-CAS）是唯一权威。
PR-5 起 Worker 切换到 GitHub 原生权威层，PR-6 删除 SQLite 权威实现（`store.py` /
`service.py` / `leasing.py` / `locking.py` / `completion.py` / `mirror.py` /
`common.py`），**不保留缓存或回退**——两个权威并存会产生状态分叉。

目录（每个文件 < 500 行，职责单一）：

| 文件 | 职责 |
| --- | --- |
| `api.py` | 冻结契约：类型、错误、状态机、Scheduler 协议（唯一接口真相源） |
| `ref_api.py` | GitHub 原生权威契约：ref 命名、租约存活、评论标记、RefStore 协议 |
| `github_refs.py` | `RestRefStore`：GitHub refs HTTP + 服务端原子 CAS |
| `ref_claim.py` | HWM CAS 认领、派生视图/快照 |
| `ref_fencing.py` | 代次校验、续租推进 token、迁移/释放/取消 |
| `ref_reap.py` | 过期租约回收（回收权 CAS，Actions cron 调用） |
| `ref_scope.py` | 文件范围锁（创建后校验 + 冲突回退 + 孤儿清理） |
| `ref_completion.py` | 幂等完成（评论标记 + 标签收敛） |
| `ref_workers.py` | Worker 注册表（注册表 Issue 评论） |
| `ref_service.py` | `GitHubRefScheduler`：`api.Scheduler` 的 GitHub 原生实现（组装） |
| `factory.py` | 工厂：`make_scheduler` / `make_ref_store`（**没有 sqlite 分支**） |
| `scopes.py` | 文件范围路径规范化（`ref_scope` 复用） |

导入方式：`apps/agent-worker/_bootstrap.py` 的 `load_scheduler()` 负责把
`apps/control-plane/backend` 放入 `sys.path` 后按标准包导入（本包目录名合法，
只有上级 `control-plane` 带连字符，不能作为包路径）。
"""

from __future__ import annotations

from . import factory, ref_api
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
from .factory import DEFAULT_REF_TTL, make_ref_store, make_scheduler
from .ref_service import GitHubRefScheduler

__all__ = [
    "BLOCKED",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "CLAIMABLE_STATES",
    "CLAIMED",
    "COMPLETED",
    "DEFAULT_HEARTBEAT_TTL",
    "DEFAULT_LEASE_TTL",
    "DEFAULT_REF_TTL",
    "EXPIRED",
    "FAILED",
    "GitHubRefScheduler",
    "QUEUED",
    "RETRYABLE",
    "RUNNING",
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
    "factory",
    "from_epoch",
    "iso",
    "make_ref_store",
    "make_scheduler",
    "parse_iso",
    "ref_api",
    "to_epoch",
    "utcnow",
]
