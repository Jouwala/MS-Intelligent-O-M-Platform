"""统一多 Muse 状态视图（Phase 3 P3-2，Issue #142）。

背景：审计 Phase 3 发现至今没有统一的多 Muse 状态视图。Muse 1 要知道在线
几人、谁在跑什么、等了多久、哪里卡住，只能逐个翻 Issue/PR/评论——正是用户
2026-11-17 批评的被动模式。

做法：仓库里一个固定状态 Issue（标题 `STATUS_TITLE`），由本 CLI 机器生成
正文并就地 PATCH 更新，不另开 Issue、不追加评论：

- 载体选固定 Issue 而不用 `docs/workspace/agent-status.md` 文件：往 main
  直接提交文件违反“完工走 PR、不直接推 main”；固定 Issue 就地 PATCH，
  与 #143 告警收件箱（`alert_inbox.py`）同一模式，单页常驻、无 PR 噪音。
- 指标复用现有读接口，不新增中心服务：Worker 七态读心跳 refs + 租约
  refs（`scheduler.ref_heartbeat` / `scheduler.ref_api`，与 `list_workers`
  同源；评论心跳通道是 no-op——全仓库 `registry_issue=None`，见 #141）；
  任务状态读 Issue 标签 + 时间线事件；审核/冒烟读 PR 评论里的机器可读
  标记（`review_gate.parse_review_markers` + smoke-result 标记）。
- 事件驱动更新（`status-view.yml`）：PR 打开/合入、Issue 认领/关闭/贴
  QA 失败标记、认领与审核评论到达时刷新；**不用定时轮询 PR 状态**
  （用户禁令：不为 PR 工作排定时检查）。GITHUB_TOKEN 合并不会触发
  pull_request closed 事件，auto-merge 合入后显式 dispatch 本工作流
  （与 pr-merged-writeback 同一模式）。
- 正文完全相同时跳过 PATCH（`unchanged`），不产生无意义的写操作；
  正文头部注明生成方式与时间，手改内容下次更新即被覆盖。

正文只含任务/PR 编号、状态、耗时与比率；token/密码只出现在环境变量里，
永不进正文。只读 + 更新本 Issue，不改代码、不改标签、不重跑、不合并。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

try:  # 作为包导入（pytest: dispatch.status_view）
    from .github_client import GitHubClient, UrllibGitHubClient
    from .status_collect import (
        TASK_ROW_LIMIT,
        collect_prs,
        collect_tasks,
        collect_workers,
    )
except ImportError:  # 作为脚本直接运行（__package__ 为空）
    from github_client import GitHubClient, UrllibGitHubClient  # type: ignore[no-redef]
    from status_collect import (  # type: ignore[no-redef]
        TASK_ROW_LIMIT,
        collect_prs,
        collect_tasks,
        collect_workers,
    )

#: 固定状态 Issue 的标题与机器可读标记（与告警收件箱同一模式）。
STATUS_TITLE = "[状态视图] Muse 1"
STATUS_MARKER = "<!-- muse1-status-view -->"
STATUS_INTRO = (
    "本 Issue 是统一多 Muse 状态视图（Phase 3 P3-2，Issue #142）。"
    "正文由 `status-view.yml` 机器生成并就地更新；手改内容下次更新即被覆盖。"
)

def _fmt_duration(seconds: Optional[float]) -> str:
    """秒 → `2天3小时` / `3小时12分` / `45分` / `—`（无数据）。"""
    if seconds is None or seconds < 0:
        return "—"
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, _ = divmod(rest, 60)
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分"


def _fmt_rate(numerator: int, denominator: int) -> str:
    """比率；分母为 0 时返回 `—`（不是 0%，避免把“没数据”写成“没失败”）。"""
    if denominator <= 0:
        return "—"
    return f"{numerator}/{denominator}（{numerator / denominator:.0%}）"


# ---------------------------------------------------------------------------
# 固定状态 Issue
# ---------------------------------------------------------------------------


def find_status_issue(client: GitHubClient, repo: str) -> Optional[int]:
    """按标题精确查找 open 的状态 Issue；没有返回 None。

    Search API 是模糊匹配，返回结果里再按标题精确过滤，避免标题相近的
    Issue 被误认作状态视图。"""
    for item in client.search_issues(f'repo:{repo} "{STATUS_TITLE}" state:open'):
        if str(item.get("title") or "").strip() == STATUS_TITLE:
            return int(item["number"])
    return None


def ensure_status_issue(client: GitHubClient, repo: str) -> int:
    """状态 Issue 不存在时新建（带说明正文），返回 Issue 编号。"""
    number = find_status_issue(client, repo)
    if number is not None:
        return number
    created = client.create_issue(STATUS_TITLE, STATUS_INTRO)
    return int(created["number"])


def update_status_issue(
    client: GitHubClient, repo: str, body: str
) -> dict[str, Any]:
    """就地更新状态 Issue 正文；正文完全相同时跳过。

    返回 `{"action": "created" | "updated" | "unchanged", "issue": N}`。"""
    number = ensure_status_issue(client, repo)
    current = client.get_issue(number)
    if str(current.get("body") or "") == body:
        return {"action": "unchanged", "issue": number}
    client.update_issue(number, body)
    return {"action": "updated", "issue": number}


# ---------------------------------------------------------------------------
# 指标汇总与渲染
# ---------------------------------------------------------------------------


def render_status(
    workers: dict[str, Any],
    tasks: dict[str, Any],
    prs: dict[str, Any],
    now: datetime,
) -> str:
    """渲染状态 Issue 全文（首行 marker；手改内容下次更新即被覆盖）。"""
    counts = workers["counts"]
    online = counts.get("online", 0) + counts.get("idle", 0) + counts.get("working", 0)
    lines = [
        STATUS_MARKER,
        "",
        "# 多 Muse 状态视图（Muse 1）",
        "",
        f"生成时间：{now.strftime('%Y-%m-%dT%H:%M:%SZ')}（UTC，机器生成；手改内容下次更新即被覆盖）",
        "",
        "## 在线 Muse",
        "",
        f"- 在线：{online} 人（online {counts.get('online', 0)} / idle {counts.get('idle', 0)} / working {counts.get('working', 0)}）",
        f"- 等待：{counts.get('waiting', 0)} 人 / 阻塞：{counts.get('blocked', 0)} 人 / 失败：{counts.get('failed', 0)} 人",
        f"- 离线：{counts.get('offline', 0)} 人 / 未知：{counts.get('unknown', 0)} 人",
    ]
    if workers["workers"]:
        lines.append("")
        lines.append("| Muse | 实例 | 状态 | 自报 | 最后心跳 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for worker in workers["workers"]:
            last = worker["last_beat"].strftime("%m-%d %H:%M") if worker["last_beat"] else "—"
            lines.append(
                f"| {worker['agent']} | {worker['instance']} | {worker['state']} | "
                f"{worker['reported']} | {last} |"
            )
    lines += [
        "",
        "## 任务总览",
        "",
        f"- 任务总数：{tasks['total']}（执行中 {tasks['running']} / 无人认领 {tasks['unclaimed']} / 阻塞 {tasks['blocked']} / 当前 QA 失败 {tasks['qa_failed_now']}）",
        f"- 等待中位：{_fmt_duration(tasks['median_wait'])}（创建→认领，已认领任务）",
        f"- 执行中位：{_fmt_duration(tasks['median_exec'])}（认领→关闭，已关闭任务）",
    ]
    waiter = tasks["longest_waiter"]
    if waiter is not None:
        lines.append(
            f"- 等最久的未认领任务：#{waiter['number']}（{_fmt_duration(waiter['wait_seconds'])}）"
        )
    longest = tasks["longest_running"]
    if longest is not None:
        lines.append(
            f"- 跑最久的执行中任务：#{longest['number']}（{_fmt_duration(longest['exec_seconds'])}）"
        )
    lines += [
        "",
        "## 质量",
        "",
        f"- QA 失败率：{_fmt_rate(tasks['qa_failed_ever'], tasks['claimed_ever'])}（曾贴 state:qa-failed / 曾认领）",
        f"- 审核退回率：{_fmt_rate(prs['rejected_count'], prs['reviewed_count'])}（近 30 天有审核标记的 PR，最新结论为 changes-requested/blocked）",
        f"- 自动重试次数：{prs['auto_fix_rounds']}（auto-fix 轮次标记，不同任务×轮次）",
        f"- 自动合并数：{prs['auto_merged']} / 人工合入数：{prs['manual_merged']}（全部已合入 PR：{prs['merged_total']}）",
    ]
    if tasks["median_recovery"] is not None:
        lines.append(f"- QA 失败恢复中位：{_fmt_duration(tasks['median_recovery'])}（首次贴 state:qa-failed→关闭/现在）")
    lines += ["", "## 进行中的 PR", ""]
    if prs["open"]:
        lines.append("| PR | 审核 | 冒烟 |")
        lines.append("| --- | --- | --- |")
        for row in prs["open"]:
            review = row["review"]
            review_text = (
                f"{review['verdict']}（{review['reviewer']}）" if review else "未审"
            )
            smoke = row["smoke"]
            smoke_text = smoke["result"] if smoke else "未跑"
            draft = "（草稿）" if row["draft"] else ""
            lines.append(
                f"| #{row['number']}{draft} | {review_text} | {smoke_text} |"
            )
    else:
        lines.append("无进行中的 PR。")
    rows = sorted(tasks["tasks"], key=lambda t: t["number"], reverse=True)[:TASK_ROW_LIMIT]
    lines += ["", "## 每任务行", ""]
    if rows:
        lines.append("| 任务 | 负责人 | 状态 | 审核 | QA | 故障 | 等待 | 执行 | 恢复 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for task in rows:
            linked = prs.get("by_issue", {}).get(task["number"]) or {}
            review = linked.get("review") or {}
            review_text = (
                f"{review.get('verdict')}（{review.get('reviewer')}）" if review else "—"
            )
            smoke = linked.get("smoke") or {}
            if smoke:
                qa_text = "通过" if smoke.get("result") == "passed" else "失败"
            elif task["qa_failed_at"] is not None:
                qa_text = "失败"
            elif task["state"] == "state:merged":
                qa_text = "通过"
            else:
                qa_text = "—"
            if task["state"] == "state:blocked":
                fault = "阻塞"
            elif smoke and smoke.get("result") == "failed":
                fault = "冒烟失败"
            elif task["qa_failed_at"] is not None:
                fault = "QA 失败"
            else:
                fault = "—"
            lines.append(
                f"| #{task['number']} | {task['agent']} | {task['state']} | {review_text} | "
                f"{qa_text} | {fault} | {_fmt_duration(task['wait_seconds'])} | "
                f"{_fmt_duration(task['exec_seconds'])} | "
                f"{_fmt_duration(task['recovery_seconds'])} |"
            )
        if len(tasks["tasks"]) > TASK_ROW_LIMIT:
            lines.append(f"（仅展示最新 {TASK_ROW_LIMIT} 条，共 {len(tasks['tasks'])} 条）")
    else:
        lines.append("无任务。")
    lines += [
        "",
        "## 口径说明",
        "",
        "- Worker 状态：心跳 refs（90 秒 TTL）+ 租约 refs（10 分钟 TTL），与 `list_workers` 同源；无心跳 beat 的 Worker 不列出。",
        "- QA 失败率：曾贴 state:qa-failed 的任务数 / 曾认领过的任务数；分母为 0 时写 `—`，不是 0%。",
        "- 审核退回率：open PR + 近 30 天内合入的 PR，最新审核结论为准；无审核标记的 PR 不计入分母。",
        "- 自动重试次数：已取评论中 `auto-fix` 轮次标记的不同（任务，轮次）对数。",
        "- 自动合并数：merged_by 为 github-actions[bot] 的合入 PR 数；人工合入数：其余合入 PR 数。",
        "- 恢复用时：任务首次贴 state:qa-failed 到关闭（未关闭算到现在）的时长。",
        "",
        "负责人：Muse 1（本 Issue 由 `status-view.yml` 自动更新）",
    ]
    return "\n".join(lines)


def update_status(
    client: GitHubClient,
    repo: str,
    *,
    now: Optional[datetime] = None,
    log=print,
) -> dict[str, Any]:
    """收集 → 渲染 → 就地更新状态 Issue；正文相同则跳过。"""
    current = now or datetime.now(timezone.utc)
    workers = collect_workers(client, current)
    tasks = collect_tasks(client, current)
    prs = collect_prs(client, current)
    body = render_status(workers, tasks, prs, current)
    result = update_status_issue(client, repo, body)
    log(
        f"[status-view] 状态视图已更新（Issue #{result['issue']}，{result['action']}；"
        f"任务 {tasks['total']} 个，open PR {prs['open_count']} 个，"
        f"在线 {sum(workers['counts'].get(s, 0) for s in ('online', 'idle', 'working'))} 人）"
    )
    return result


# ---------------------------------------------------------------------------
# CLI：供 status-view.yml 调用
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
    parser = argparse.ArgumentParser(description="统一多 Muse 状态视图（#142）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("update", help="收集指标并就地更新状态 Issue")
    args = parser.parse_args(argv)
    try:
        repo, token = _env_config(env)
    except ValueError as exc:
        print(f"[status-view] 配置错误：{exc}", file=sys.stderr)
        return 2
    client = client or UrllibGitHubClient(repo=repo, token=token)
    try:
        if args.command == "update":
            update_status(client, repo)
        else:  # pragma: no cover - argparse required=True 已拦住
            parser.error(f"未知子命令：{args.command}")
    except Exception as exc:  # noqa: BLE001 - CLI 失败必须显式：非零退出
        print(f"[status-view] 状态更新失败：{exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
