"""告警收件箱：四类告警统一路由给 Muse 1（Issue #143，审计 P1-8）。

背景：此前 CI 失败只通知 builder、Reaper 失败只开通用告警 Issue、长期无进展 /
阻塞升级没有告警出口，Muse 1 只能靠自己轮询发现问题——正是用户 2026-11-17
批评的被动模式。

做法：仓库里一个固定收件箱 Issue（标题 `INBOX_TITLE`），四类告警都以机器可读
标记往里追加；同一（类别，key）只留一条评论，就地 PATCH 更新，不刷屏：

- ci-failure：key = `pr-<N>`（同一 PR 多次失败只更新一条，附最新运行）
- reaper-failure：key = `latest`（每次失败更新同一条，附最新运行）
- no-progress：key = `issue-<N>`（认领后 4 小时无进展，被 claim-expiry 过期时发）
- blocked-escalation：key = `issue-<N>`（高优先级立即；普通 30 分钟后）

只通知，不改代码、不改标签、不重跑。告警正文只含任务/PR 编号、原因、时间、
已采取的自动动作、下一步建议；token/密码只出现在环境变量里，永不进正文。

正文时间戳一律取**事件时间**（运行时间 / 阻塞打上时间 / 认领时间 + 租约），
不用发送时间，这样同一事件重复投递时正文完全相同，`post_alert` 可直接跳过
（不产生无意义的 PATCH）。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

try:  # 作为包导入（pytest: dispatch.alert_inbox）
    from .github_client import GitHubClient, UrllibGitHubClient
except ImportError:  # 作为脚本直接运行（__package__ 为空）
    from github_client import GitHubClient, UrllibGitHubClient  # type: ignore[no-redef]

#: 固定收件箱 Issue 的标题（精确匹配查找；改标题会另建一个收件箱，勿动）。
INBOX_TITLE = "[告警收件箱] Muse 1"

INBOX_INTRO = (
    "<!-- muse1-alert-inbox -->\n"
    "# 告警收件箱（Muse 1）\n\n"
    "本 Issue 是四类告警的唯一收件箱，各 workflow 以机器可读标记往里追加，"
    "不另开新 Issue、不刷屏：\n\n"
    "- `ci-failure`：CI（quality）失败，key 为 PR 编号\n"
    "- `reaper-failure`：lease-reaper 运行失败，key 为 latest（只留最新一次）\n"
    "- `no-progress`：任务认领后 4 小时无进展被过期，key 为 Issue 编号\n"
    "- `blocked-escalation`：任务阻塞升级，key 为 Issue 编号\n\n"
    "同一（类别，key）只留一条评论，就地更新。只通知，不改代码、不改标签、"
    "不重跑。\n"
)

#: 四类告警（审计 #143 验收标准）。
ALERT_CLASSES = frozenset(
    {"ci-failure", "reaper-failure", "no-progress", "blocked-escalation"}
)

BLOCKED_LABEL = "state:blocked"

#: 普通优先级阻塞满 30 分钟才升级；高优先级（P0/P1）立即升级。
NORMAL_ESCALATION_DELAY = timedelta(minutes=30)
HIGH_PRIORITIES = frozenset({"P0", "P1"})

#: 任务模板里的优先级两种写法都认：`优先级：P0` 与 `## 优先级\n\nP0`。
_PRIORITY_RE = re.compile(r"优先级[：:]*\s*(P[012])")


def alert_marker(alert_class: str, key: str) -> str:
    """机器可读标记：`<!-- muse1-alert: <类别>:<key> -->`。"""
    return f"<!-- muse1-alert: {alert_class}:{key} -->"


@dataclass
class Alert:
    """一条告警。`at` 是事件时间（ISO 8601 UTC），不是发送时间。"""

    alert_class: str
    key: str
    heading: str
    task_ref: str
    reason: str
    at: str
    actions_taken: list[str] = field(default_factory=list)
    next_step: str = ""
    run_url: str = ""

    def __post_init__(self) -> None:
        if self.alert_class not in ALERT_CLASSES:
            raise ValueError(f"未知告警类别：{self.alert_class!r}")


def render_alert_body(alert: Alert) -> str:
    """渲染告警评论正文（首行 marker；只含编号/原因/时间/动作/建议，无敏感值）。"""
    lines = [alert_marker(alert.alert_class, alert.key), "", f"## {alert.heading}", ""]
    lines.append(f"- 任务/PR：{alert.task_ref}")
    lines.append(f"- 原因：{alert.reason}")
    lines.append(f"- 发生时间：{alert.at}（UTC）")
    if alert.run_url:
        lines.append(f"- 运行记录：{alert.run_url}")
    if alert.actions_taken:
        lines.append("- 已采取的自动动作：")
        for action in alert.actions_taken:
            lines.append(f"  - {action}")
    if alert.next_step:
        lines.append(f"- 下一步建议：{alert.next_step}")
    lines.append("")
    lines.append("负责人：Muse 1（本评论由告警收件箱自动追加/更新）")
    return "\n".join(lines)


def find_inbox(client: GitHubClient, repo: str) -> Optional[int]:
    """按标题精确查找 open 的收件箱 Issue；没有返回 None。

    Search API 是模糊匹配，返回结果里再按标题精确过滤，避免标题相近的
    Issue 被误认作收件箱。"""
    for item in client.search_issues(f'repo:{repo} "{INBOX_TITLE}" state:open'):
        if str(item.get("title") or "").strip() == INBOX_TITLE:
            return int(item["number"])
    return None


def ensure_inbox(client: GitHubClient, repo: str) -> int:
    """收件箱不存在时新建（带说明正文），返回 Issue 编号。"""
    number = find_inbox(client, repo)
    if number is not None:
        return number
    created = client.create_issue(INBOX_TITLE, INBOX_INTRO)
    return int(created["number"])


def post_alert(
    client: GitHubClient, repo: str, alert: Alert
) -> dict[str, Any]:
    """往收件箱追加/更新一条告警；同一（类别，key）只留一条评论。

    返回 `{"action": "created" | "updated" | "unchanged", "issue": N,
    "comment_id": id}`。已存在同 marker 评论且正文完全相同时跳过
    （`unchanged`），不产生无意义的 PATCH。
    """
    inbox = ensure_inbox(client, repo)
    body = render_alert_body(alert)
    marker = alert_marker(alert.alert_class, alert.key)
    existing = None
    for comment in client.list_issue_comments(inbox):
        if marker in str(comment.get("body") or ""):
            existing = comment
    if existing is not None:
        if str(existing.get("body") or "") == body:
            return {
                "action": "unchanged",
                "issue": inbox,
                "comment_id": int(existing["id"]),
            }
        updated = client.update_issue_comment(int(existing["id"]), body)
        return {"action": "updated", "issue": inbox, "comment_id": int(updated["id"])}
    created = client.post_issue_comment(inbox, body)
    return {"action": "created", "issue": inbox, "comment_id": int(created["id"])}


# ---------------------------------------------------------------------------
# 四类告警的构造器（纯函数：好测试；CLI 与 lease_reaper.py 都调它们）
# ---------------------------------------------------------------------------


def ci_failure_alert(
    *,
    pr_number: int,
    run_id: str,
    run_url: str,
    jobs: str,
    head_sha: str,
    at: str,
    issue_number: Optional[int] = None,
) -> Alert:
    """CI 失败告警：key 按 PR 聚合，同一 PR 多次失败只更新一条。"""
    task_ref = f"PR #{pr_number}"
    if issue_number is not None:
        task_ref += f"（任务 Issue #{issue_number}）"
    return Alert(
        alert_class="ci-failure",
        key=f"pr-{pr_number}",
        heading=f"CI 失败：PR #{pr_number}",
        task_ref=task_ref,
        reason=f"quality 工作流失败（失败 job：{jobs}；head SHA：`{head_sha}`）",
        at=at,
        actions_taken=["已在 PR 与关联 Issue 下通知 builder（ci-failure-notify 原有通知不变）"],
        next_step="请 Muse 1 确认 builder 是否已认领修复；同一 PR 的后续失败会更新本条评论，不另开。",
        run_url=run_url,
    )


def reaper_failure_alert(*, run_id: str, run_url: str, at: str) -> Alert:
    """Reaper 失败告警：key 固定 latest，只留最新一次失败。"""
    return Alert(
        alert_class="reaper-failure",
        key="latest",
        heading="lease-reaper 运行失败",
        task_ref="lease-reaper（每 5 分钟定时）",
        reason=f"本次运行失败（run {run_id}），过期租约/孤儿范围锁未回收。",
        at=at,
        actions_taken=["未自动重跑；下一轮定时运行会重试"],
        next_step="请 Muse 1 打开运行日志看 [reaper] 开头的报错行；连续失败按 qa-fix-loop.md 人工介入。",
        run_url=run_url,
    )


def no_progress_alert(
    *, issue_number: int, claimed_at: str, ttl_hours: int, at: str
) -> Alert:
    """长期无进展告警：认领后超租约无进展，被 claim-expiry 过期时发。"""
    return Alert(
        alert_class="no-progress",
        key=f"issue-{issue_number}",
        heading=f"任务长期无进展：#{issue_number}",
        task_ref=f"Issue #{issue_number}",
        reason=(
            f"认领于 {claimed_at}，超过 {ttl_hours} 小时无进展"
            "（无新评论，也没有关联的 open PR）。"
        ),
        at=at,
        actions_taken=[
            "已发 CLAIM_EXPIRED 评论",
            "已摘 `state:claimed`、补回 `state:todo`，任务可被重新分派",
        ],
        next_step="请 Muse 1 确认原负责人是否失联；任务已可重派，无需手动改标签。",
    )


def blocked_escalation_alert(
    *, issue_number: int, priority: str, blocked_since: str, at: str
) -> Alert:
    """阻塞升级告警：key 按 Issue 聚合。"""
    return Alert(
        alert_class="blocked-escalation",
        key=f"issue-{issue_number}",
        heading=f"任务阻塞升级：#{issue_number}",
        task_ref=f"Issue #{issue_number}",
        reason=f"任务处于 `state:blocked`（优先级 {priority}），自 {blocked_since} 起未解除。",
        at=at,
        actions_taken=["未自动改派（阻塞升级只通知，由 Muse 1 决定）"],
        next_step="请 Muse 1 查看任务 Issue 上的阻塞说明，决定改派、放宽范围或人工介入。",
    )


# ---------------------------------------------------------------------------
# 阻塞升级：扫描 state:blocked，按优先级与阻塞时长决定是否升级
# ---------------------------------------------------------------------------


def parse_priority(body: str) -> Optional[str]:
    """从任务 Issue 正文的「优先级：P0/P1/P2」取优先级；没有返回 None。"""
    match = _PRIORITY_RE.search(body or "")
    return match.group(1) if match else None


def parse_iso(value: Any) -> Optional[datetime]:
    """解析 `2026-11-18T08:00:00Z`；解析失败返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def blocked_since_from_events(events: list[dict[str, Any]]) -> Optional[datetime]:
    """从 Issue 时间线取 `state:blocked` 最近一次打上的时间；找不到返回 None。"""
    latest: Optional[datetime] = None
    for event in events:
        if str(event.get("event") or "") != "labeled":
            continue
        label = event.get("label") or {}
        if str(label.get("name") or "") != BLOCKED_LABEL:
            continue
        moment = parse_iso(event.get("created_at"))
        if moment and (latest is None or moment > latest):
            latest = moment
    return latest


