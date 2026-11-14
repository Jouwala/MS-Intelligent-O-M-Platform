"""基于 GitHub ref 的文件范围锁（ADR-7 §四，多 VM 串行化）。

为什么不要求「原子 test-and-set」：GitHub refs 只能做**创建是否成功**的原子判定，
没有「检查时-执行时」原子块。本模块因此采用**创建后校验（create-then-verify）**：

1. 代次校验（HWM 最大值 == 我的 token 且租约 ref 存在且未过期）；
2. 为每个 scope 路径创建自己的 ref（名字含 `task-token`，天然不与他人冲突）；
3. list 自己路径前缀 + 所有祖先前缀，找出所有**确实重叠**的其它持有者 ref；
4. 对其租约 ref 判存活：任一存活 → 删除自己刚建的全部 ref，抛 `api.ScopeConflict`
   （Worker 现有等待循环会退让重试）；全部过期 → 视为陈旧锁，忽略（Reaper 回收）；
5. 无存活重叠 → 获取成功。

安全性论证：两个并发者若都完成了第 2 步，则至少有一个在第 3 步看见了对方的 ref
→ 它退让并删除自己的 ref；若时间错开，后到者必然看见先到者的 ref。因此**不会同时
保留两把有效锁**，最坏情况是双方都退让后重试（安全侧，不会重复执行）。
不重叠的路径前缀不同，彼此不可见，可并发。

存活判定用**任务级**租约（`agent-lease/issue-<N>/` 下任一条未过期即持有者活着），
而不是「ref 里的 token 必须等于当前 HWM」：续租会推进 token 并删除旧 lease ref，
旧 token 名字的 scope ref 仍必须被视为有效，否则续租后别人就能抢同一范围。

释放：代次校验通过后删除**本任务**（同 issue）且 token <= 当前 token 的 scope ref。
旧 token 的代次校验必然失败（HWM 已推进），删不到新赢家的 ref（名字不同 + 校验拦截）。

本文件只依赖冻结契约 `ref_api.py` / `api.py` 与路径规范化 `scopes.py`；
不 import 具体 `RefStore` 实现，测试用内存 FakeRefStore 即可覆盖。
"""

from __future__ import annotations

from typing import Optional, Sequence

from . import ref_api
from .api import ScopeConflict, StaleFencing, utcnow
from .scopes import normalize_scopes

#: 兼容短名（调用方可以只传 clock / ttl 之一）。
DEFAULT_REF_TTL = ref_api.DEFAULT_REF_LEASE_TTL

_SCOPE_PREFIX = ref_api.SCOPE_ROOT + "/"


# ---------------------------------------------------------------------------
# 小工具：时间 / 任务号 / ref 列表
# ---------------------------------------------------------------------------


def _now_ms(clock) -> int:
    return int((clock or utcnow)().timestamp() * 1000)


def _ttl_ms(ttl) -> int:
    return ref_api.ttl_ms(ttl or DEFAULT_REF_TTL)


def issue_number_of(task_id: str) -> int:
    """`issue-<N>` → N；格式不符直接 ValueError（不猜测）。"""
    text = str(task_id or "")
    if not text.startswith("issue-"):
        raise ValueError(f"task_id 必须是 issue-<N> 形式：{task_id!r}")
    digits = text[len("issue-") :]
    if not digits.isdigit() or int(digits) <= 0:
        raise ValueError(f"task_id 必须是 issue-<N> 形式：{task_id!r}")
    return int(digits)


def _resolve_issue(issue_number, task_id) -> tuple[int, str]:
    """两种入参形式（issue_number 或 task_id）统一成 (N, "issue-N")。"""
    if issue_number is None:
        if not task_id:
            raise ValueError("必须给出 issue_number 或 task_id")
        issue = issue_number_of(task_id)
    else:
        issue = int(issue_number)
        if issue <= 0:
            raise ValueError(f"issue_number 必须为正整数：{issue_number!r}")
    return issue, f"issue-{issue}"


def _list_refs(store, prefix: str) -> list:
    return list(store.list(prefix) or [])


def _leases_of(store, issue: int, token: Optional[int] = None) -> list[ref_api.LeaseRef]:
    prefix = (
        ref_api.lease_prefix(issue)
        if token is None
        else ref_api.lease_prefix(issue, token)
    )
    found: list[ref_api.LeaseRef] = []
    for item in _list_refs(store, prefix):
        lease = ref_api.parse_lease(getattr(item, "ref", "") or "")
        if lease is not None:
            found.append(lease)
    return found


def _live_lease(store, issue: int, now_ms: int, ttl_ms: int, cache: dict):
    """该任务当前是否有存活租约；有则返回最新那条（按 token/epoch），否则 None。"""
    if issue in cache:
        return cache[issue]
    live = [lease for lease in _leases_of(store, issue) if lease.is_live(now_ms, ttl_ms)]
    result = max(live, key=lambda lease: (lease.token, lease.epoch_ms)) if live else None
    cache[issue] = result
    return result


