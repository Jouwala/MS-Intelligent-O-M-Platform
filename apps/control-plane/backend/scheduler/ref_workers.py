"""PR-5：Worker 注册表（GitHub 原生后端）——注册表 Issue 评论实现。

`api.Scheduler` 的 `register_worker / heartbeat_worker / list_workers` 在 GitHub
原生权威层里没有本地表可写（ADR-7 §零：状态全落 GitHub、不依赖本地 DB），因此用
**注册表 Issue 的评论**承载（与 `adapters/github_issue.py` 的既有写法兼容）：

- 注册：`<!-- worker-registry: <agent>, at: ISO -->`；
- 心跳：`<!-- worker-heartbeat: <agent>, at: ISO -->`（默认 60s 节流，省 API 配额）；
- 实例：`<!-- worker-instance: <instance>, hostname: ..., platform: ... -->`；
- 扩展：`<!-- worker-meta: agent=..., instance=..., hostname=..., platform=...,
  capabilities=a,b -->`（结构化字段；没有 meta 的旧评论也能解析）。

`list_workers` 分页读评论，按 `(agent, instance)` 取最新事实并按
`derive_worker_state` 输出七态。**无 registry issue 时本地 no-op**：只在进程内记一份展示快照，不报错（B2 的
factory 默认 `registry_issue=None`），与 SQLite 版「无注册表」行为一致。

心跳的 refs 通道（Phase 3 P3-1，#141，`ref_heartbeat.py`）：评论心跳需要一个
真实的 `registry_issue`，但全仓库默认是 None，且 5 个 Worker 每天要往一个
Issue 写约 7200 条评论，`list_workers` 分页读全量评论只会越来越慢。所以
`heartbeat_worker` 在有 `ref_store` 时**同时**写一条 beat ref
（`refs/heads/agent-heartbeat/<agent>/<instance>/<epoch_ms>/<state>`，状态只
看 ref 名，不读评论、不读 commit），评论通道保留作兼容。`list_workers` 合并
两个通道的事实，按 `ref_heartbeat.derive_worker_state` 输出七态
ONLINE / IDLE / WORKING / WAITING / BLOCKED / OFFLINE / FAILED（外加从未见过
的 unknown）；其中 WORKING 由有效租约 ref 或 Worker 自报 working 判定，
WAITING 由 Worker 自报（调用方传 state 参数），BLOCKED 由 Worker 在 mark_blocked
时自报（best-effort，节流窗口内可能写不出），FAILED 暂无生产者——进程崩溃时
写不出 beat，只能表现为 offline，显式失败上报由 P3-3 接线。

身份归一（P0 教训，#141 复审）：Linux Worker 认领用租约身份
`(ref_agent_id, ref_instance)`（ref 名只接受 hex），注册/心跳用人类身份
`(worker_id, instance)`。`list_workers` 判 working 时，对每个事实键同时查
原始键与 `ref_heartbeat.ref_instance_id(instance)` 派生的租约键，两者任一
持有有效租约即算 working；Windows Worker 的 instance 本就是 hex，派生查
询只是多一次未命中，无影响。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Sequence

from . import ref_api, ref_heartbeat
from .api import (
    DEFAULT_HEARTBEAT_TTL,
    DEFAULT_LEASE_TTL,
    SchedulerError,
    WorkerSnapshot,
    iso,
    utcnow,
)

log = logging.getLogger(__name__)

# -- 注册表评论的机器可读标记（兼容 adapters/github_issue.py 的写法） ----------
_REGISTRY_RE = re.compile(
    r"<!--\s*worker-registry:\s*(?P<agent>[a-z0-9][a-z0-9-]*),\s*at:\s*(?P<at>[0-9T:Z+-]+)\s*-->"
)
_HEARTBEAT_RE = re.compile(
    r"<!--\s*worker-heartbeat:\s*(?P<agent>[a-z0-9][a-z0-9-]*),\s*at:\s*(?P<at>[0-9T:Z+-]+)\s*-->"
)
_INSTANCE_RE = re.compile(
    r"<!--\s*worker-instance:\s*(?P<instance>[^,>]+),\s*hostname:\s*(?P<hostname>[^,>]*),"
    r"\s*platform:\s*(?P<platform>[^>]*?)\s*-->"
)
_META_RE = re.compile(r"<!--\s*worker-meta:\s*(?P<payload>.+?)\s*-->")

#: 心跳评论节流（默认 60s；GitHub 评论与 refs 都有速率限制）。
DEFAULT_HEARTBEAT_POST_INTERVAL = timedelta(seconds=60)


def _parse_at(raw: str) -> Optional[datetime]:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=utcnow().tzinfo
        )
    except (TypeError, ValueError):
        return None


def _meta_fields(payload: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in payload.split(","):
        key, _, value = part.partition("=")
        if key.strip():
            fields[key.strip().lower()] = value.strip()
    return fields


class WorkerRegistryMixin:
    """`GitHubRefScheduler` 的 Worker 注册表部分（依赖宿主的 transport/now）。"""

    registry_issue: Optional[int]
    heartbeat_ttl: timedelta
    heartbeat_post_interval: timedelta
    _workers: dict[tuple[str, str], dict[str, Any]]
    _last_heartbeat_post: dict[tuple[str, str], datetime]

    # -- 评论生成 ------------------------------------------------------------
    def _worker_comment(
        self,
        kind: str,
        *,
        agent_id: str,
        instance_id: str,
        hostname: str = "",
        platform: str = "",
        capabilities: Sequence[str] = (),
        now: Optional[datetime] = None,
    ) -> str:
        at = iso(now or self.now())
        head = (
            f"<!-- worker-registry: {agent_id}, at: {at} -->"
            if kind == "registry"
            else f"<!-- worker-heartbeat: {agent_id}, at: {at} -->"
        )
        caps = ",".join(str(cap) for cap in capabilities)
        lines = [
            head,
            f"<!-- worker-instance: {instance_id}, hostname: {hostname}, platform: {platform} -->",
            f"<!-- worker-meta: agent={agent_id}, instance={instance_id}, "
            f"hostname={hostname}, platform={platform}, capabilities={caps} -->",
        ]
        if kind == "registry":
            lines.append(
                f"Worker 注册：`{agent_id}`（实例：{instance_id or '未注明'}，"
                f"主机：{hostname or '未注明'}，平台：{platform or '未注明'}，"
                f"能力：{caps or '未注明'}）。"
            )
        else:
            lines.append(f"Worker 心跳：`{agent_id}`（实例：{instance_id or '未注明'}）在线。")
        return "\n".join(lines)

    def _post_registry(self, body: str, author: str = "") -> None:
        try:
            self.transport.add_comment(self.registry_issue, body, author=author)
        except Exception as exc:  # noqa: BLE001 - 注册表评论失败不阻塞任务调度
            raise SchedulerError(
                f"注册表 Issue #{self.registry_issue} 评论失败：{exc}"
            ) from exc

    def _post_registry_best_effort(self, body: str, author: str = "") -> bool:
        """评论通道是兼容层：失败只记日志，不阻塞 refs 主通道。

        refs beat 永远先写；评论写不出时心跳本身仍算成功（读侧以 refs 为准）。
        """
        try:
            self._post_registry(body, author=author)
            return True
        except SchedulerError as exc:
            log.warning("心跳评论写失败（refs beat 不受影响）：%s", exc)
            return False

    # -- 注册 / 心跳 ---------------------------------------------------------
    def _post_refs_beat(
        self, agent_id: str, instance_id: str, state: str, now: datetime
    ) -> bool:
        """写一条 refs 心跳 beat；无 ref_store/anchor 时静默跳过（降级为旧行为）。

        节流由调用方（register/heartbeat_worker）在写双通道前统一判定，这里
        不再二次节流——否则评论先写会把同一轮的 beat 也节流掉。
        """
        ref_store = getattr(self, "ref_store", None)
        anchor = getattr(self, "anchor", None)
        if ref_store is None or anchor is None:
            return False
        try:
            anchor_sha = anchor()
            posted, _ref = ref_heartbeat.post_heartbeat(
                ref_store,
                anchor_sha,
                agent_id,
                instance_id,
                state,
                now,
                interval=self.heartbeat_post_interval,
                last_post=None,
            )
        except (SchedulerError, ValueError):
            return False
        if posted:
            # 顺手清理过期 beat（best-effort，失败不影响心跳本身）。
            try:
                ref_heartbeat.prune_heartbeats(ref_store, now)
            except Exception:  # noqa: BLE001 - 清理失败不阻塞
                pass
        return posted

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
        # 身份统一小写：ref 名只接受小写，beat 键也是小写；注册键保留原始大小写
        # 会与 beat 键分裂（同一 Worker 在 list_workers 里出现两行）。
        agent_id = str(agent_id).lower()
        instance_id = str(instance_id).lower()
        now = self.now()
        key = (agent_id, instance_id)
        entry = {
            "agent_id": agent_id,
            "instance_id": instance_id,
            "hostname": hostname,
            "platform": platform,
            "capabilities": tuple(str(cap) for cap in capabilities),
            "registered_at": now,
            "last_heartbeat_at": now,
        }
        self._workers[key] = entry
        # 窗口内重复注册：本地条目照常更新，不重复写通道（与 heartbeat 节流一致）。
        last = self._last_heartbeat_post.get(key)
        if last is not None and now - last < self.heartbeat_post_interval:
            return self._worker_snapshot(entry, state="online")
        posted = False
        # refs 通道先写（主通道）：注册即一次心跳（state=online）。
        if self._post_refs_beat(agent_id, instance_id, "online", now):
            posted = True
        # 评论通道后写，失败不阻塞注册（best-effort，见 _post_registry_best_effort）。
        if self.registry_issue is not None:
            if self._post_registry_best_effort(
                self._worker_comment(
                    "registry",
                    agent_id=agent_id,
                    instance_id=instance_id,
                    hostname=hostname,
                    platform=platform,
                    capabilities=capabilities,
                    now=now,
                ),
                agent_id,
            ):
                posted = True
        if posted:
            self._last_heartbeat_post[key] = now
        return self._worker_snapshot(entry, state="online")

    def heartbeat_worker(
        self, agent_id: str, instance_id: str, state: str = "online"
    ) -> bool:
        if state not in ref_heartbeat.HEARTBEAT_STATES:
            raise ValueError(
                f"心跳状态必须是 {sorted(ref_heartbeat.HEARTBEAT_STATES)} 之一：{state!r}"
            )
        agent_id = str(agent_id).lower()
        instance_id = str(instance_id).lower()
        key = (agent_id, instance_id)
        now = self.now()
        entry = self._workers.get(key)
        if entry is None and self.registry_issue is None:
            # 无 registry issue：本地表里没有就如实返回 False。refs 通道不改变
            # 这一条——心跳前必须先 register_worker（注册时即写第一条 beat）。
            return False
        if entry is not None:
            entry["last_heartbeat_at"] = now
        # 双通道共用一次节流判定：窗口内不写任何通道，但心跳被接受（省 API 配额）。
        last = self._last_heartbeat_post.get(key)
        if last is not None and now - last < self.heartbeat_post_interval:
            return True
        posted = False
        # refs 主通道先写；评论通道后写且失败不阻塞（best-effort）。
        if self._post_refs_beat(agent_id, instance_id, state, now):
            posted = True
        if self.registry_issue is not None:
            if self._post_registry_best_effort(
                self._worker_comment(
                    "heartbeat",
                    agent_id=agent_id,
                    instance_id=instance_id,
                    hostname=str((entry or {}).get("hostname") or ""),
                    platform=str((entry or {}).get("platform") or ""),
                    capabilities=tuple((entry or {}).get("capabilities") or ()),
                    now=now,
                ),
                agent_id,
            ):
                posted = True
        if posted:
            self._last_heartbeat_post[key] = now
        if self.registry_issue is None and getattr(self, "ref_store", None) is None:
            # 无任何通道可写（纯本地模式）：返回本地是否已注册。
            return entry is not None
        # 有通道可写时，返回是否真的写出了至少一条心跳——写失败不得谎报 True。
        return posted

    # -- 列表 ----------------------------------------------------------------
    @staticmethod
    def _worker_snapshot(entry: Mapping[str, Any], *, state: str) -> WorkerSnapshot:
        return WorkerSnapshot(
            agent_id=str(entry.get("agent_id") or ""),
            instance_id=str(entry.get("instance_id") or ""),
            hostname=str(entry.get("hostname") or ""),
            platform=str(entry.get("platform") or ""),
            capabilities=tuple(entry.get("capabilities") or ()),
            state=state,
            last_heartbeat_at=entry.get("last_heartbeat_at"),
            registered_at=entry.get("registered_at"),
        )

    def _live_lease_keys(self, now: datetime) -> set[tuple[str, str]]:
        """持有有效租约的 (agent, instance) 集合（refs 通道；读失败返回空集）。"""
        ref_store = getattr(self, "ref_store", None)
        if ref_store is None:
            return set()
        try:
            infos = list(ref_store.list(ref_api.LEASE_ROOT + "/") or [])
        except Exception:  # noqa: BLE001 - 租约读失败时降级，不谎报 working
            return set()
        leases = [
            lease
            for lease in (
                ref_api.parse_lease(getattr(info, "ref", "") or "") for info in infos
            )
            if lease is not None
        ]
        ttl_ms = ref_api.ttl_ms(
            getattr(self, "lease_ttl", DEFAULT_LEASE_TTL)
        )
        now_ms = ref_heartbeat.epoch_ms_of(now)
        # 注意：不能用 ref_api.live_lease（它取 token 最大的一条，是单任务语义）；
        # 这里要的是“该 Worker 有没有任意一条未过期租约”，逐条 is_live。
        return {
            (lease.agent_id, lease.instance_id)
            for lease in leases
            if lease.is_live(now_ms, ttl_ms)
        }

    def list_workers(
        self, stale_after: timedelta = DEFAULT_HEARTBEAT_TTL
    ) -> list[WorkerSnapshot]:
        now = self.now()
        if self.registry_issue is not None:
            facts = self._registry_facts()
        else:
            # 读方法不得写本进程状态：facts 必须是副本（每条目也拷一份），否则
            # 外来 beat 的 setdefault 会把未注册身份写进 self._workers，绕过
            # “心跳前必须先注册”的守卫（#141 复审 P1-2）。
            facts = {key: dict(entry) for key, entry in self._workers.items()}
        # refs 通道的心跳事实与评论/本地事实合并：取最新心跳时刻与最新自报状态。
        reported: dict[tuple[str, str], str] = {}
        ref_store = getattr(self, "ref_store", None)
        if ref_store is not None:
            try:
                beats = ref_heartbeat.read_heartbeats(ref_store)
            except SchedulerError:
                beats = {}
            for key, beat in beats.items():
                entry = facts.setdefault(
                    key,
                    {
                        "agent_id": key[0],
                        "instance_id": key[1],
                        "hostname": "",
                        "platform": "",
                        "capabilities": (),
                        "registered_at": None,
                        "last_heartbeat_at": None,
                    },
                )
                last = entry.get("last_heartbeat_at")
                if last is None or beat.at > last:
                    entry["last_heartbeat_at"] = beat.at
                    reported[key] = beat.state
                elif key not in reported:
                    reported[key] = beat.state
        live_leases = self._live_lease_keys(now)
        result: list[WorkerSnapshot] = []
        for key in sorted(facts):
            entry = dict(facts[key])
            last = entry.get("last_heartbeat_at") or entry.get("registered_at")
            # 租约键归一：Linux Worker 认领用租约身份 (agent, ref_instance_hex)，
            # 注册/心跳用人类身份 (agent, instance)；任一键持有有效租约即算 working。
            has_live_lease = key in live_leases or (
                key[0],
                ref_heartbeat.ref_instance_id(key[1]),
            ) in live_leases
            state = ref_heartbeat.derive_worker_state(
                last_heartbeat_at=last,
                now=now,
                stale_after=stale_after,
                reported_state=reported.get(key, "online"),
                has_live_lease=has_live_lease,
            )
            result.append(self._worker_snapshot(entry, state=state))
        return result

    def _registry_facts(self) -> dict[tuple[str, str], dict[str, Any]]:
        """分页读注册表 Issue 评论，按 (agent, instance) 取最新事实。"""
        try:
            comments = list(self.transport.list_comments(self.registry_issue))
        except Exception as exc:  # noqa: BLE001
            raise SchedulerError(f"读取注册表 Issue 评论失败：{exc}") from exc
        facts: dict[tuple[str, str], dict[str, Any]] = {}
        for comment in comments:
            body = getattr(comment, "body", "") or ""
            meta_match = _META_RE.search(body)
            fields = _meta_fields(meta_match.group("payload")) if meta_match else {}
            agent = fields.get("agent", "")
            instance = fields.get("instance", "")
            if not agent:
                match = _REGISTRY_RE.search(body) or _HEARTBEAT_RE.search(body)
                agent = match.group("agent") if match else ""
            if not instance:
                match = _INSTANCE_RE.search(body)
                instance = (match.group("instance").strip() if match else "")
            if not agent or not instance:
                continue
            entry = facts.setdefault(
                (agent, instance),
                {
                    "agent_id": agent,
                    "instance_id": instance,
                    "hostname": "",
                    "platform": "",
                    "capabilities": (),
                    "registered_at": None,
                    "last_heartbeat_at": None,
                },
            )
            for name in ("hostname", "platform"):
                if fields.get(name):
                    entry[name] = fields[name]
            if fields.get("capabilities"):
                entry["capabilities"] = tuple(
                    part for part in fields["capabilities"].split(",") if part
                )
            instance_match = _INSTANCE_RE.search(body)
            if instance_match:
                entry["hostname"] = entry["hostname"] or instance_match.group("hostname").strip()
                entry["platform"] = entry["platform"] or instance_match.group("platform").strip()
            registry = _REGISTRY_RE.search(body)
            heartbeat = _HEARTBEAT_RE.search(body)
            at_raw = (registry or heartbeat).group("at") if (registry or heartbeat) else ""
            at = _parse_at(at_raw) if at_raw else None
            if at is not None:
                if registry:
                    entry["registered_at"] = entry["registered_at"] or at
                last = entry["last_heartbeat_at"]
                entry["last_heartbeat_at"] = at if last is None else max(last, at)
        return facts


__all__ = ["DEFAULT_HEARTBEAT_POST_INTERVAL", "WorkerRegistryMixin"]