def should_escalate_blocked(
    priority: Optional[str], blocked_since: Optional[datetime], now: datetime
) -> bool:
    """高优先级（P0/P1）立即升级；普通（P2/未注明）满 30 分钟才升级。

    普通优先级且打标时间未知时不升级（无法证明已满 30 分钟，宁可漏报一次
    等下一轮——下一轮事件仍在，不会永久漏掉；高优先级不依赖时间，直接升级）。
    """
    if priority in HIGH_PRIORITIES:
        return True
    if blocked_since is None:
        return False
    return now - blocked_since >= NORMAL_ESCALATION_DELAY


def escalate_blocked_tasks(
    client: GitHubClient,
    repo: str,
    *,
    now: Optional[datetime] = None,
    log=print,
) -> list[int]:
    """扫描 open 的 `state:blocked` 任务，按规则升级到收件箱。

    返回本轮新发/更新了告警的 Issue 编号。告警发送失败只记日志、不抛错
    （通知失败不应让 Reaper 整轮变红；下一轮同一事件会重试——正文按事件
    时间渲染，重复投递时 `post_alert` 直接跳过，不刷屏）。
    """
    current = now or datetime.now(timezone.utc)
    escalated: list[int] = []
    issues = [
        issue
        for issue in client.list_open_issues(BLOCKED_LABEL)
        if "pull_request" not in issue
    ]
    for issue in issues:
        number = int(issue["number"])
        try:
            priority = parse_priority(str(issue.get("body") or ""))
            blocked_since = blocked_since_from_events(
                client.list_issue_events(number)
            )
            if not should_escalate_blocked(priority, blocked_since, current):
                since_text = (
                    blocked_since.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if blocked_since is not None
                    else "未知"
                )
                log(
                    f"[alert-inbox] #{number} 阻塞未达升级条件"
                    f"（优先级 {priority or '未注明'}，阻塞于 {since_text}），跳过"
                )
                continue
            at = current.strftime("%Y-%m-%dT%H:%M:%SZ")
            since_text = (
                blocked_since.strftime("%Y-%m-%dT%H:%M:%SZ")
                if blocked_since is not None
                else "未知（高优先级直接升级）"
            )
            result = post_alert(
                client,
                repo,
                blocked_escalation_alert(
                    issue_number=number,
                    priority=priority or "未注明",
                    blocked_since=since_text,
                    at=at,
                ),
            )
            if result["action"] in ("created", "updated"):
                escalated.append(number)
            log(f"[alert-inbox] #{number} 阻塞升级已发（{result['action']}）")
        except Exception as exc:  # noqa: BLE001 - 单个 Issue 失败不中断其余
            log(f"[alert-inbox] #{number} 阻塞升级失败：{exc!r}")
    return escalated


