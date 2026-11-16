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

#: ISO 8601（秒级，UTC，`Z` 结尾）——协议里所有 `at:` / 认领时间都用这个格式。
ISO_TS_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"

#: auto-fix 工作流建的修复 Issue 带这行标记：`<!-- auto-fix: for-issue-99 ... -->`。
_AUTO_FIX_PARENT_RE = re.compile(r"auto-fix:[^>]*?\bfor-issue-(?P<parent>\d+)")

_HEADER_TIME_RE = re.compile(r"agent-msg:\s*[A-Z_]+[^>]*?\bat:\s*(" + ISO_TS_PATTERN + r")")
_CLAIM_TIME_RE = re.compile(r"agent-claim:\s*[^,\n>]+,\s*(" + ISO_TS_PATTERN + r")")

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


def auto_fix_parent(body: str | None) -> int | None:
    """auto-fix 修复 Issue 的「原 Issue」编号（标记 `<!-- auto-fix: for-issue-N ... -->`）。

    这类 Issue 由 QA 失败自动创建，正文模板要求写 `depends on #<原 Issue>`；
    但原 Issue 要等本修复合入后才关闭，两者互相等待——分派链路必须把这条
    依赖识别为「自依赖」并忽略，否则修复任务永远不就绪（2026-11-16 #108 实测）。
    """
    match = _AUTO_FIX_PARENT_RE.search(body or "")
    return int(match.group("parent")) if match else None


def effective_dependencies(body: str | None) -> list[int]:
    """分派实际该检查的依赖：去掉 auto-fix 修复任务对其原 Issue 的自依赖。"""
    deps = parse_dependencies(body)
    parent = auto_fix_parent(body)
    if parent is not None and parent in deps:
        deps.remove(parent)
    return deps


def parse_marker_time(body: str | None) -> datetime | None:
    """从协议评论里取时间：`agent-msg: ..., at: <ISO>` 或 `agent-claim: <agent>, <ISO>`。

    读不到（老评论没写 `at:` / 格式不符）返回 None，调用方按“未知时间”保守处理。
    """
    text = body or ""
    match = _HEADER_TIME_RE.search(text) or _CLAIM_TIME_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:  # 正则已限定格式，理论上到不了这里
        return None


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


def agent_of(
    labels: list[str] | tuple[str, ...] | None,
    builders: tuple[str, ...] | list[str] = (),
) -> str:
    """返回第一个命中白名单的 agent 标签；没有则空串。

    白名单由调用方传入（来源是 `builder_registry.load_builders()`，即
    `docs/workspace/agent-registry.md`），本模块保持零 I/O、不持有名单副本。
    """
    allowed = tuple(builders)
    for label in labels or ():
        if label in allowed:
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
