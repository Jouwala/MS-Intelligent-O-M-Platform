"""Agent 协议口径的纯函数：Issue 正文解析、评论机器可读头、标签常量。

协议定义见 `docs/workspace/agent-protocol.md`（12 类消息的机器可读头）与
`docs/workspace/dispatch-rules.md`（拆分与分派规则）。本模块**零 I/O**，
只做字符串处理，因此可以脱离 GitHub 单测。

集中在这里的另一个原因：三处自动化（auto-dispatch / agent-orchestrator /
dispatch-check）都要解析 `depends on #N`、`Closes #N` 与消息头，过去各抄
一份，口径一改就分叉。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# 可接业务任务的 builder（agent-registry.md 第三节）。
BUILDERS = ("agent:muse-2", "agent:muse-3")

# 认领标记：协议第二节的 agent-claim 与 TASK_ACCEPTED 两种写法都算已认领。
CLAIM_MARKERS = ("agent-claim:", "agent-msg: TASK_ACCEPTED")

# 分派标记：auto-dispatch 发出的分派评论头。
ASSIGNED_MARKER = "agent-msg: TASK_ASSIGNED"

# 分派登记评论里必须出现的幂等键字段（TASK-07）。
DISPATCH_ID_FIELD = "dispatch_id:"


def strip_html_comments(text: str | None) -> str:
    """去掉 HTML 注释，避免模板示例（含 `depends on #N` 样例）被误解析。"""
    return re.sub(r"<!--.*?-->", "", text or "", flags=re.S)


def parse_dependencies(body: str | None) -> list[int]:
    """解析正文里的 `depends on #N`，去重并按编号排序。"""
    plain = strip_html_comments(body)
    return sorted({int(n) for n in re.findall(r"depends on #(\d+)", plain, flags=re.I)})


def resolve_issue_number(body: str | None, head_ref: str | None) -> int | None:
    """从 PR 正文 / 分支名解析关联 Issue：Closes #N → task: #N → agent/task-N。"""
    body = body or ""
    head_ref = head_ref or ""
    m = re.search(r"(closes|fixes|resolves)\s+#(\d+)", body, flags=re.I)
    if m:
        return int(m.group(2))
    m = re.search(r"task:\s*#(\d+)", body)
    if m:
        return int(m.group(1))
    m = re.match(r"agent/task-(\d+)(-|$)", head_ref)
    if m:
        return int(m.group(1))
    return None


def agent_of(labels: list[str] | tuple[str, ...] | None) -> str:
    """返回第一个 builder 标签（agent:muse-2/3），没有则空串。"""
    for label in labels or ():
        if label in BUILDERS:
            return label
    return ""


def utc_now_iso() -> str:
    """UTC ISO 8601（秒级），协议要求的时间格式。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_agent_header(
    message: str,
    task: int | None = None,
    sender: str = "github-actions",
    to: str | None = None,
    at: str | None = None,
    dispatch_id: str | None = None,
) -> str:
    """生成协议机器可读头（HTML 注释）。

    带 `dispatch_id` 时写在头内（TASK-07 要求评论头带幂等键），人读不到但
    可以稳定检索：同一 dispatch_id 的评论在任意分页位置都能被识别。
    """
    parts = [f"agent-msg: {message}"]
    if task is not None:
        parts.append(f"task: #{task}")
    parts.append(f"from: {sender}")
    if to:
        parts.append(f"to: {to}")
    parts.append(f"at: {at or utc_now_iso()}")
    if dispatch_id:
        parts.append(f"{DISPATCH_ID_FIELD} {dispatch_id}")
    return "<!-- " + ", ".join(parts) + " -->"


def comment_has_dispatch_id(body: str | None, dispatch_id: str) -> bool:
    """判断评论是否属于某个 dispatch（按头里的 dispatch_id 精确匹配）。"""
    if not dispatch_id:
        return False
    return bool(re.search(
        rf"dispatch_id:\s*{re.escape(dispatch_id)}(?![\w-])",
        body or "",
    ))
