"""认领 / 分派超时过期（lease-reaper 每 5 分钟巡检中的一步）。

协议规定（`docs/workspace/agent-protocol.md` 第二、三节）：

- 认领后 **4 小时无进展**（无 push、无评论）即租约过期，应摘 `state:claimed`、
  补回 `state:todo`，让任务可被重新分派；
- TASK_ASSIGNED **24 小时无人认领**则追问 / 改派。

本模块负责第一步的自动化：扫 `state:claimed` 的 open Issue，以认领评论时间为
起点，认领之后的评论和关联 open PR 都算“进展”；确认超时后：

1. 发一条 `CLAIM_EXPIRED` 评论（同一次认领只发一次，评论带
   `<!-- claim-expiry: issue-N claimed-at-<ts> -->` 幂等标记）；
2. 标签收敛：摘 `state:claimed`、补 `state:todo`。

第二步（分派超时）在 `auto_dispatch.is_ready` 里处理：超过 24h 无人认领的
TASK_ASSIGNED 不再阻塞重新分派。分开的原因：分派超时不需要改标签，读评论时
顺手判断即可，不必每 5 分钟再扫一遍 `state:todo`（GITHUB_TOKEN 每小时 1000
次请求的预算要省着用）。

失败显式：单个 Issue 的处理结果先记录，全部跑完后统一抛错让 workflow 变红；
绝不把“标签没写成”静默成“已过期”。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from dispatch.github_client import GitHubClient
from dispatch.protocol import (
    ASSIGNED_MARKER,
    CLAIM_MARKERS,
    parse_marker_time,
    resolve_issue_number,
    utc_now_iso,
)

STATE_CLAIMED = "state:claimed"
STATE_TODO = "state:todo"

#: 认领后无进展的租约时长（协议规定 4 小时）。
DEFAULT_CLAIM_TTL = timedelta(hours=4)

#: 环境变量：覆盖认领租约（小时，正整数）。
ENV_CLAIM_TTL_HOURS = "AGENT_CLAIM_TTL_HOURS"

#: 机器人评论者（GitHub Actions）不算“进展”。
_BOT_LOGINS = {"github-actions", "github-actions[bot]", "dependabot[bot]"}


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _compact(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def claim_expiry_marker(number: int, claimed_at: datetime) -> str:
    """同一次认领的幂等标记：认领时间变了才会发下一条过期评论。"""
    return f"<!-- claim-expiry: issue-{int(number)} claimed-at-{_compact(claimed_at)} -->"


def _comment_bodies(comments: list[dict[str, Any]]) -> list[str]:
    return [str(comment.get("body") or "") for comment in comments]


def latest_claim_time(comments: list[dict[str, Any]]) -> datetime | None:
    """最近一次认领评论的时间；读不到（老格式）返回 None。"""
    moments = [
        parse_marker_time(body)
        for body in _comment_bodies(comments)
        if any(marker in body for marker in CLAIM_MARKERS)
    ]
    return max((moment for moment in moments if moment), default=None)


def progress_after(comments: list[dict[str, Any]], since: datetime) -> datetime | None:
    """认领之后由“人/Agent”发的普通评论时间（最后一条）；没有返回 None。

    关联 open PR 的“push 也算进展”在 `expire_stale_claims` 里用 PR 列表补判。
    """
    latest: datetime | None = None
    for comment in comments:
        body = str(comment.get("body") or "")
        if any(marker in body for marker in CLAIM_MARKERS) or ASSIGNED_MARKER in body:
            continue
        login = str((comment.get("user") or {}).get("login") or "").lower()
        if login in _BOT_LOGINS:
            continue
        moment = _parse_iso(comment.get("created_at"))
        if moment and moment > since and (latest is None or moment > latest):
            latest = moment
    return latest


def _open_pr_issues(client: GitHubClient) -> set[int]:
    """当前 open PR 关联的 Issue 编号（有 open PR = 有 push，不算“无进展”）。"""
    numbers: set[int] = set()
    for pull in client.list_open_pulls():
        issue = resolve_issue_number(
            pull.get("body") or "", (pull.get("head") or {}).get("ref") or ""
        )
        if issue:
            numbers.add(issue)
    return numbers


def _expired_comment(
    number: int,
    claimed_at: datetime,
    now: datetime,
    marker: str,
    claim_ttl: timedelta,
) -> str:
    hours = int(claim_ttl.total_seconds() // 3600)
    return (
        f"<!-- agent-msg: CLAIM_EXPIRED, task: #{number}, from: lease-reaper, "
        f"at: {utc_now_iso()} -->\n"
        f"{marker}\n"
        f"## 认领已过期\n\n"
        f"- 认领时间：`{claimed_at:%Y-%m-%dT%H:%M:%SZ}`\n"
        f"- 判定：认领后超过 {hours} 小时无进展（无新评论，也没有关联的 open PR）。\n"
        f"- 处理：已摘除 `state:claimed`、补回 `state:todo`，任务可被重新分派。\n\n"
        f"负责人：Muse 1\n"
    )


def expire_stale_claims(
    client: GitHubClient,
    *,
    now: datetime | None = None,
    claim_ttl: timedelta = DEFAULT_CLAIM_TTL,
    dry_run: bool = False,
    log=print,
) -> dict[str, Any]:
    """扫描 `state:claimed` Issue，把超时未动的收敛回 `state:todo`。

    返回 `{"checked": N, "expired": [编号...], "expired_claims":
    [{"number": N, "claimed_at": "ISO"}, ...], "dry_run": bool}`；
    单个 Issue 失败先记录，全部处理完后统一抛 `RuntimeError`（显式失败）。
    """
    current = now or datetime.now(timezone.utc)
    issues = [
        issue
        for issue in client.list_open_issues(STATE_CLAIMED)
        if "pull_request" not in issue
    ]
    expired: list[int] = []
    expired_claims: list[dict[str, Any]] = []
    errors: list[str] = []
    open_pr_issues: set[int] | None = None

    for issue in issues:
        number = int(issue["number"])
        try:
            comments = client.list_issue_comments(number)
            bodies = _comment_bodies(comments)
            claimed_at = latest_claim_time(comments)
            if claimed_at is None:
                log(
                    f"[claim-expiry] #{number} 有 {STATE_CLAIMED} 但读不到认领评论"
                    "（格式不符），跳过，留待人工处理"
                )
                continue
            progress = progress_after(comments, claimed_at)
            last_activity = max(
                [claimed_at] + ([progress] if progress is not None else [])
            )
            if current - last_activity < claim_ttl:
                continue
            if open_pr_issues is None:
                open_pr_issues = _open_pr_issues(client)
            if number in open_pr_issues:
                log(f"[claim-expiry] #{number} 有 open PR 关联，视为仍在进展，跳过")
                continue

            marker = claim_expiry_marker(number, claimed_at)
            if not any(marker in body for body in bodies):
                if dry_run:
                    log(f"[claim-expiry] #{number} 认领超时（dry-run，不发评论、不改标签）")
                else:
                    client.post_issue_comment(
                        number,
                        _expired_comment(number, claimed_at, current, marker, claim_ttl),
                    )
            if not dry_run:
                client.remove_issue_label(number, STATE_CLAIMED)
                client.add_issue_labels(number, [STATE_TODO])
            expired.append(number)
            expired_claims.append(
                {
                    "number": number,
                    "claimed_at": claimed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
            log(
                f"[claim-expiry] #{number} 认领于 {claimed_at:%Y-%m-%dT%H:%M:%SZ} "
                f"已超过 {int(claim_ttl.total_seconds() // 3600)}h 无进展："
                f"{STATE_CLAIMED} → {STATE_TODO}"
            )
        except Exception as exc:  # noqa: BLE001 - 记录后统一抛出，不中断其余 Issue
            errors.append(f"#{number}: {exc!r}")
            log(f"[claim-expiry] #{number} 处理失败：{exc!r}")

    log(f"[claim-expiry] 巡检 {len(issues)} 个 Issue，过期 {len(expired)} 个")
    if errors:
        raise RuntimeError(
            f"[claim-expiry] {len(errors)} 个 Issue 处理失败（本轮显式失败）："
            + "；".join(errors)
        )
    return {
        "checked": len(issues),
        "expired": expired,
        "expired_claims": expired_claims,
        "dry_run": dry_run,
    }
