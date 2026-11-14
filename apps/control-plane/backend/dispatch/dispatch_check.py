"""Issue 必填字段 / 依赖阻塞检查（原 `dispatch-check.yml` 内嵌脚本）。

只做提醒与标签，不合并、不关闭 Issue、不替 builder 认领、不发分派评论
（分派在 `auto_dispatch.py`）。行为口径见
`docs/workspace/dispatch-rules.md` 第五节。

修复：

1. **全量分页**：评论、`state:todo/state:blocked` Issue 列表、依赖方搜索
   结果全部走 `client.paginate`。第 2 页的 marker 评论能读到 → 不再重复
   提醒；第 2 页之后的待检查 Issue 也能覆盖。
2. **幂等写入**：标签操作改成幂等写法（POST 加标签天然去重；DELETE 不存在
   的标签把 404/422 当成功），不再“先查标签再写”；提醒评论用
   `comment_ledger.ensure_comment_once`，重复执行 / 超时重试不产生第二条。
3. **失败显式**：单 Issue 检查失败会记录并在本轮结束时抛错让 workflow 变红，
   不再静默跳过；分页失败直接抛出，绝不用不完整数据判断“没有 marker”。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

# 直接以文件方式运行时把 backend 目录放进 sys.path（见 auto_dispatch.py 同款注释）。
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from dispatch.comment_ledger import ensure_comment_once  # noqa: E402
from dispatch.github_client import GitHubAPIError, GitHubClient, client_from_env  # noqa: E402
from dispatch.protocol import parse_dependencies, strip_html_comments  # noqa: E402

# 必填字段：与 .github/ISSUE_TEMPLATE/agent-task.md 的小节一一对应。
REQUIRED_SECTIONS = [
    "任务说明",
    "负责人",
    "优先级",
    "文件修改范围",
    "依赖任务",
    "验收标准",
    "所属分支",
]

MISSING_MARKER = "<!-- dispatch-check: missing-fields -->"
BLOCKED_MARKER = "<!-- dispatch-check: blocked -->"
UNBLOCKED_MARKER = "<!-- dispatch-check: unblocked -->"

# 依赖方搜索的 REST Search API（gh api search/issues 在 runner 里报 404，
# 2026-11-14 实测改用 REST；见 github-automation.md 第五节备忘）。
SEARCH_MAX_RESULTS = 1000


def _labels_of(issue: dict[str, Any]) -> list[str]:
    return [lb.get("name", "") for lb in issue.get("labels", []) or []]


def _has_marker(client: GitHubClient, number: int, marker: str) -> bool:
    """分页扫描全部评论确认 marker 是否存在（失败显式抛出）。"""
    return any(
        marker in (comment.get("body") or "")
        for comment in client.list_issue_comments(number)
    )


def ensure_marker_comment(
    client: GitHubClient,
    number: int,
    marker: str,
    body: str,
    *,
    wait_timeout: float = 0.0,
) -> str:
    """同一 marker 只发一次；超时后先对账再补发。"""
    return ensure_comment_once(
        client, number, lambda text: marker in text, body, wait_timeout=wait_timeout
    )


def missing_sections(body: str | None) -> list[str]:
    """返回缺失的必填小节（小节存在但内容为空也算缺失）。"""
    plain = strip_html_comments(body)
    missing: list[str] = []
    for name in REQUIRED_SECTIONS:
        match = re.search(rf"^## {re.escape(name)}\s*$", plain, flags=re.M)
        if not match:
            missing.append(name)
            continue
        rest = plain[match.end():]
        nxt = re.search(r"^## ", rest, flags=re.M)
        content = (rest[: nxt.start()] if nxt else rest).strip()
        if not content:
            missing.append(name)
    # 负责人一栏必须明确写成 agent:*（模板注释不算数）。
    if "负责人" not in missing and not re.search(r"agent:(muse-1|muse-2|muse-3|grok)", plain):
        missing.append("负责人（未写明 agent:muse-2/3/grok）")
    return missing


def is_agent_task(issue: dict[str, Any]) -> bool:
    body = issue.get("body") or ""
    labels = _labels_of(issue)
    return "## 任务说明" in body or any(
        lb.startswith(("state:", "agent:", "phase:")) for lb in labels
    )


def check_issue(client: GitHubClient, number: int) -> dict[str, Any]:
    """检查单个 Issue：缺字段提醒 + 依赖阻塞 / 解封。返回处理摘要。"""
    issue = client.get_issue(number)
    if "pull_request" in issue:
        return {"number": number, "status": "skip", "reason": "是 PR"}
    if issue.get("state") != "open":
        return {"number": number, "status": "skip", "reason": "已关闭"}
    if not is_agent_task(issue):
        return {"number": number, "status": "skip", "reason": "不是 agent-task Issue"}

    body = issue.get("body") or ""
    labels = _labels_of(issue)
    result: dict[str, Any] = {"number": number, "status": "checked", "actions": []}

    # 1) 缺字段提醒：只对 state:todo（或无 state 标签）的新任务检查，避免刷屏。
    if "state:todo" in labels or not any(lb.startswith("state:") for lb in labels):
        missing = missing_sections(body)
        if missing:
            items = "\n".join(f"- {m}" for m in missing)
            outcome = ensure_marker_comment(
                client,
                number,
                MISSING_MARKER,
                f"{MISSING_MARKER}\n"
                f"## 缺必填字段提醒\n\n"
                f"本 Issue 缺以下必填字段（见 `docs/workspace/dispatch-rules.md` 第一节），"
                f"请补齐后再认领：\n\n{items}\n\n"
                f"模板：`.github/ISSUE_TEMPLATE/agent-task.md`。",
            )
            result["actions"].append(f"缺字段提醒:{outcome}")

    # 2) 依赖阻塞 / 3) 依赖解除。
    deps = parse_dependencies(body)
    if not deps:
        return result
    open_deps = [
        dep for dep in deps if client.get_issue(dep).get("state") != "closed"
    ]
    if open_deps:
        # 幂等写：重复加同一个标签不会产生重复标签。
        client.add_issue_labels(number, ["state:blocked"])
        deps_text = "、".join(f"#{dep}" for dep in open_deps)
        outcome = ensure_marker_comment(
            client,
            number,
            BLOCKED_MARKER,
            f"{BLOCKED_MARKER}\n"
            f"依赖未满足：{deps_text} 尚未完成（未关闭），"
            f"本任务暂不能认领，已打 `state:blocked` 标签。\n"
            f"依赖全部完成后会自动摘掉标签。",
        )
        result["actions"].append(f"阻塞:{outcome}")
        return result

    had_blocked = "state:blocked" in labels
    # 幂等写：标签本来就不存在时 DELETE 返回 404，函数内部当成功处理。
    client.remove_issue_label(number, "state:blocked")
    if had_blocked:
        # 只在“原本确实被阻塞过”时发解封评论，避免给从未阻塞的任务刷评论。
        outcome = ensure_marker_comment(
            client,
            number,
            UNBLOCKED_MARKER,
            f"{UNBLOCKED_MARKER}\n"
            f"依赖已全部完成（{', '.join(f'#{dep}' for dep in deps)}），"
            f"已摘掉 `state:blocked` 标签，可以认领了。",
        )
        result["actions"].append(f"解封:{outcome}")
    return result


def find_dependents(client: GitHubClient, repo: str, closed_number: int) -> list[int]:
    """找出正文写了 `depends on #N` 的 open Issue（依赖方关闭时重新检查）。"""
    query = f'repo:{repo} "depends on #{closed_number}" type:issue state:open'
    numbers = [
        int(item["number"])
        for item in client.search_issues(query)[:SEARCH_MAX_RESULTS]
        if item.get("number") is not None
    ]
    return sorted(set(numbers))


def run_dispatch_check(
    client: GitHubClient,
    repo: str,
    *,
    issue_number: int | None = None,
    action: str = "",
) -> dict[str, Any]:
    """入口逻辑：事件单个 Issue 或定时全量巡检。任何检查失败最终显式抛错。"""
    numbers: set[int] = set()
    if issue_number:
        numbers.add(int(issue_number))
        if action == "closed":
            numbers.update(find_dependents(client, repo, int(issue_number)))
    else:
        # 定时巡检：全量分页读取待认领 / 被阻塞的 open Issue。
        # 注意 REST `labels=a,b` 是“同时带 a 和 b”，拿它查“todo 或 blocked”会
        # 永远查不到；这里按标签分别查询再取并集（原内嵌脚本的隐藏漏检点）。
        for label in ("state:todo", "state:blocked"):
            for item in client.list_open_issues(label):
                if "pull_request" not in item and item.get("number") is not None:
                    numbers.add(int(item["number"]))

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for number in sorted(numbers):
        try:
            results.append(check_issue(client, number))
        except GitHubAPIError as exc:
            # 单个 Issue 失败不吞掉：记录后继续，本轮结束统一抛错让 workflow 变红。
            print(f"[dispatch-check] #{number} 检查失败：{exc}", flush=True)
            errors.append(f"#{number}: {exc}")
    if errors:
        raise GitHubAPIError(
            f"dispatch-check 有 {len(errors)} 个 Issue 检查失败（数据不完整，本轮失败）："
            + "；".join(errors)
        )
    return {"status": "ok", "checked": results}


def run_from_env(env: dict[str, str], client: GitHubClient | None = None) -> dict[str, Any]:
    repo = env.get("REPO") or env.get("GITHUB_REPOSITORY") or ""
    if not repo:
        raise GitHubAPIError("缺少 REPO 环境变量")
    raw_number = env.get("ISSUE_NUMBER") or ""
    issue_number = int(raw_number) if raw_number.isdigit() else None
    print(
        f"[dispatch-check] event={env.get('EVENT_NAME', '')} action={env.get('EVENT_ACTION', '')} "
        f"issue={issue_number}",
        flush=True,
    )
    client = client or client_from_env(env)
    return run_dispatch_check(
        client, repo, issue_number=issue_number, action=env.get("EVENT_ACTION") or ""
    )


def main() -> int:
    result = run_from_env(dict(os.environ))
    print(f"[dispatch-check] 结果：{result}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
