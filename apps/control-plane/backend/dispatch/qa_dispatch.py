"""QA 触发（原 `agent-orchestrator.yml` 的解析 / 去重 / webhook 步骤）。

保留既有触发路径与安全阀（见 `docs/workspace/github-automation.md` 第二节）：

- 三种触发：`workflow_run`（quality 成功）按 head SHA 找 open PR、
  `pull_request` 打 `state:qa-ready`、手动 `workflow_dispatch`；
- 只认 quality success 结论；草稿 PR 跳过；
- PR 已有 `state:qa-ready` 跳过（打标签路径会触发）；
- 无对应 open PR 只记日志；
- **同一 head SHA 只触发一次**。

修复：

1. **head SHA 去重换成 ref CAS**：原实现查“本工作流按 head SHA 的运行记录”，
   两个并发运行会同时读到“还没触发过”，各 POST 一次 webhook；且运行记录
   只覆盖本工作流、分页与状态判断都不可靠。现在用
   `POST /git/refs` 创建 `dispatch/qa-trigger/pr-<N>-<headsha>` 做原子登记，
   422 = 已触发（并发输家 / 重试），只有赢家 POST webhook。
2. **全量分页**：open PR 列表、Issue 解析等查询都走 `client.paginate`。
3. **失败语义**：webhook 明确返回非 2xx → 回滚登记 ref 并抛错（允许重试，
   与原“failure 的运行不算已触发”一致）；传输层超时 → 请求可能已被接受，
   **保留登记**（宁可漏一次重试也不重复触发同一 SHA），显式抛错。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

# 直接以文件方式运行时把 backend 目录放进 sys.path（见 auto_dispatch.py 同款注释）。
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from dispatch.dispatch_registry import SOURCE_QA, DispatchRegistry  # noqa: E402
from dispatch.github_client import (  # noqa: E402
    GitHubAPIError,
    GitHubClient,
    GitHubTransportError,
    client_from_env,
)
from dispatch.protocol import resolve_issue_number  # noqa: E402

WEBHOOK_TIMEOUT = 20.0
PostWebhook = Callable[[str, str, dict[str, Any]], int]


class WebhookError(RuntimeError):
    """webhook 明确返回非 2xx / 配置缺失（登记 ref 已回滚，可重试）。"""


def _labels_of(pr: dict[str, Any]) -> list[str]:
    return [lb.get("name", "") for lb in pr.get("labels", []) or []]


def build_payload(
    repo: str,
    pr_number: int,
    head_sha: str,
    issue_number: int | None,
    run_url: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event": "qa_requested",
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "run_url": run_url,
    }
    if issue_number:
        payload["issue_number"] = issue_number
    return payload


def default_post_webhook(url: str, key: str, payload: dict[str, Any]) -> int:
    """真实 webhook POST（标准库 urllib），返回 HTTP 状态码。"""
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "noc-soc-dispatch/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:  # 传输失败：不确定是否已被接受
        raise GitHubTransportError(f"webhook POST 失败（结果未知）：{exc}") from exc


def resolve_target(
    client: GitHubClient,
    *,
    event: str,
    pr_number: int,
    head_sha: str,
    issue_number: int | None,
) -> dict[str, Any] | None:
    """按触发路径解析 PR / head SHA / 关联 Issue；返回 None 表示跳过。"""
    if event == "workflow_run":
        sha = head_sha
        if not sha:
            return None
        target_pr = None
        for pr in client.list_open_pulls():  # 全量分页
            head = (pr.get("head") or {}).get("sha")
            base = (pr.get("base") or {}).get("ref")
            if head == sha and base == "main":
                target_pr = pr
                break
        if target_pr is None:
            print(f"[qa] head {sha[:12]} 没有对应的 open PR（base 为 main），跳过", flush=True)
            return None
        number = int(target_pr["number"])
        issue_number = issue_number or resolve_issue_number(
            target_pr.get("body") or "", (target_pr.get("head") or {}).get("ref") or ""
        )
        return {
            "pr": target_pr,
            "pr_number": number,
            "head_sha": sha,
            "issue_number": issue_number,
        }
    if pr_number <= 0:
        print("[qa] 未取到 PR 编号，跳过", flush=True)
        return None
    pr = client.get_pull(pr_number)
    sha = str((pr.get("head") or {}).get("sha") or "")
    if not sha:
        raise GitHubAPIError(f"PR #{pr_number} 解析不到 head SHA")
    issue_number = issue_number or resolve_issue_number(
        pr.get("body") or "", (pr.get("head") or {}).get("ref") or ""
    )
    return {"pr": pr, "pr_number": pr_number, "head_sha": sha, "issue_number": issue_number}


def trigger_qa(
    client: GitHubClient,
    repo: str,
    *,
    event: str = "",
    pr_number: int = 0,
    head_sha: str = "",
    issue_number: int | None = None,
    run_url: str = "",
    webhook_url: str = "",
    webhook_key: str = "",
    post_webhook: PostWebhook | None = None,
) -> dict[str, Any]:
    """去重 + 登记 + POST webhook。返回结构化结果供单测 / 日志。"""
    if not webhook_url or not webhook_key:
        raise WebhookError("缺少 GROK_ROUTINE_WEBHOOK_URL / GROK_ROUTINE_WEBHOOK_KEY")
    target = resolve_target(
        client,
        event=event,
        pr_number=pr_number,
        head_sha=head_sha,
        issue_number=issue_number,
    )
    if target is None:
        return {"status": "skip", "reason": "无对应 open PR 或 PR 编号缺失"}
    pr = target["pr"]
    number = int(target["pr_number"])
    sha = str(target["head_sha"])
    labels = _labels_of(pr)

    if pr.get("draft") is True:
        return {"status": "skip", "reason": f"PR #{number} 是草稿"}
    if "state:qa-ready" in labels:
        return {"status": "skip", "reason": f"PR #{number} 已有 state:qa-ready 标签"}
    registry = DispatchRegistry(client, repo)
    already = registry.exists("pr", number, sha, SOURCE_QA)
    if "smoke-passed" in labels and already:
        return {"status": "skip", "reason": f"PR #{number} 已有 smoke-passed 且该 SHA 已触发过"}
    if already:
        return {"status": "skip", "reason": f"head SHA {sha[:12]} 已触发过 QA（幂等）"}

    # 原子登记：并发运行 / 重复投递只有一个赢家能 POST webhook。
    registration = registry.register("pr", number, sha, SOURCE_QA, anchor_sha=sha)
    if not registration.created:
        return {
            "status": "skip",
            "reason": f"head SHA {sha[:12]} 已被并发运行登记，本次不重复触发",
            "dispatch_id": registration.dispatch_id,
        }

    payload = build_payload(repo, number, sha, target["issue_number"], run_url)
    poster = post_webhook or default_post_webhook
    try:
        status = poster(webhook_url, webhook_key, payload)
    except GitHubTransportError:
        # 结果未知（可能已被接受）：保留登记，宁可少重试一次也不重复触发同一 SHA。
        print("[qa] webhook 结果未知（超时），保留登记 ref 防止重复触发", flush=True)
        raise
    if not 200 <= int(status) < 300:
        # 明确非 2xx：Grok 没接受 → 回滚登记，允许下次重试（与原行为一致）。
        registry.release(registration.dispatch_id, SOURCE_QA)
        raise WebhookError(f"webhook HTTP {status}，已回滚登记 ref 允许重试")
    print(
        f"[qa] PR #{number} head {sha[:12]} 已触发 QA（dispatch_id={registration.dispatch_id}）",
        flush=True,
    )
    return {
        "status": "triggered",
        "pr_number": number,
        "head_sha": sha,
        "issue_number": target["issue_number"],
        "dispatch_id": registration.dispatch_id,
        "http_status": int(status),
    }


def run_from_env(
    env: dict[str, str],
    client: GitHubClient | None = None,
    post_webhook: PostWebhook | None = None,
) -> dict[str, Any]:
    repo = env.get("REPO") or env.get("GITHUB_REPOSITORY") or ""
    if not repo:
        raise GitHubAPIError("缺少 REPO 环境变量")
    event = env.get("EVENT") or ""
    pr_number = int(env.get("PR_NUMBER_EVENT") or env.get("PR_NUMBER_INPUT") or "0")
    issue_raw = env.get("ISSUE_NUMBER_INPUT") or ""
    issue_number = int(issue_raw) if issue_raw.isdigit() else None
    print(f"[qa] event={event or '未知'} pr={pr_number} run={env.get('RUN_ID', '')}", flush=True)
    client = client or client_from_env(env)
    return trigger_qa(
        client,
        repo,
        event=event,
        pr_number=pr_number,
        head_sha=env.get("RUN_HEAD_SHA") or "",
        issue_number=issue_number,
        run_url=env.get("RUN_URL") or "",
        webhook_url=env.get("WEBHOOK_URL") or "",
        webhook_key=env.get("WEBHOOK_KEY") or "",
        post_webhook=post_webhook,
    )


def main() -> int:
    result = run_from_env(dict(os.environ))
    print(f"[qa] 结果：{result}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
