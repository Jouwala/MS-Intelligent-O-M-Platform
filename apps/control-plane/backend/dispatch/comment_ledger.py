"""评论“只发一次”的对账工具（TASK-07）。

GitHub 评论接口没有“不存在才创建”的语义，只能先查再发；而查完到发之间
有窗口，POST 之后客户端超时也无法立刻判断是否生效。本模块把这个模式收敛
成一处：

1. 分页扫描（`client.list_issue_comments` 全量，不只看第一页）判断标记是否
   已存在；
2. `wait_timeout > 0` 时先等一会儿（给并发运行的赢家时间写完评论）；
3. 发送遇到传输层超时 → **先重扫对账**，确实没有才补发一次；
4. 补发后仍读不到 → 显式抛错（不静默当成功）。

真正的并发互斥由 `dispatch_registry.py` 的 git ref CAS 负责；本模块只保证
“重试 / 重复执行不产生第二条评论”。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .github_client import GitHubClient, GitHubTransportError

POLL_INTERVAL = 0.05


def comment_bodies(client: GitHubClient, number: int) -> list[str]:
    """该 Issue 的全量评论正文（分页跟随 Link；失败显式抛出）。"""
    return [c.get("body") or "" for c in client.list_issue_comments(number)]


def has_comment(
    client: GitHubClient, number: int, predicate: Callable[[str], bool]
) -> bool:
    return any(predicate(body) for body in comment_bodies(client, number))


def ensure_comment_once(
    client: GitHubClient,
    number: int,
    predicate: Callable[[str], bool],
    body: str,
    *,
    wait_timeout: float = 0.0,
    poll_interval: float = POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """保证满足 `predicate` 的评论恰好一条；返回处理方式字符串。"""
    if has_comment(client, number, predicate):
        return "exists"
    deadline = time.monotonic() + max(0.0, wait_timeout)
    while time.monotonic() < deadline:
        sleep(poll_interval)
        if has_comment(client, number, predicate):
            return "exists"
    try:
        client.post_issue_comment(number, body)
        return "posted"
    except GitHubTransportError:
        # 请求可能已生效：先对账，读得到就算成功，绝不盲目再发一条。
        if has_comment(client, number, predicate):
            print(f"[comments] Issue #{number} 评论请求超时，但对账确认已生效", flush=True)
            return "reconciled"
        client.post_issue_comment(number, body)
        if not has_comment(client, number, predicate):
            raise GitHubTransportError(
                f"Issue #{number} 评论补发后仍读不到目标标记，无法确认写入"
            )
        return "posted-after-timeout"
