"""状态视图的数据收集（Phase 3 P3-2，Issue #142）。

只读：Worker 七态读心跳 refs + 租约 refs（与 `list_workers` 同源）；
任务状态读 Issue 标签 + 时间线事件；审核/冒烟读 PR 评论里的机器可读标记。
不新增中心服务；判定口径见 `status_view.py` 模块 docstring。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Optional

from .alert_inbox import parse_iso
from .github_client import GitHubClient
from .review_gate import parse_review_markers
from scheduler import ref_api, ref_heartbeat
from scheduler.api import DEFAULT_HEARTBEAT_TTL, DEFAULT_LEASE_TTL

#: 任务 Issue 的 agent 标签。
_AGENT_LABEL_RE = re.compile(r"^agent:muse-(\d+)$")

#: 冒烟结果标记（SHA 可能是 7–40 位：部分评论用短 SHA）。
_SMOKE_MARKER_RE = re.compile(
    r"smoke-result:\s*pr-(?P<pr>\d+)\s+"
    r"sha-(?P<sha>[0-9a-f]{7,40})\s+"
    r"result=(?P<result>passed|failed)"
)

#: 自动修复轮次标记（qa-fix-loop）：`<!-- auto-fix: for-issue-<N> ... round-<K> ... -->`。
_AUTO_FIX_MARKER_RE = re.compile(
    r"auto-fix:\s*for-issue-(?P<issue>\d+)\b.*?round-(?P<round>\d+)"
)

#: 审核退回率的统计窗口（open PR + 窗口内合入的 PR）。
REVIEW_WINDOW = timedelta(days=30)

#: 每任务行最多展示条数（按 Issue 编号倒序，超出时正文注明）。
TASK_ROW_LIMIT = 50

#: 自动合入的 merger 登录名。
AUTO_MERGE_LOGIN = "github-actions[bot]"

#: 任务状态标签的展示优先级（一个任务同时带多个 state: 标签时取最严重的）。
_STATE_ORDER = (
    "state:blocked",
    "state:qa-failed",
    "state:claimed",
    "state:todo",
    "state:merged",
)


def _labels(issue: dict[str, Any]) -> set[str]:
    return {
        str(item.get("name") or "")
        for item in (issue.get("labels") or [])
        if isinstance(item, dict)
    }


def _agent_of(labels: set[str]) -> Optional[str]:
    for label in labels:
        match = _AGENT_LABEL_RE.match(label)
        if match:
            return f"muse-{match.group(1)}"
    return None


def _median_seconds(values: list[float]) -> Optional[float]:
    return float(median(values)) if values else None


# ---------------------------------------------------------------------------
# 数据收集（只读；复用现有读接口）
# ---------------------------------------------------------------------------


def collect_workers(
    client: GitHubClient, now: datetime
) -> dict[str, Any]:
    """Worker 七态：最新心跳 beat + 有效租约，`derive_worker_state` 推导。

    只统计有心跳 beat 的 Worker（与 `list_workers` 一致：租约只升级已知
    Worker 的状态，不凭空造出一个 Worker）。Linux Worker 的租约身份是
    hex（`ref_instance_id`），查租约时原始键与派生键都查。"""
    beats: dict[tuple[str, str], Any] = {}
    for item in client.list_matching_refs("heads/agent-heartbeat/"):
        beat = ref_heartbeat.parse_heartbeat_ref(str(item.get("ref") or ""))
        if beat is None:
            continue
        key = (beat.agent_id, beat.instance_id)
        current = beats.get(key)
        if current is None or beat.epoch_ms > current.epoch_ms:
            beats[key] = beat
    live_leases: set[tuple[str, str]] = set()
    now_ms = int(now.timestamp() * 1000)
    ttl_ms = int(DEFAULT_LEASE_TTL.total_seconds() * 1000)
    for item in client.list_matching_refs("heads/agent-lease/"):
        lease = ref_api.parse_lease(str(item.get("ref") or ""))
        if lease is None:
            continue
        if lease.is_live(now_ms, ttl_ms):
            live_leases.add((lease.agent_id, lease.instance_id))
    workers: list[dict[str, Any]] = []
    for (agent, instance), beat in sorted(beats.items()):
        has_live_lease = (agent, instance) in live_leases or (
            agent,
            ref_heartbeat.ref_instance_id(instance),
        ) in live_leases
        state = ref_heartbeat.derive_worker_state(
            last_heartbeat_at=beat.at,
            now=now,
            stale_after=DEFAULT_HEARTBEAT_TTL,
            reported_state=beat.state,
            has_live_lease=has_live_lease,
        )
        workers.append(
            {
                "agent": agent,
                "instance": instance,
                "state": state,
                "reported": beat.state,
                "last_beat": beat.at,
                "has_live_lease": has_live_lease,
            }
        )
    counts: dict[str, int] = {state: 0 for state in sorted(ref_heartbeat.WORKER_STATES)}
    for worker in workers:
        counts[worker["state"]] = counts.get(worker["state"], 0) + 1
    return {"workers": workers, "counts": counts}


def _first_labeled_at(
    events: list[dict[str, Any]], label: str
) -> Optional[datetime]:
    """Issue 时间线里某标签最早一次打上的时间；没有返回 None。"""
    earliest: Optional[datetime] = None
    for event in events:
        if str(event.get("event") or "") != "labeled":
            continue
        name = str((event.get("label") or {}).get("name") or "")
        if name != label:
            continue
        moment = parse_iso(event.get("created_at"))
        if moment and (earliest is None or moment < earliest):
            earliest = moment
    return earliest


def collect_tasks(client: GitHubClient, now: datetime) -> dict[str, Any]:
    """任务 Issue（带 agent:muse-* 标签、非 PR）：状态分类 + 等待/执行耗时。

    等待 = 创建→认领（未认领的算到现在）；执行 = 认领→关闭（执行中的算到
    现在）；恢复 = 首次贴 state:qa-failed→关闭（未恢复的算到现在）。
    认领/QA 失败时间取时间线事件（标签可能事后被摘，事件不会消失）。"""
    issues = client.list_issues(state="all")
    tasks: list[dict[str, Any]] = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        labels = _labels(issue)
        agent = _agent_of(labels)
        if agent is None:
            continue
        number = int(issue["number"])
        created_at = parse_iso(issue.get("created_at")) or now
        closed_at = parse_iso(issue.get("closed_at"))
        state = "other"
        for candidate in _STATE_ORDER:
            if candidate in labels:
                state = candidate
                break
        if issue.get("state") == "closed" and state == "other":
            state = "state:merged"
        events = client.list_issue_events(number)
        claimed_at = _first_labeled_at(events, "state:claimed")
        qa_failed_at = _first_labeled_at(events, "state:qa-failed")
        wait = (claimed_at - created_at).total_seconds() if claimed_at else (now - created_at).total_seconds()
        exec_seconds: Optional[float] = None
        if claimed_at:
            end = closed_at or now
            exec_seconds = (end - claimed_at).total_seconds()
        recovery: Optional[float] = None
        if qa_failed_at:
            recovery = ((closed_at or now) - qa_failed_at).total_seconds()
        tasks.append(
            {
                "number": number,
                "title": str(issue.get("title") or ""),
                "agent": agent,
                "state": state,
                "created_at": created_at,
                "closed_at": closed_at,
                "claimed_at": claimed_at,
                "qa_failed_at": qa_failed_at,
                "wait_seconds": wait,
                "exec_seconds": exec_seconds,
                "recovery_seconds": recovery,
            }
        )
    by_state: dict[str, int] = {}
    for task in tasks:
        by_state[task["state"]] = by_state.get(task["state"], 0) + 1
    claimed = [t for t in tasks if t["claimed_at"] is not None]
    closed_claimed = [t for t in tasks if t["claimed_at"] is not None and t["closed_at"] is not None]
    qa_failed_ever = [t for t in tasks if t["qa_failed_at"] is not None]
    running = [t for t in tasks if t["state"] == "state:claimed"]
    waiting = [t for t in tasks if t["state"] == "state:todo"]
    return {
        "tasks": tasks,
        "by_state": by_state,
        "total": len(tasks),
        "unclaimed": len(waiting),
        "running": len(running),
        "blocked": by_state.get("state:blocked", 0),
        "qa_failed_now": by_state.get("state:qa-failed", 0),
        "qa_failed_ever": len(qa_failed_ever),
        "claimed_ever": len(claimed),
        "median_wait": _median_seconds([t["wait_seconds"] for t in claimed]),
        "median_exec": _median_seconds(
            [t["exec_seconds"] for t in closed_claimed if t["exec_seconds"] is not None]
        ),
        "longest_waiter": max(waiting, key=lambda t: t["wait_seconds"], default=None),
        "longest_running": max(
            (t for t in running if t["exec_seconds"] is not None),
            key=lambda t: t["exec_seconds"],
            default=None,
        ),
        "median_recovery": _median_seconds(
            [t["recovery_seconds"] for t in qa_failed_ever if t["recovery_seconds"] is not None]
        ),
    }


def _latest_smoke(bodies: list[str]) -> Optional[dict[str, Any]]:
    """PR 评论里最后一条冒烟标记；没有返回 None。"""
    latest: Optional[dict[str, Any]] = None
    for body in bodies or []:
        for match in _SMOKE_MARKER_RE.finditer(body or ""):
            latest = {
                "pr": int(match.group("pr")),
                "sha": match.group("sha"),
                "result": match.group("result"),
            }
    return latest


def _auto_fix_rounds(bodies: list[str]) -> set[tuple[int, int]]:
    """评论里的自动修复轮次标记集合（任务，轮次）。"""
    rounds: set[tuple[int, int]] = set()
    for body in bodies or []:
        for match in _AUTO_FIX_MARKER_RE.finditer(body or ""):
            rounds.add((int(match.group("issue")), int(match.group("round"))))
    return rounds


#: PR 正文/分支名里关联任务 Issue 的写法（与 pr-merged-writeback 同顺序）。
_LINK_CLOSES_RE = re.compile(r"(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)
_LINK_TASK_RE = re.compile(r"task:\s*#(\d+)", re.IGNORECASE)
_LINK_BRANCH_RE = re.compile(r"agent/task-(\d+)(?:-|$)")


def _linked_issues(pr: dict[str, Any]) -> list[int]:
    """从 PR 正文（Closes #N / task: #N）与分支名取关联的任务 Issue 编号。"""
    linked: list[int] = []
    body = str(pr.get("body") or "")
    head_ref = str((pr.get("head") or {}).get("ref") or "")
    for pattern in (_LINK_CLOSES_RE, _LINK_TASK_RE):
        for match in pattern.finditer(body):
            number = int(match.group(1))
            if number not in linked:
                linked.append(number)
    match = _LINK_BRANCH_RE.search(head_ref)
    if match:
        number = int(match.group(1))
        if number not in linked:
            linked.append(number)
    return linked