def _require_generation(
    store,
    issue: int,
    token: int,
    *,
    now_ms: int,
    ttl_ms: int,
    need_live: bool,
    reason: str,
) -> int:
    """代次校验：HWM 最大值 == 我的 token；need_live 时还要求我的租约未过期。"""
    task_id = f"issue-{issue}"
    max_token = ref_api.max_hwm_token(_list_refs(store, ref_api.hwm_prefix(issue)))
    own = _leases_of(store, issue, token)
    present = bool(own)
    if need_live and present:
        present = any(lease.is_live(now_ms, ttl_ms) for lease in own)
    ref_api.require_current_token(task_id, "", int(token), max_token, present, reason=reason)
    return max_token


def _paths_overlap(ours: str, other: str) -> bool:
    """两条**已编码**路径是否重叠：相同，或一方是另一方的目录前缀。"""
    if not ours or not other:
        return False
    return (
        ours == other
        or ours.startswith(other + "/")
        or other.startswith(ours + "/")
    )


def _delete_best_effort(store, refs: Sequence[str]) -> list[str]:
    """删除自己刚建的 ref；单条失败不掩盖主错误（返回失败列表供排障）。"""
    failed: list[str] = []
    for ref in refs:
        try:
            store.delete(ref)
        except Exception:  # noqa: BLE001 - 回退路径上的网络/API 错误不覆盖冲突信号
            failed.append(ref)
    return failed


def _find_live_conflicts(
    store, task_id: str, encoded_paths: Sequence[str], now_ms: int, ttl_ms: int
) -> list[dict]:
    """扫描与自己重叠、且持有者租约仍存活的其它 scope ref。"""
    candidates: dict[str, dict] = {}
    for encoded in encoded_paths:
        # 前缀 list：自己路径（捕获后代持有者）+ 所有祖先（捕获祖先持有者）
        for prefix in ref_api.scope_overlap_prefixes(encoded):
            for item in _list_refs(store, prefix):
                ref = getattr(item, "ref", "") or ""
                parsed = ref_api.parse_scope_ref(ref)
                if not parsed or parsed["task_id"] == task_id:
                    continue
                if not _paths_overlap(encoded, parsed["encoded_path"]):
                    continue  # 祖先前缀会顺带列出「兄弟目录」，这里精确过滤
                candidates[ref] = parsed
    cache: dict = {}
    conflicts: list[dict] = []
    for parsed in candidates.values():
        holder_issue = issue_number_of(parsed["task_id"])
        lease = _live_lease(store, holder_issue, now_ms, ttl_ms, cache)
        if lease is None:
            continue  # 陈旧的 scope ref：持有者租约已过期，等 Reaper 清理
        conflicts.append(
            {
                "task_id": parsed["task_id"],
                "agent_id": lease.agent_id,
                "path": parsed["encoded_path"],
            }
        )
    conflicts.sort(key=lambda item: (item["task_id"], item["path"]))
    return conflicts


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def acquire_locks(
    ref_store,
    transport=None,
    issue_number=None,
    token: int = 0,
    scopes: Sequence[str] = (),
    *,
    clock=None,
    ttl=None,
    task_id: Optional[str] = None,
    attempt_id: str = "",
    casefold: bool = False,
) -> None:
    """获取一组文件范围锁；冲突抛 `api.ScopeConflict`，代次不符抛 `api.StaleFencing`。

    `transport` 保留给未来审计评论（ADR 里评论只是展示层），当前权威状态全在 refs，
    实现不读它；保留参数是为了与 `ref_service.GitHubRefScheduler` 的委托签名一致。
    """
    issue, resolved_task = _resolve_issue(issue_number, task_id)
    token = int(token or 0)
    if token <= 0:
        raise ValueError("acquire_locks 需要正数 fencing_token")
    normalized = normalize_scopes(scopes, casefold=casefold) if scopes else ()
    if not normalized:
        # 与 SQLite 实现的错误口径一致：不申请空范围，避免“无锁运行”
        raise ValueError("acquire_locks 需要至少一个文件范围")

    now_ms, ttl_ms = _now_ms(clock), _ttl_ms(ttl)
    _require_generation(
        ref_store,
        issue,
        token,
        now_ms=now_ms,
        ttl_ms=ttl_ms,
        need_live=True,
        reason="租约 ref 不存在或已过期（或代次已旧），不能申请范围锁",
    )

    encoded_paths = [ref_api.encode_scope_path(path) for path in normalized]
    branch_sha = ref_store.branch_sha()
    created: list[str] = []
    try:
        for encoded in encoded_paths:
            ref = ref_api.scope_ref(encoded, resolved_task, token)
            # 同名 ref 唯一：False 说明本次是重试且之前已建过（必属自己），不算失败
            ref_store.create(ref, branch_sha)
            created.append(ref)
        conflicts = _find_live_conflicts(
            ref_store, resolved_task, encoded_paths, now_ms, ttl_ms
        )
    except Exception:  # noqa: BLE001 - 创建/列表失败：先回退已建 ref 再原样抛
        _delete_best_effort(ref_store, created)
        raise

    if conflicts:
        _delete_best_effort(ref_store, created)
        first = conflicts[0]
        raise ScopeConflict(
            resolved_task,
            first["task_id"],
            first["agent_id"],
            [item["path"] for item in conflicts],
        )


