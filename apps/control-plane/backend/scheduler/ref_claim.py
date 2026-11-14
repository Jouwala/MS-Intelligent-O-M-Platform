"""PR-1 认领：ref-CAS 权威层的共享读写工具 + HWM CAS 认领实现。

本模块是 ref 权威层的地基（PR-1），后续阶段（fencing / reap / completion）都
复用这里的 `RefAuthorityBase`：

- **权威来源只有 GitHub refs**：HWM（high-water mark）ref 只增不减，创建 HWM
  的 `POST /git/refs` 是 GitHub 服务端裁决的唯一线性化点；本模块**不读也不写任何
  本地 DB/文件**，进程内只有展示用的元数据。
- **认领（claim）流程**：
  1. list HWM → 当前最大 token `T`；list lease → 存活租约（`epoch_ms + ttl`）；
  2. 有存活租约且持有者是别人 → `LeaseHeld`；已终态（完成评论/终态标记）→
     `AlreadyTerminal`；
  3. `create(agent-hwm/issue-N/T+1)`：True=唯一赢家，False=失败者；
  4. 赢家创建 lease ref（epoch_ms + 身份）→ **复核没有其它实例的存活租约**
     → 复核任务未被并发完成 → 发认领评论 → 返回 `api.Lease`。
- **失败者重读**：拿到 422 后重新 list；看到别人的存活租约就 `LeaseHeld`，
  否则下一轮用更高 token 重试（上限 `claim_retries`）。

关于 ADR-7 §3.3 未写到的并发窗口（安全修正，已在 `claim` 注释与测试中说明）：
赢家「HWM CAS 成功 → lease ref 建成」之间若被杀死，失败者会看到「HWM 前进了
但没有租约」而抢到更高 token，两个进程就可能同时返回 claim 成功。修正规则：
**成功者必须确认此刻没有任何其它实例的存活租约**（成功后自己的租约一直保留到
TTL/回收），因此后到的任何复核者都会看见它并退让——宁可少认领一次，不可两个赢家。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from . import ref_api
from .api import (
    CLAIMED,
    COMPLETED,
    TERMINAL_STATES,
    AlreadyTerminal,
    Lease,
    LeaseHeld,
    SchedulerError,
    TaskNotFound,
    TaskSnapshot,
    iso,
    utcnow,
)
from .github_refs import GitHubRefError
from .ref_view import (
    TaskRefView,
    as_utc,
    build_view,
    issue_number_for,
    parse_completions,
    parse_state_markers,
    state_comment_body,
    task_id_of,
)

#: 身份/代次校验失败的统一原因文案（日志/排障用）。
REASON_STALE = "代次不匹配（旧执行者或租约已被推进）"


class RefAuthorityBase:
    """ref 权威层共享读写工具（无本地状态；所有权威事实都来自 refs/评论）。"""

    def __init__(
        self,
        ref_store: Any,
        transport: Any,
        *,
        repo: str = "",
        clock=utcnow,
        lease_ttl: timedelta = ref_api.DEFAULT_REF_LEASE_TTL,
        anchor_sha: str = "",
        anchor_branch: str = "main",
        claim_retries: int = ref_api.DEFAULT_CLAIM_RETRIES,
        label_retries: int = 2,
        reaper_instance: str = "",
    ) -> None:
        self.ref_store = ref_store
        self.transport = transport
        self.repo = repo
        self.clock = clock
        self.lease_ttl = lease_ttl
        self.anchor_sha = anchor_sha
        self.anchor_branch = anchor_branch
        self.claim_retries = max(1, int(claim_retries))
        #: 标签收敛的有界重试次数（标签是展示层，写失败不失败完成；Reaper/对账收敛）。
        self.label_retries = max(0, int(label_retries))
        #: Reaper 身份（写进 agent-reap ref 名做回收权 CAS；空则由本实例生成）。
        self.reaper_instance = reaper_instance or uuid.uuid4().hex[:12]
        self._anchor_cache = ""
        #: 标签写失败的最近错误（可观测；不影响权威结果）。
        self.label_errors: list[str] = []

    # -- 时间 / 身份 ---------------------------------------------------------
    def now(self) -> datetime:
        return as_utc(self.clock())

    def _now_ms(self) -> int:
        return int(self.now().timestamp() * 1000)

    def _ttl_ms(self, ttl: timedelta) -> int:
        return ref_api.ttl_ms(ttl)

    def _issue_of(self, task_id: str) -> int:
        return issue_number_for(task_id)

    def task_id_of(self, issue_number: int) -> str:
        return task_id_of(issue_number)

    def anchor(self) -> str:
        """建 ref 的锚点 SHA（懒加载分支头并缓存；多 VM 共享同一仓库）。"""
        if self.anchor_sha:
            return self.anchor_sha
        if not self._anchor_cache:
            try:
                self._anchor_cache = self.ref_store.branch_sha(self.anchor_branch)
            except GitHubRefError as exc:
                raise SchedulerError(f"读取锚点分支 {self.anchor_branch} 失败：{exc}") from exc
        return self._anchor_cache

    # -- 读 refs / 评论 -----------------------------------------------------
    def _require_issue(self, issue_number: int, task_id: str) -> None:
        """确认 Issue 存在（新任务认领/查询前）：不存在 → TaskNotFound。

        注意：这里把「读 Issue 失败」统一映射成 TaskNotFound；transport 区分不了
        「404」与「断网」时宁可让 Worker 下一轮重新发现（幂等，无副作用）。
        """
        try:
            self.transport.get_issue(issue_number)
        except Exception as exc:  # noqa: BLE001 - 适配器异常类型不一
            raise TaskNotFound(task_id) from exc

    def _comments(self, issue_number: int) -> list[str]:
        """全量评论（适配器自身分页；失败显式报错，不返回半截）。"""
        try:
            return [
                getattr(comment, "body", "") or ""
                for comment in self.transport.list_comments(issue_number)
            ]
        except Exception as exc:  # noqa: BLE001 - 适配器异常类型不一，统一收敛
            raise SchedulerError(f"读取 Issue #{issue_number} 评论失败：{exc}") from exc

    def _max_token(self, issue_number: int) -> int:
        try:
            return ref_api.max_hwm_token(self.ref_store.list(ref_api.hwm_prefix(issue_number)))
        except GitHubRefError as exc:
            raise SchedulerError(f"读取任务代次失败（refs 不可用）：{exc}") from exc

    def _list_leases(
        self, issue_number: int, token: Optional[int] = None
    ) -> list[ref_api.LeaseRef]:
        try:
            refs = self.ref_store.list(ref_api.lease_prefix(issue_number, token))
        except GitHubRefError as exc:
            raise SchedulerError(f"读取租约 ref 失败（refs 不可用）：{exc}") from exc
        return [lease for lease in (ref_api.parse_lease(i.ref) for i in refs) if lease]

    def _delete_quietly(self, ref: str) -> bool:
        """删除 ref；失败只记 False（清理失败不影响权威判定）。"""
        if not ref:
            return False
        try:
            return bool(self.ref_store.delete(ref))
        except GitHubRefError:
            return False

    def _comment(self, issue_number: int, body: str, author: str = "") -> None:
        """写状态/完成标记评论；失败抛 SchedulerError（标记是派生状态的权威输入）。"""
        try:
            self.transport.add_comment(issue_number, body, author=author)
        except Exception as exc:  # noqa: BLE001 - 统一收敛成调度器错误
            raise SchedulerError(f"Issue #{issue_number} 评论写入失败：{exc}") from exc

    def _comment_best_effort(self, issue_number: int, body: str, author: str = "") -> None:
        """认领/续租评论只是审计：失败不让已成功的 CAS 退化成失败。"""
        try:
            self.transport.add_comment(issue_number, body, author=author)
        except Exception:  # noqa: BLE001 - 审计评论不允许影响权威结果
            return

    # -- 派生视图 / 快照 -----------------------------------------------------
    def view(
        self,
        issue_number: int,
        *,
        task_id: str = "",
        repo: str = "",
        title: str = "",
        comments: Optional[Sequence[str]] = None,
        scopes: Sequence[str] = (),
        max_token: Optional[int] = None,
        leases: Optional[Sequence[ref_api.LeaseRef]] = None,
    ) -> TaskRefView:
        """从 refs + 评论派生任务视图（支持传入已读事实，避免重复请求）。"""
        bodies = list(comments) if comments is not None else self._comments(issue_number)
        token = self._max_token(issue_number) if max_token is None else int(max_token)
        lease_refs = list(leases) if leases is not None else self._list_leases(issue_number)
        live = ref_api.live_lease(lease_refs, self._now_ms(), self._ttl_ms(self.lease_ttl))
        return build_view(
            task_id=task_id or task_id_of(issue_number),
            issue_number=issue_number,
            repo=repo or self.repo,
            title=title,
            max_token=token,
            lease_refs=lease_refs,
            live=live,
            comment_bodies=bodies,
            scopes=scopes,
        )

    def snapshot(
        self,
        task_id: str,
        *,
        issue_number: Optional[int] = None,
        repo: str = "",
        title: str = "",
        scopes: Sequence[str] = (),
    ) -> TaskSnapshot:
        """派生完整 TaskSnapshot（refs + 评论；只读、无副作用）。"""
        issue = int(issue_number) if issue_number is not None else issue_number_for(task_id)
        view = self.view(issue, task_id=task_id, repo=repo, title=title, scopes=scopes)
        lease = view.live
        return TaskSnapshot(
            task_id=task_id,
            issue_number=issue,
            repo=view.repo,
            title=view.title,
            state=view.state,
            terminal_status=view.terminal_status,
            fencing_token=view.max_token,
            attempt_id=view.attempt_id,
            agent_id=view.agent_id,
            instance_id=view.instance_id,
            lease_expire_at=(
                datetime.fromtimestamp(lease.epoch_ms / 1000, timezone.utc) + self.lease_ttl
                if lease is not None
                else None
            ),
            heartbeat_at=view.heartbeat_at,
            started_at=view.claimed_at,
            completed_at=view.completed_at,
            error_code=view.error_code,
            error_message=view.error_message,
            idempotency_key=view.idempotency_key,
            scopes=view.scopes,
            cancel_requested_at=view.cancel_requested_at,
            updated_at=view.updated_at,
        )


class RefClaimer(RefAuthorityBase):
    """HWM CAS 认领（PR-1）。"""

    def claim(
        self,
        task_id: str,
        *,
        agent_id: str,
        instance_id: str,
        attempt_id: str,
        lease_ttl: Optional[timedelta] = None,
    ) -> Lease:
        issue = issue_number_for(task_id)
        ttl = lease_ttl or self.lease_ttl
        if ttl.total_seconds() <= 0:
            raise ValueError("lease_ttl 必须为正")
        if not agent_id or not instance_id or not attempt_id:
            raise ValueError("claim 需要 agent_id / instance_id / attempt_id 均非空")
        # 提前校验身份能安全放进 ref 名（非法值直接 ValueError，不白烧 token）
        ref_api.lease_ref(issue, 1, 1, agent_id, instance_id, attempt_id)
        now = self.now()
        now_ms = int(now.timestamp() * 1000)
        ttl_ms = self._ttl_ms(ttl)
        anchor = self.anchor()
        my_instance = str(instance_id).lower()
        last_live: Optional[ref_api.LeaseRef] = None
        create_errors = 0
        for _ in range(self.claim_retries):
            max_token = self._max_token(issue)
            leases = self._list_leases(issue)
            if max_token == 0 and not leases:
                # 全新任务：确认 Issue 真实存在（防 task_id 笔误在仓库里留下孤儿 ref）
                self._require_issue(issue, task_id)
            live = ref_api.live_lease(leases, now_ms, ttl_ms)
            if live is not None:
                ref_api.require_live_claim(task_id, live, instance_id, now_ms, ttl)
                last_live = live
            bodies = self._comments(issue) if max_token > 0 else []
            view = self.view(
                issue,
                task_id=task_id,
                comments=bodies,
                max_token=max_token,
                leases=leases,
            )
            if view.state in TERMINAL_STATES:
                raise AlreadyTerminal(task_id, view.terminal_status or view.state)
            candidate = max_token + 1
            won: Optional[bool]
            try:
                won = bool(self.ref_store.create(ref_api.hwm_ref(issue, candidate), anchor))
            except GitHubRefError:
                won = None  # 结果不确定：按失败者处理，下一轮更高 token 重试（安全侧）
            if won is None:
                create_errors += 1
                continue
            if not won:
                # 失败者重读。三种情况：
                # 1) 有存活租约 → 别人持有 → LeaseHeld；
                # 2) 只有「已过期的 lease ref」→ 赢家已失联，本进程可安全地从更高
                #    token 重试（ADR-7 §3.3 的「已过期则从 T+1 重试」）；
                # 3) 完全没有任何 lease ref → 可能是赢家「CAS 成功但还没建 lease」的
                #    在途窗口，立刻抢更高 token 会产生两个赢家 → 保守退让，下一轮再试。
                leases2 = self._list_leases(issue)
                live2 = ref_api.live_lease(leases2, now_ms, ttl_ms)
                if live2 is not None:
                    ref_api.require_live_claim(task_id, live2, instance_id, now_ms, ttl)
                    last_live = live2
                    continue
                if not leases2:
                    raise LeaseHeld(task_id, "", "", now + ttl)
                continue
            # 唯一赢家：建租约 ref（epoch_ms 是存活判定的唯一依据）
            lease_name = ref_api.lease_ref(
                issue, candidate, now_ms, agent_id, instance_id, attempt_id
            )
            try:
                created = bool(self.ref_store.create(lease_name, anchor))
            except GitHubRefError as exc:
                raise SchedulerError(
                    f"认领 token={candidate} 已占用，但租约 ref 创建失败（稍后重试）：{exc}"
                ) from exc
            if not created:
                raise SchedulerError(f"租约 ref 已存在（token={candidate}），拒绝重复认领")
            # 复核 1：任何其它实例的存活租约 → 我们不是唯一赢家，删租约退让。
            # 退让后**继续重试**：另一个赢家若也在复核中退让（双方同时看到对方租约），
            # 会留下「没人持有」的状态；下一轮 CAS 会由唯一赢家收敛，避免双方都
            # LeaseHeld 导致任务本轮无人认领（liveness，不破坏安全性）。
            rivals = [
                other
                for other in self._list_leases(issue)
                if other.token != candidate
                and other.instance_id != my_instance
                and other.is_live(now_ms, ttl_ms)
            ]
            if rivals:
                self._delete_quietly(lease_name)
                last_live = max(rivals, key=lambda item: (item.token, item.epoch_ms))
                continue
            # 复核 2：任务是否在 CAS 期间被别人完成/取消（终态优先，绝不复活）
            fresh = self.view(issue, task_id=task_id)
            if fresh.state in TERMINAL_STATES:
                self._delete_quietly(lease_name)
                raise AlreadyTerminal(task_id, fresh.terminal_status or fresh.state)
            # 收尾：删掉本任务更低代次的旧 lease ref（本实例重认领/过期接管后的残留）。
            # 其它实例的存活租约已在上面被拦下，所以这里只会删「过期」或「本实例旧代」。
            for old in self._list_leases(issue):
                if old.token < candidate:
                    self._delete_quietly(old.ref)
            self._comment_best_effort(
                issue,
                "\n".join(
                    (
                        ref_api.lease_comment_body(
                            issue_number=issue,
                            token=candidate,
                            agent_id=agent_id,
                            instance_id=instance_id,
                            attempt_id=attempt_id,
                            timestamp=iso(now),
                        ),
                        state_comment_body(
                            issue_number=issue,
                            token=candidate,
                            state=CLAIMED,
                            timestamp=now,
                            reason="认领成功（HWM CAS 唯一赢家）",
                        ),
                    )
                ),
                agent_id,
            )
            return Lease(
                task_id=task_id,
                attempt_id=str(attempt_id).lower(),
                agent_id=str(agent_id).lower(),
                instance_id=my_instance,
                fencing_token=candidate,
                claimed_at=now,
                expires_at=now + ttl,
                heartbeat_at=now,
            )
        if create_errors and create_errors >= self.claim_retries:
            raise SchedulerError("认领失败：ref 权威层不可用（HWM 创建全部异常，稍后重试）")
        if last_live is not None:
            expires = datetime.fromtimestamp(last_live.epoch_ms / 1000, timezone.utc) + ttl
            raise LeaseHeld(task_id, last_live.agent_id, last_live.instance_id, expires)
        raise LeaseHeld(task_id, "", "", now + ttl)


def completion_markers(comment_bodies: Sequence[str]):
    """便捷再导出：完成标记解析（ref_completion/测试共用）。"""
    return parse_completions(comment_bodies)


def state_markers(comment_bodies: Sequence[str]):
    """便捷再导出：状态标记解析（fencing/reap/测试共用）。"""
    return parse_state_markers(comment_bodies)


__all__ = [
    "RefAuthorityBase",
    "RefClaimer",
    "REASON_STALE",
    "completion_markers",
    "issue_number_for",
    "state_markers",
    "task_id_of",
]
