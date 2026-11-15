"""PR-5：`GitHubRefScheduler` —— `api.Scheduler` 的 GitHub 原生权威实现（组装）。

本文件只做**组装与外围职责**，生命周期逻辑都在按阶段拆分的模块里：

| 阶段 | 模块 | 职责 |
| --- | --- | --- |
| PR-1 | `github_refs.py` / `ref_claim.py` | RestRefStore（HTTP/原子创建）+ HWM CAS 认领 |
| PR-2 | `ref_fencing.py` | 代次校验、续租推进 token、迁移/释放/取消 |
| PR-3 | `ref_reap.py` | 过期回收 + 回收权 CAS（由 Actions cron 调用） |
| PR-4 | `ref_scope.py`（B2 实现） | 范围锁「创建后校验」/ 释放 / 孤儿清理 |
| PR-5 | `ref_completion.py` / 本文件 | 幂等完成 + 协议组装、注册表、分派、审计 |

**权威唯一**（ADR-7 §零）：本实现不依赖任何本地 DB/文件保存权威状态——refs 是
唯一权威，评论是派生/审计输入，进程内 `_known`/`_workers`/`_dispatches` 只是展示
缓存。SQLite 后端不参与裁决（后续 PR-6 删除）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Sequence

from . import ref_api, ref_reap
from .api import (
    DEFAULT_HEARTBEAT_TTL,
    EXPIRED,
    QUEUED,
    RETRYABLE,
    BLOCKED,
    DispatchRecord,
    OutboxOp,
    ReapReport,
    SchedulerError,
    TaskNotFound,
    TaskSnapshot,
    WorkerSnapshot,
    iso,
    utcnow,
)
from .github_refs import GitHubRefError
from .ref_completion import RefCompleter
from .ref_workers import DEFAULT_HEARTBEAT_POST_INTERVAL, WorkerRegistryMixin
from .ref_view import issue_number_for, task_id_of

#: 回收后仍可被重新认领的状态（用于 ReapReport.retryable）。
_REAP_RETRYABLE = frozenset({RETRYABLE, EXPIRED, QUEUED, BLOCKED})


class GitHubRefScheduler(RefCompleter, WorkerRegistryMixin):
    """GitHub 原生权威层（多 VM 共享同一仓库时同一任务只有一个有效执行者）。

    构造签名（B2 的 `factory.py` 依赖，已冻结）：
    ``GitHubRefScheduler(ref_store, transport, *, repo="", clock=utcnow,
    lease_ttl=DEFAULT_REF_LEASE_TTL, registry_issue=None,
    case_insensitive_scopes=False, ...)``
    """

    def __init__(
        self,
        ref_store: Any,
        transport: Any,
        *,
        repo: str = "",
        clock=utcnow,
        lease_ttl: timedelta = ref_api.DEFAULT_REF_LEASE_TTL,
        registry_issue: Optional[int] = None,
        case_insensitive_scopes: bool = False,
        heartbeat_ttl: timedelta = DEFAULT_HEARTBEAT_TTL,
        heartbeat_post_interval: timedelta = DEFAULT_HEARTBEAT_POST_INTERVAL,
        anchor_sha: str = "",
        anchor_branch: str = "main",
        claim_retries: int = ref_api.DEFAULT_CLAIM_RETRIES,
        label_retries: int = 2,
        reaper_instance: str = "",
    ) -> None:
        super().__init__(
            ref_store,
            transport,
            repo=repo,
            clock=clock,
            lease_ttl=lease_ttl,
            anchor_sha=anchor_sha,
            anchor_branch=anchor_branch,
            claim_retries=claim_retries,
            label_retries=label_retries,
            reaper_instance=reaper_instance,
        )
        self.registry_issue = int(registry_issue) if registry_issue is not None else None
        self.case_insensitive_scopes = bool(case_insensitive_scopes)
        self.heartbeat_ttl = heartbeat_ttl
        self.heartbeat_post_interval = heartbeat_post_interval
        #: 本进程展示元数据（upsert_task 写入；不是权威状态）。
        self._known: dict[str, dict[str, Any]] = {}
        #: Worker 注册表：无 registry issue 时本地 no-op；有则从评论派生。
        self._workers: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_heartbeat_post: dict[tuple[str, str], datetime] = {}
        self._dispatches: dict[str, DispatchRecord] = {}

    # -- 小工具 --------------------------------------------------------------
    def _meta(self, task_id: str) -> dict[str, Any]:
        return dict(self._known.get(task_id) or {})

    def _snapshot(
        self,
        task_id: str,
        *,
        issue_number: Optional[int] = None,
        repo: str = "",
        title: str = "",
        scopes: Sequence[str] = (),
    ) -> TaskSnapshot:
        meta = self._meta(task_id)
        issue = (
            int(issue_number)
            if issue_number is not None
            else int(meta.get("issue_number") or issue_number_for(task_id))
        )
        return self.snapshot(
            task_id,
            issue_number=issue,
            repo=repo or str(meta.get("repo") or self.repo),
            title=title or str(meta.get("title") or ""),
            scopes=scopes or tuple(meta.get("scopes") or ()),
        )

    # -- 任务登记（幂等，不写权威状态） ---------------------------------------
    def upsert_task(
        self,
        task_id: str,
        issue_number: int,
        repo: str = "",
        title: str = "",
        scopes: Sequence[str] = (),
    ) -> TaskSnapshot:
        if not task_id:
            raise ValueError("task_id 不能为空")
        issue = int(issue_number)
        meta = self._known.setdefault(task_id, {})
        meta["issue_number"] = issue
        meta["repo"] = repo or meta.get("repo") or self.repo
        meta["title"] = title or meta.get("title") or ""
        if scopes:
            meta["scopes"] = tuple(scopes)
        return self._snapshot(task_id, issue_number=issue)

    def get_task(self, task_id: str) -> TaskSnapshot:
        try:
            return self._snapshot(task_id)
        except SchedulerError:
            # 只有「Issue 确实不存在」才报 TaskNotFound；refs/网络错误原样抛出
            try:
                self.transport.get_issue(issue_number_for(task_id))
            except Exception as exc:  # noqa: BLE001 - 适配器抛 TransportError
                raise TaskNotFound(task_id) from exc
            raise

    # -- 范围锁（委托 ref_scope.py，B2 实现；PR-4） ---------------------------
    @staticmethod
    def _scope_module() -> Any:
        try:
            from . import ref_scope  # noqa: PLC0415 - PR-4 边界
        except ImportError as exc:  # pragma: no cover - 落地后不会走到
            raise SchedulerError(
                "ref 范围锁实现 ref_scope.py 尚未就绪（并发开发中），本方法暂不可用："
                f"{exc}"
            ) from exc
        return ref_scope

    def acquire_scope(
        self,
        task_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        scopes: Sequence[str],
    ) -> None:
        self._scope_module().acquire_locks(
            self.ref_store,
            self.transport,
            issue_number=issue_number_for(task_id),
            token=int(fencing_token),
            scopes=scopes,
            clock=self.clock,
            ttl=self.lease_ttl,
            task_id=task_id,
            attempt_id=attempt_id,
            casefold=self.case_insensitive_scopes,
        )

    def release_scope(self, task_id: str, *, attempt_id: str, fencing_token: int) -> bool:
        removed = self._scope_module().release_locks(
            self.ref_store,
            self.transport,
            issue_number=issue_number_for(task_id),
            token=int(fencing_token),
            scopes=None,
            clock=self.clock,
            ttl=self.lease_ttl,
            task_id=task_id,
            attempt_id=attempt_id,
            casefold=self.case_insensitive_scopes,
        )
        return int(removed) > 0

    def scope_holders(self) -> dict[str, tuple[str, tuple[str, ...]]]:
        return self._scope_module().scope_holders(
            self.ref_store, clock=self.clock, ttl=self.lease_ttl
        )

    # -- 回收（PR-3 的 ref_reap + PR-4 的孤儿 scope 清理） -------------------
    def reap_expired(self, now: Optional[datetime] = None) -> ReapReport:
        now_dt = now if now is not None else self.now()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=utcnow().tzinfo)
        records = ref_reap.reap_expired_leases(
            self.ref_store,
            int(now_dt.timestamp() * 1000),
            ref_api.ttl_ms(self.lease_ttl),
            reaper_instance=self.reaper_instance,
        )
        expired_tasks: list[str] = []
        retryable: list[str] = []
        orphans: list[str] = []
        for record in records:
            if record["outcome"] != "reaped":
                continue
            task_id = task_id_of(record["issue_number"])
            if task_id not in expired_tasks:
                expired_tasks.append(task_id)
            lease = ref_api.parse_lease(record["ref"])
            if lease is not None:
                orphans.append(lease.attempt_id)
        for task_id in expired_tasks:
            try:
                state = self.view(issue_number_for(task_id), task_id=task_id).state
            except SchedulerError:
                continue
            if state in _REAP_RETRYABLE:
                retryable.append(task_id)
        released: tuple[str, ...] = ()
        try:
            from . import ref_scope  # noqa: PLC0415
        except ImportError:
            ref_scope = None  # type: ignore[assignment]
        if ref_scope is not None:
            released = tuple(
                ref_scope.cleanup_orphans(self.ref_store, self.transport, clock=self.clock)
            )
        return ReapReport(
            expired_leases=tuple(expired_tasks),
            released_locks=released,
            retryable=tuple(retryable),
            orphan_attempts=tuple(orphans),
        )

    # -- outbox（GitHub 后端没有 outbox：refs 权威、评论由本类直接写） --------
    def pending_outbox(self, limit: int = 100) -> list[OutboxOp]:
        return []

    def mark_outbox(self, op_id: int, *, status: str, error: str = "") -> None:
        raise SchedulerError(
            "GitHub 原生权威后端没有 outbox（完成评论写失败会在 complete 重试时按幂等收敛）"
        )

    # -- 分派幂等（ref CAS；权威是 ref 是否存在） -----------------------------
    @staticmethod
    def _dispatch_ref(dispatch_key: str) -> str:
        import hashlib

        digest = hashlib.sha256(str(dispatch_key).encode("utf-8")).hexdigest()[:40]
        return f"{ref_api.DISPATCH_ROOT}/{digest}"

    def register_dispatch(
        self,
        dispatch_key: str,
        *,
        issue_number: Optional[int] = None,
        pr_number: Optional[int] = None,
        merge_sha: str = "",
    ) -> bool:
        if not dispatch_key:
            raise ValueError("dispatch_key 不能为空")
        ref = self._dispatch_ref(dispatch_key)
        try:
            created = bool(self.ref_store.create(ref, self.anchor()))
        except GitHubRefError:
            # 结果不确定：GET 消歧；ref 存在即「已登记」（安全侧，不重复分派）
            created = self.ref_store.get(ref) is None
        if dispatch_key not in self._dispatches:
            self._dispatches[dispatch_key] = DispatchRecord(
                dispatch_key=dispatch_key,
                issue_number=issue_number,
                pr_number=pr_number,
                merge_sha=merge_sha,
                status="registered",
                comment_posted=False,
                created_at=self.now(),
            )
        return created

    def dispatch_state(self, dispatch_key: str) -> Optional[DispatchRecord]:
        record = self._dispatches.get(dispatch_key)
        if record is not None:
            return record
        try:
            exists = self.ref_store.get(self._dispatch_ref(dispatch_key)) is not None
        except GitHubRefError:
            exists = False
        if not exists:
            return None
        return DispatchRecord(dispatch_key=dispatch_key, status="registered")

    def set_dispatch_status(
        self, dispatch_key: str, status: str, *, comment_posted: Optional[bool] = None
    ) -> None:
        record = self.dispatch_state(dispatch_key)
        if record is None:
            raise SchedulerError(f"分派记录不存在：{dispatch_key}")
        self._dispatches[dispatch_key] = DispatchRecord(
            dispatch_key=dispatch_key,
            issue_number=record.issue_number,
            pr_number=record.pr_number,
            merge_sha=record.merge_sha,
            status=str(status),
            comment_posted=(
                record.comment_posted if comment_posted is None else bool(comment_posted)
            ),
            created_at=record.created_at,
        )

    # -- 审计（从评论派生；refs 权威） ---------------------------------------
    def list_audit(self, task_id: str, limit: int = 200) -> list[Mapping[str, Any]]:
        if limit <= 0:
            return []
        issue = issue_number_for(task_id)
        try:
            comments = list(self.transport.list_comments(issue))
        except Exception as exc:  # noqa: BLE001
            raise SchedulerError(f"读取 Issue #{issue} 评论失败：{exc}") from exc
        entries: list[dict[str, Any]] = []
        for comment in comments:
            body = getattr(comment, "body", "") or ""
            event = ""
            if "<!-- ref-completion-id:" in body:
                event = "complete"
            elif "<!-- ref-lease:" in body:
                event = "renew" if "TASK_PROGRESS" in body else "claim"
            elif "<!-- ref-state:" in body:
                event = "transition"
            if not event:
                continue
            entries.append(
                {
                    "task_id": task_id,
                    "event": event,
                    "agent_id": "",
                    "instance_id": "",
                    "attempt_id": "",
                    "fencing_token": 0,
                    "detail": {},
                    "created_at": None,
                }
            )
        return list(reversed(entries))[: int(limit)]

    # -- 生命周期 ------------------------------------------------------------
    def close(self) -> None:
        return

    def __enter__(self) -> "GitHubRefScheduler":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["GitHubRefScheduler"]