def collect_prs(client: GitHubClient, now: datetime) -> dict[str, Any]:
    """PR 审核/冒烟/合并情况。

    审核退回率统计 open PR + 近 30 天内合入的 PR（最新审核结论为准）；
    自动合并数按 merged_by 是否为 github-actions[bot] 区分；自动重试
    次数按已取评论里的 auto-fix 轮次标记计数。"""
    open_prs = client.list_pulls(state="open")
    open_rows: list[dict[str, Any]] = []
    auto_fix_rounds: set[tuple[int, int]] = set()
    by_issue: dict[int, dict[str, Any]] = {}
    for pr in open_prs:
        number = int(pr["number"])
        bodies = [str(c.get("body") or "") for c in client.list_issue_comments(number)]
        markers = parse_review_markers(bodies)
        latest_review = markers[-1] if markers else None
        smoke = _latest_smoke(bodies)
        open_rows.append(
            {
                "number": number,
                "title": str(pr.get("title") or ""),
                "draft": bool(pr.get("draft")),
                "head_sha": str((pr.get("head") or {}).get("sha") or ""),
                "review": latest_review,
                "smoke": smoke,
            }
        )
        auto_fix_rounds |= _auto_fix_rounds(bodies)
        for linked in _linked_issues(pr):
            by_issue[linked] = {
                "pr": number,
                "review": latest_review,
                "smoke": smoke,
            }
    merged: list[dict[str, Any]] = []
    for pr in client.list_pulls(state="closed"):
        if not pr.get("merged"):
            continue
        merged_at = parse_iso(pr.get("merged_at"))
        merged_by = str(((pr.get("merged_by") or {}).get("login") or ""))
        merged.append(
            {
                "number": int(pr["number"]),
                "merged_at": merged_at,
                "merged_by": merged_by,
                "auto": merged_by == AUTO_MERGE_LOGIN,
                "body": str(pr.get("body") or ""),
                "head": pr.get("head") or {},
            }
        )
    window_start = now - REVIEW_WINDOW
    recent_merged = [
        m for m in merged if m["merged_at"] is not None and m["merged_at"] >= window_start
    ]
    reviewed: list[Optional[dict[str, Any]]] = []
    for row in open_rows:
        reviewed.append(row["review"])
    for m in recent_merged:
        bodies = [
            str(c.get("body") or "") for c in client.list_issue_comments(m["number"])
        ]
        markers = parse_review_markers(bodies)
        latest_review = markers[-1] if markers else None
        reviewed.append(latest_review)
        auto_fix_rounds |= _auto_fix_rounds(bodies)
        smoke = _latest_smoke(bodies)
        for linked in _linked_issues(m):
            by_issue.setdefault(
                linked,
                {"pr": m["number"], "review": latest_review, "smoke": smoke},
            )
    with_review = [r for r in reviewed if r is not None]
    rejected = [r for r in with_review if r["verdict"] in ("changes-requested", "blocked")]
    auto_merged = [m for m in merged if m["auto"]]
    manual_merged = [m for m in merged if not m["auto"]]
    return {
        "open": open_rows,
        "open_count": len(open_rows),
        "by_issue": by_issue,
        "reviewed_count": len(with_review),
        "rejected_count": len(rejected),
        "merged_total": len(merged),
        "auto_merged": len(auto_merged),
        "manual_merged": len(manual_merged),
        "auto_fix_rounds": len(auto_fix_rounds),
    }


