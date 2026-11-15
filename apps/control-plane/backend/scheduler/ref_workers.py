"""PR-5：Worker 注册表（GitHub 原生后端）——注册表 Issue 评论实现。

`api.Scheduler` 的 `register_worker / heartbeat_worker / list_workers` 在 GitHub
原生权威层里没有本地表可写（ADR-7 §零：状态全落 GitHub、不依赖本地 DB），因此用
**注册表 Issue 的评论**承载（与 `adapters/github_issue.py` 的既有写法兼容）：

- 注册：`<!-- worker-registry: <agent>, at: ISO -->`；
- 心跳：`<!-- worker-heartbeat: <agent>, at: ISO -->`（默认 60s 节流，省 API 配额）；
- 实例：`<!-- worker-instance: <instance>, hostname: ..., platform: ... -->`；
- 扩展：`<!-- worker-meta: agent=..., instance=..., hostname=..., platform=...,
  capabilities=a,b -->`（结构化字段；没有 meta 的旧评论也能解析）。

`list_workers` 分页读评论，按 `(agent, instance)` 取最新事实并计算 active/stale。
**无 registry issue 时本地 no-op**：只在进程内记一份展示快照，不报错（B2 的
factory 默认 `registry_issue=None`），与 SQLite 版「无注册表」行为一致。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Sequence

from .api import (
    DEFAULT_HEARTBEAT_TTL,
    SchedulerError,
    WorkerSnapshot,
    iso,
    utcnow,
)

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

    # -- 注册 / 心跳 ---------------------------------------------------------
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
        now = self.now()
        entry = {
            "agent_id": agent_id,
            "instance_id": instance_id,
            "hostname": hostname,
            "platform": platform,
            "capabilities": tuple(str(cap) for cap in capabilities),
            "registered_at": now,
            "last_heartbeat_at": now,
        }
        self._workers[(agent_id, instance_id)] = entry
        if self.registry_issue is not None:
            self._post_registry(
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
            )
            self._last_heartbeat_post[(agent_id, instance_id)] = now
        return self._worker_snapshot(entry, state="active")

    def heartbeat_worker(self, agent_id: str, instance_id: str) -> bool:
        key = (agent_id, instance_id)
        now = self.now()
        entry = self._workers.get(key)
        if entry is None and self.registry_issue is None:
            return False  # 无 registry issue：本地表里没有就如实返回 False
        if entry is not None:
            entry["last_heartbeat_at"] = now
        if self.registry_issue is None:
            return entry is not None
        last = self._last_heartbeat_post.get(key)
        if last is not None and now - last < self.heartbeat_post_interval:
            return True  # 节流窗口内也算心跳被接受（不写评论，省 API 配额）
        self._post_registry(
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
        )
        self._last_heartbeat_post[key] = now
        return True

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

    def list_workers(
        self, stale_after: timedelta = DEFAULT_HEARTBEAT_TTL
    ) -> list[WorkerSnapshot]:
        now = self.now()
        facts = self._registry_facts() if self.registry_issue is not None else self._workers
        result: list[WorkerSnapshot] = []
        for key in sorted(facts):
            entry = dict(facts[key])
            last = entry.get("last_heartbeat_at") or entry.get("registered_at")
            state = "unknown"
            if last is not None:
                state = "active" if now - last <= stale_after else "stale"
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