def release_locks(
    ref_store,
    transport=None,
    issue_number=None,
    token: int = 0,
    scopes: Optional[Sequence[str]] = None,
    *,
    clock=None,
    ttl=None,
    task_id: Optional[str] = None,
    attempt_id: str = "",
    casefold: bool = False,
) -> int:
    """释放自己的范围锁，返回删除的 ref 条数（重复释放返回 0，幂等）。

    - 代次校验：HWM 最大值必须仍等于我的 token，旧 token 一律 `StaleFencing`，
      因此删不到新赢家的 ref；
    - 删除范围是「本任务 + token <= 当前 token」：续租推进 token 后，之前用旧 token
      建的 ref 名字不同，也一并清掉（否则会挡别人到 Reaper 下一轮）；
    - 不要求租约 ref 仍存在：终态迁移/回收可能已删租约，但本实例仍是当前代次。
    """
    issue, resolved_task = _resolve_issue(issue_number, task_id)
    token = int(token or 0)
    max_token = ref_api.max_hwm_token(
        _list_refs(ref_store, ref_api.hwm_prefix(issue))
    )
    if token <= 0 or token != max_token:
        raise StaleFencing(
            resolved_task,
            attempt_id,
            token,
            "",
            max_token,
            "旧 token 不能释放新代次的锁",
        )

    if scopes:
        normalized = normalize_scopes(scopes, casefold=casefold)
        prefixes = [
            _SCOPE_PREFIX + ref_api.encode_scope_path(path) + "/"
            for path in normalized
        ]
    else:
        # 调用方（api.release_scope）没有 scopes 参数：退化为全量扫并只删自己的
        prefixes = [_SCOPE_PREFIX]

    removed: list[str] = []
    seen: set[str] = set()
    for prefix in prefixes:
        for item in _list_refs(ref_store, prefix):
            ref = getattr(item, "ref", "") or ""
            if ref in seen:
                continue
            parsed = ref_api.parse_scope_ref(ref)
            if not parsed or parsed["task_id"] != resolved_task:
                continue
            if parsed["token"] > token:  # 防御：更高代次的 ref 绝不由我删
                continue
            seen.add(ref)
            if ref_store.delete(ref):
                removed.append(ref)
    return len(removed)


def cleanup_orphans(
    ref_store, transport=None, *, clock=None, ttl=None, dry_run: bool = False
) -> list[str]:
    """删除「所属任务没有任何存活租约」的孤儿 scope ref，返回被删（或待删）ref 名。

    供 cron Reaper 调用（ADR-7 §3.7）。HWM 与任何 lease ref 都不在这里动：
    HWM 永不删、lease 由 Reaper 的过期回收处理。`dry_run=True` 只返回名单不删除。
    """
    now_ms, ttl_ms = _now_ms(clock), _ttl_ms(ttl)
    cache: dict = {}
    removed: list[str] = []
    for item in _list_refs(ref_store, _SCOPE_PREFIX):
        ref = getattr(item, "ref", "") or ""
        parsed = ref_api.parse_scope_ref(ref)
        if not parsed:
            continue
        issue = issue_number_of(parsed["task_id"])
        if _live_lease(ref_store, issue, now_ms, ttl_ms, cache) is not None:
            continue  # 持有者活着：锁仍有效
        if dry_run or ref_store.delete(ref):
            removed.append(ref)
    return removed


def scope_holders(ref_store, *, clock=None, ttl=None) -> dict[str, tuple[str, tuple[str, ...]]]:
    """当前有效范围锁：task_id -> (agent_id, scopes)。

    返回的路径是 ref 里编码后的形式；正常仓库相对路径（无特殊字符）编码前后一致。
    与 SQLite 实现 `scope_holders()` 同口径：过期租约的锁不返回。
    """
    now_ms, ttl_ms = _now_ms(clock), _ttl_ms(ttl)
    cache: dict = {}
    collected: dict[str, tuple[str, list[str]]] = {}
    for item in _list_refs(ref_store, _SCOPE_PREFIX):
        parsed = ref_api.parse_scope_ref(getattr(item, "ref", "") or "")
        if not parsed:
            continue
        issue = issue_number_of(parsed["task_id"])
        lease = _live_lease(ref_store, issue, now_ms, ttl_ms, cache)
        if lease is None:
            continue
        task = parsed["task_id"]
        if task in collected:
            collected[task][1].append(parsed["encoded_path"])
        else:
            collected[task] = (lease.agent_id, [parsed["encoded_path"]])
    return {
        task: (agent, tuple(paths)) for task, (agent, paths) in collected.items()
    }