# ---------------------------------------------------------------------------
# CLI：供 workflow 的 bash 步骤调用（ci-failure-notify / lease-reaper）
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_config(values: Optional[dict] = None) -> tuple[str, str]:
    env = os.environ if values is None else values
    repo = str(env.get("REPO") or env.get("GITHUB_REPOSITORY") or "").strip()
    token = str(env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or "").strip()
    if not repo or "/" not in repo:
        raise ValueError("缺少 REPO / GITHUB_REPOSITORY 环境变量（形如 owner/name）")
    if not token:
        raise ValueError("缺少 GH_TOKEN / GITHUB_TOKEN 环境变量")
    return repo, token


def main(
    argv: Optional[list[str]] = None,
    *,
    env: Optional[dict] = None,
    client: Optional[GitHubClient] = None,
) -> int:
    parser = argparse.ArgumentParser(description="告警收件箱 CLI（#143）")
    sub = parser.add_subparsers(dest="command", required=True)

    ci = sub.add_parser("ci-failure", help="CI 失败告警（按 PR 聚合）")
    ci.add_argument("--pr", type=int, required=True)
    ci.add_argument("--run-id", required=True)
    ci.add_argument("--run-url", required=True)
    ci.add_argument("--jobs", default="（未能列出，以运行日志为准）")
    ci.add_argument("--head-sha", required=True)
    ci.add_argument("--issue", type=int, default=None)
    ci.add_argument("--at", default=None, help="事件时间（默认现在）")

    rp = sub.add_parser("reaper-failure", help="Reaper 失败告警（只留最新一次）")
    rp.add_argument("--run-id", required=True)
    rp.add_argument("--run-url", required=True)
    rp.add_argument("--at", default=None, help="事件时间（默认现在）")

    sub.add_parser("blocked-scan", help="扫描 state:blocked 并按规则升级")

    args = parser.parse_args(argv)
    try:
        repo, token = _env_config(env)
    except ValueError as exc:
        print(f"[alert-inbox] 配置错误：{exc}", file=sys.stderr)
        return 2
    client = client or UrllibGitHubClient(repo=repo, token=token)
    at = args.at or _utcnow_iso()
    try:
        if args.command == "ci-failure":
            result = post_alert(
                client,
                repo,
                ci_failure_alert(
                    pr_number=args.pr,
                    run_id=args.run_id,
                    run_url=args.run_url,
                    jobs=args.jobs,
                    head_sha=args.head_sha,
                    at=at,
                    issue_number=args.issue,
                ),
            )
            print(f"[alert-inbox] CI 失败告警已发（PR #{args.pr}，{result['action']}）")
        elif args.command == "reaper-failure":
            result = post_alert(
                client,
                repo,
                reaper_failure_alert(run_id=args.run_id, run_url=args.run_url, at=at),
            )
            print(f"[alert-inbox] Reaper 失败告警已发（{result['action']}）")
        elif args.command == "blocked-scan":
            escalated = escalate_blocked_tasks(client, repo)
            print(f"[alert-inbox] 阻塞升级扫描完成：升级 {len(escalated)} 个任务")
        else:  # pragma: no cover - argparse required=True 已拦住
            parser.error(f"未知子命令：{args.command}")
    except Exception as exc:  # noqa: BLE001 - CLI 失败必须显式：非零退出
        print(f"[alert-inbox] 告警发送失败：{exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
