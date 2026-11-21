"""PR-3 回收：过期租约判定 + **回收权 ref-CAS** + 只由赢家删除 lease ref。

为什么回收也要 CAS（owner 2026-11-14 明确要求）：Reaper 在 GitHub Actions 里是
cron，同一时刻可能有两个 run；删除 lease ref 虽然幂等，但「回收后的回写/状态收敛」
不能重复做。契约（`ref_api.py`，最新修订）提供：

    refs/heads/agent-reap/issue-<N>/<token:08d>

**命名刻意不含 reaper_instance**：把处理者身份编进 ref 名后，两个不同 Reaper 会
各自 create 成功（名字不同），CAS 就失效了（实测 reaped=2）。现在同一
`(issue, token)` 全局唯一：201 = 本 Reaper 是唯一赢家；422 = 另一个 Reaper 已在
收，记录 `outcome="skipped"` 跳过，不做任何删除/回写。赢家身份只作审计（日志/
调用方记录），不参与互斥。

本模块**只动 ref**（不写本地状态、不写评论/标签），因此既能在 Actions Reaper 里用，
也能被 `GitHubRefScheduler.reap_expired` 复用。异常（`GitHubRefError` 等）不吞，
交给调用方（CLI）决定退出码。

对外接口（ref-runtime 的 `dispatch/lease_reaper.py` 依赖，已冻结）：

    reap_expired_leases(store, now_ms, ttl_ms, *, reaper_instance, dry_run=False)
        -> list[dict]

每条 dict：`issue_number, token, epoch_ms, instance_id, agent_id, ref, outcome`
（outcome ∈ {"reaped", "skipped"}；dry_run 时不删任何东西，outcome="skipped"）。
同一 `(issue, token)` 下有多条 lease ref 时，以 **epoch 最大**那条判断是否过期；
该组所有 ref 都属于过期代次，赢家一并删除。HWM 永不删（token 不复用）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from . import ref_api
from .api import SchedulerError
from .github_refs import GitHubRefError

_REAPER_INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


class RefReapError(SchedulerError):
    """回收过程中的确定性失败（参数非法/ref 状态自相矛盾）。"""


def _validate_reaper_instance(reaper_instance: str) -> str:
    """校验（并归一化）回收者身份：只用于审计，不参与 ref 名/互斥。"""
    text = str(reaper_instance or "").strip().lower()
    if not _REAPER_INSTANCE_RE.match(text):
        raise RefReapError(
            f"reaper_instance 必须匹配 [a-z0-9][a-z0-9-]{{0,40}}：{reaper_instance!r}"
        )
    return text


def _group_expired(
    store: Any, now_ms: int, ttl_ms: int
) -> dict[tuple[int, int], list[tuple[Any, ref_api.LeaseRef]]]:
    """把 lease ref 按 (issue, token) 分组，只返回过期组。"""
    try:
        infos = list(store.list(ref_api.LEASE_ROOT + "/") or [])
    except GitHubRefError as exc:
        raise RefReapError(f"列出 lease ref 失败：{exc}") from exc
    groups: dict[tuple[int, int], list[tuple[Any, ref_api.LeaseRef]]] = {}
    for info in infos:
        lease = ref_api.parse_lease(getattr(info, "ref", "") or "")
        if lease is None:
            continue
        groups.setdefault((lease.issue_number, lease.token), []).append((info, lease))
    expired: dict[tuple[int, int], list[tuple[Any, ref_api.LeaseRef]]] = {}
    for key, items in sorted(groups.items()):
        newest = max(items, key=lambda pair: pair[1].epoch_ms)
        if not newest[1].is_live(now_ms, ttl_ms):
            expired[key] = items
    return expired


def _record(info: Any, lease: ref_api.LeaseRef, outcome: str) -> dict[str, Any]:
    return {
        "issue_number": lease.issue_number,
        "token": lease.token,
        "epoch_ms": lease.epoch_ms,
        "instance_id": lease.instance_id,
        "agent_id": lease.agent_id,
        "ref": getattr(info, "ref", "") or lease.ref,
        "outcome": outcome,
    }


def _anchor_sha(store: Any, expired: dict[tuple[int, int], list]) -> str:
    """回收权 ref 需要一个已存在的 git 对象 SHA：优先复用 lease ref 自带的 sha。"""
    for items in expired.values():
        for info, _lease in items:
            sha = getattr(info, "sha", "") or ""
            if sha:
                return sha
    try:
        return store.branch_sha()
    except GitHubRefError as exc:
        raise RefReapError(f"读取锚点 SHA 失败（无法创建回收权 ref）：{exc}") from exc


def reap_expired_leases(
    store: Any,
    now_ms: int,
    ttl_ms: int,
    *,
    reaper_instance: str,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """回收过期租约；返回每条候选 lease ref 的记录（见模块文档）。

    - `now_ms` / `ttl_ms`：显式传入，便于 cron 与测试使用同一时间口径；
    - `reaper_instance`：回收者身份，**仅用于审计校验**（不参与 ref 名/互斥）；
    - `dry_run=True`：只返回候选，不创建回收权 ref、不删除任何 ref
      （`outcome="skipped"`，因为本次没有真的回收）。
    """
    _validate_reaper_instance(reaper_instance)
    now_ms = int(now_ms)
    ttl_ms = int(ttl_ms)
    if ttl_ms <= 0:
        raise RefReapError(f"ttl_ms 必须为正：{ttl_ms}")
    expired = _group_expired(store, now_ms, ttl_ms)
    if not expired:
        return []
    records: list[dict[str, Any]] = []
    if dry_run:
        for items in expired.values():
            records.extend(_record(info, lease, "skipped") for info, lease in items)
        return records
    anchor = _anchor_sha(store, expired)
    for (issue_number, token), items in sorted(expired.items()):
        claim_ref = ref_api.reap_ref(issue_number, token)  # 全局唯一 → 真 CAS
        try:
            won = bool(store.create(claim_ref, anchor))
        except GitHubRefError as exc:
            # 结果不确定/不可用：不吞（CLI 非零退出），也不假装回收成功
            raise RefReapError(f"创建回收权 ref {claim_ref} 失败：{exc}") from exc
        if not won:
            # 另一个 Reaper 已经赢得回收权（422）：整组跳过，绝不删除/回写
            records.extend(_record(info, lease, "skipped") for info, lease in items)
            continue
        for info, lease in items:
            ref = getattr(info, "ref", "") or lease.ref
            try:
                store.delete(ref)  # 幂等：已被别人删掉返回 False 也算回收完成
            except GitHubRefError as exc:
                raise RefReapError(f"删除过期 lease ref {ref} 失败：{exc}") from exc
            records.append(_record(info, lease, "reaped"))
    return records


def reaped_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤出真正回收成功的记录（CLI/对账用）。"""
    return [record for record in records if record.get("outcome") == "reaped"]


def skipped_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤出被跳过的记录（CAS 输家或 dry_run）。"""
    return [record for record in records if record.get("outcome") == "skipped"]


def reap_claim_refs(store: Any) -> list[str]:
    """列出历史回收权 ref（审计用；本模块不自动清理，token 不复用故量可控）。"""
    try:
        infos = list(store.list(ref_api.reap_prefix()) or [])
    except GitHubRefError as exc:
        raise RefReapError(f"列出回收权 ref 失败：{exc}") from exc
    refs: list[str] = []
    for info in infos:
        ref = getattr(info, "ref", "") or ""
        if ref_api.parse_reap(ref) is not None:
            refs.append(ref)
    return sorted(refs)


__all__ = [
    "RefReapError",
    "reap_claim_refs",
    "reap_expired_leases",
    "reaped_records",
    "skipped_records",
]
