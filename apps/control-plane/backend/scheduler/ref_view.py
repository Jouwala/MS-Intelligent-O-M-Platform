"""ref 权威层的「评论标记 + 派生视图」纯函数层（无网络、无状态）。

refs 是权威，评论只用于两件事：**审计**与**派生任务状态**（`api.Scheduler.get_task`
需要 state / error / 完成时间等展示字段，而这些不在 ref 名里）。本模块把评论里的
机器可读标记集中成唯一解析实现，供 `ref_claim.py` / `ref_fencing.py` /
`ref_completion.py` / `ref_reap.py` / `ref_service.py` 共用：

- 认领/续租/完成标记：由冻结契约 `ref_api` 定义（`ref-api` 只写了 body 生成器）；
- 状态迁移标记：`<!-- ref-state: issue=N, token=T, state=s, at=ISO -->`
  （本模块 `state_comment_body` 生成；行尾可跟 `error_code:` / `error_message:` / `reason:`）。

派生优先级（`derive_state`）：
1. 完成标记（`ref-completion-id`，带 status）→ 终态，优先于一切；
2. 最新状态标记为终态（cancelled/failed/completed）→ 终态；
3. 有存活租约 → 最新状态标记（running/blocked/cancel_requested）否则 claimed；
4. 无存活租约但有该代次已过期 lease ref → expired（等 Reaper/重认领）；
5. 最新状态标记（retryable/queued/…）；
6. 都没有 → queued。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from . import ref_api
from .api import (
    BLOCKED,
    CANCEL_REQUESTED,
    CANCELLED,
    CLAIMED,
    COMPLETED,
    EXPIRED,
    QUEUED,
    RETRYABLE,
    RUNNING,
    TERMINAL_STATES,
    SchedulerError,
    iso,
)

#: 状态标记（所有迁移/取消/回收都写一条）。
_STATE_MARKER_RE = re.compile(
    r"<!--\s*ref-state:\s*issue=(?P<issue>\d+),\s*token=(?P<token>\d+),"
    r"\s*state=(?P<state>[a-z_]+),\s*at=(?P<at>[^>]+?)\s*-->"
)
_COMPLETION_ID_RE = re.compile(r"<!--\s*ref-completion-id:\s*(?P<key>[^>]+?)\s*-->")
_LEASE_PAYLOAD_RE = re.compile(r"<!--\s*ref-lease:\s*(?P<payload>.+?)\s*-->")
_AGENT_MSG_AT_RE = re.compile(r"<!--\s*agent-msg:[^>]*?at:\s*(?P<at>[0-9T:Z+-]+)\s*-->")
_EXTRA_LINE_RE = re.compile(
    r"^(?P<name>error_code|error_message|reason):\s*(?P<value>.*)$", re.M
)
_TASK_ID_RE = re.compile(r"^issue-(?P<number>\d+)$")


def task_id_of(issue_number: int) -> str:
    return f"issue-{int(issue_number)}"


def issue_number_for(task_id: str) -> int:
    """`issue-<N>` → N（与 worker.task_id_for_issue 的约定一致）。"""
    match = _TASK_ID_RE.match(task_id or "")
    if not match:
        raise SchedulerError(f"ref 权威层需要 task_id 形如 issue-<N>，实际：{task_id!r}")
    return int(match.group("number"))


def as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def one_line(text: object) -> str:
    """错误/摘要压成一行（标记按行解析，不能带换行）。"""
    return " ".join(str(text or "").split())


def parse_marker_at(raw: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class StateMarker:
    state: str
    token: int
    at: Optional[datetime] = None
    error_code: str = ""
    error_message: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CompletionRecord:
    idempotency_key: str
    token: int = 0
    attempt_id: str = ""
    status: str = ""
    at: Optional[datetime] = None


def _payload_fields(body: str) -> dict[str, str]:
    match = _LEASE_PAYLOAD_RE.search(body or "")
    if not match:
        return {}
    fields: dict[str, str] = {}
    for part in match.group("payload").split(","):
        key, _, value = part.partition("=")
        if key.strip():
            fields[key.strip()] = value.strip()
    return fields


def parse_state_markers(comment_bodies: Sequence[str]) -> list[StateMarker]:
    markers: list[StateMarker] = []
    for body in comment_bodies:
        match = _STATE_MARKER_RE.search(body or "")
        if not match:
            continue
        extra = {m.group("name"): m.group("value").strip() for m in _EXTRA_LINE_RE.finditer(body)}
        markers.append(
            StateMarker(
                state=match.group("state"),
                token=int(match.group("token")),
                at=parse_marker_at(match.group("at")),
                error_code=extra.get("error_code", ""),
                error_message=extra.get("error_message", ""),
                reason=extra.get("reason", ""),
            )
        )
    return markers


def parse_completions(comment_bodies: Sequence[str]) -> list[CompletionRecord]:
    records: list[CompletionRecord] = []
    for body in comment_bodies:
        key_match = _COMPLETION_ID_RE.search(body or "")
        if not key_match:
            continue
        fields = _payload_fields(body)
        at_match = _AGENT_MSG_AT_RE.search(body)
        records.append(
            CompletionRecord(
                idempotency_key=key_match.group("key").strip(),
                token=int(fields.get("token", "0") or 0),
                attempt_id=(fields.get("attempt", "") or "").lower(),
                status=(fields.get("status", "") or "").lower(),
                at=parse_marker_at(at_match.group("at")) if at_match else None,
            )
        )
    return records


def terminal_state_of(markers: Sequence[StateMarker]) -> Optional[str]:
    """最新一条状态标记若是终态则返回它（旧算力/新认领会写新标记，只看最新）。"""
    if not markers:
        return None
    latest = markers[-1]
    return latest.state if latest.state in TERMINAL_STATES else None


def state_comment_body(
    *,
    issue_number: int,
    token: int,
    state: str,
    timestamp: datetime,
    error_code: str = "",
    error_message: str = "",
    reason: str = "",
) -> str:
    """状态迁移/取消/回收评论：机器可读标记 + 人类可读说明。"""
    lines = [
        f"<!-- ref-state: issue={int(issue_number)}, token={int(token):08d}, "
        f"state={state}, at={iso(timestamp)} -->"
    ]
    if error_code:
        lines.append(f"error_code: {one_line(error_code)}")
    if error_message:
        lines.append(f"error_message: {one_line(error_message)}")
    if reason:
        lines.append(f"reason: {one_line(reason)}")
    lines.append(f"任务 {task_id_of(issue_number)} 状态更新为 `{state}`（token={int(token):08d}）。")
    lines.append("> 权威状态在 GitHub refs（agent-hwm / agent-lease），评论仅用于派生与审计。")
    return "\n".join(lines)


@dataclass(frozen=True)
class TaskRefView:
    """从 refs + 评论派生的任务视图（展示/状态机校验用，不是权威存储）。"""

    task_id: str
    issue_number: int
    repo: str = ""
    title: str = ""
    state: str = QUEUED
    terminal_status: Optional[str] = None
    max_token: int = 0
    live: Optional[ref_api.LeaseRef] = None
    completion: Optional[CompletionRecord] = None
    marker: Optional[StateMarker] = None
    attempt_id: str = ""
    agent_id: str = ""
    instance_id: str = ""
    idempotency_key: str = ""
    error_code: str = ""
    error_message: str = ""
    claimed_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancel_requested_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    scopes: tuple[str, ...] = ()


def derive_state(
    *,
    max_token: int,
    live: Optional[ref_api.LeaseRef],
    has_expired_lease: bool,
    completions: Sequence[CompletionRecord],
    markers: Sequence[StateMarker],
) -> tuple[str, Optional[str]]:
    """派生 (state, terminal_status)；优先级见模块文档。

    关键安全规则：**租约 ref 已被清掉（Reaper 回收/释放）时，旧状态标记不能把任务
    显示成「仍在执行」**；而 cancel_requested 的持有者一旦失联且租约被回收，必须
    派生为 cancelled（终态），否则新实例会把取消中的任务复活。
    """
    if completions:
        latest = max(
            completions,
            key=lambda r: (r.token, r.at or datetime.min.replace(tzinfo=timezone.utc)),
        )
        status = latest.status if latest.status in TERMINAL_STATES else COMPLETED
        return status, status
    terminal = terminal_state_of(markers)
    if terminal is not None:
        return terminal, terminal
    if live is not None:
        if markers and markers[-1].state in (RUNNING, BLOCKED, CANCEL_REQUESTED, CLAIMED):
            return markers[-1].state, None
        return CLAIMED, None
    if has_expired_lease:
        # 租约 ref 还在但已过期、尚未被 Reaper 清掉：与 SQLite 语义一致，仍显示
        # 持有者最后一次状态（claimed/running/blocked/cancel_requested），
        # 过期本身不改变状态机（只决定「谁还能认领」）。
        if markers and markers[-1].state in (CLAIMED, RUNNING, BLOCKED, CANCEL_REQUESTED):
            return markers[-1].state, None
        return EXPIRED, None
    if markers:
        latest = markers[-1].state
        if latest == CANCEL_REQUESTED:
            # 取消请求 + 租约已不在（过期被回收）：取消生效，不可被新实例复活
            return CANCELLED, CANCELLED
        if latest in (CLAIMED, RUNNING, BLOCKED, EXPIRED):
            return RETRYABLE, None
        return latest, None
    return QUEUED, None


def build_view(
    *,
    task_id: str,
    issue_number: int,
    max_token: int,
    lease_refs: Sequence[ref_api.LeaseRef],
    live: Optional[ref_api.LeaseRef],
    comment_bodies: Sequence[str],
    repo: str = "",
    title: str = "",
    scopes: Sequence[str] = (),
) -> TaskRefView:
    """把 refs 事实 + 评论正文组装成视图（`RefLeaseManager.derive` 的纯函数部分）。"""
    ttl_like = [lease for lease in lease_refs if lease.token == max_token]
    has_expired = bool(ttl_like) and live is None
    completions = parse_completions(comment_bodies)
    markers = parse_state_markers(comment_bodies)
    state, terminal_status = derive_state(
        max_token=max_token,
        live=live,
        has_expired_lease=has_expired,
        completions=completions,
        markers=markers,
    )
    completion = completions[-1] if completions else None
    marker = markers[-1] if markers else None
    claimed_at = (
        datetime.fromtimestamp(live.epoch_ms / 1000, timezone.utc) if live is not None else None
    )
    updated = [value for value in (
        completion.at if completion else None,
        marker.at if marker else None,
    ) if value]
    return TaskRefView(
        task_id=task_id,
        issue_number=int(issue_number),
        repo=repo,
        title=title,
        state=state,
        terminal_status=terminal_status,
        max_token=max_token,
        live=live,
        completion=completion,
        marker=marker,
        attempt_id=(
            live.attempt_id
            if live is not None
            else (completion.attempt_id if completion is not None else "")
        ),
        agent_id=live.agent_id if live is not None else "",
        instance_id=live.instance_id if live is not None else "",
        idempotency_key=completion.idempotency_key if completion is not None else "",
        error_code=marker.error_code if marker is not None else "",
        error_message=marker.error_message if marker is not None else "",
        claimed_at=claimed_at,
        heartbeat_at=claimed_at,
        completed_at=(
            completion.at
            if completion is not None
            else (marker.at if marker is not None and marker.state in TERMINAL_STATES else None)
        ),
        cancel_requested_at=(
            marker.at if marker is not None and marker.state == CANCEL_REQUESTED else None
        ),
        updated_at=max(updated) if updated else claimed_at,
        scopes=tuple(scopes),
    )
