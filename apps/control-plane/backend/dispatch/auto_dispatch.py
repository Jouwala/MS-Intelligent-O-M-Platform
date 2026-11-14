"""PR 合入后自动分派下一个就绪任务（原 `auto-dispatch.yml` 内嵌脚本）。

行为与安全阀沿用原工作流（见 `docs/workspace/github-automation.md` 第二节）：

- 只处理 merged == true 且 base 为 main 的 PR（workflow_dispatch 路径也再校验一次）；
- 就绪 = `state:todo` + 无其他 state 标签 + `agent:muse-2/3` + `depends on #N`
  依赖全关闭 + 无认领评论 + 无 TASK_ASSIGNED 分派评论；
- 优先派给本次合并空闲出来的 builder，其次编号最小的就绪任务；每次只派一个；
- 没有就绪任务只记日志，不报错。

修复的三处缺陷：

1. **全量分页**：候选 Issue（`state:todo`）与每个 Issue 的评论都走
   `client.paginate`，不再只读第一页。评论 >100 条时第 2 页的认领 / 分派
   记录能读到；Issue >100 条时第 2 页之后的任务能发现。
2. **并发幂等（TASK-07）**：任务选定后先 `POST /git/refs` 登记
   `dispatch/pr-merged/issue-<N>-<sha>-<digest>`（原子 CAS，见
   `dispatch_registry.py`），登记成功才发 TASK_ASSIGNED 评论；评论头带
   `dispatch_id`。并发运行只有一个赢家；重复投递 / workflow 重试命中已存在
   的 ref，只做对账，不再分派第二个任务。
3. **失败显式**：查询 / 分页失败不再 `except` 成“没记录”，直接抛错让
   workflow 变红，避免用不完整数据做分派决定。

调用方式（workflow 里就是这一行）：`python apps/control-plane/backend/dispatch/auto_dispatch.py`
环境变量：`REPO` / `GH_TOKEN` / `EVENT` / `PR_NUMBER_EVENT` / `PR_NUMBER_INPUT`
/ `MERGE_SHA_EVENT`。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

# 直接以文件方式运行时（workflow 的调用方式）把 backend 目录放进 sys.path，
# 这样 `dispatch` 包可导入；作为包被测试导入时这段是空操作。
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from dispatch.comment_ledger import ensure_comment_once  # noqa: E402
from dispatch.dispatch_registry import SOURCE_AUTO_DISPATCH, DispatchRegistry  # noqa: E402
from dispatch.github_client import GitHubAPIError, GitHubClient, client_from_env  # noqa: E402
from dispatch.protocol import (  # noqa: E402
    ASSIGNED_MARKER,
    CLAIM_MARKERS,
    agent_of,
    build_agent_header,
    comment_has_dispatch_id,
    parse_dependencies,
    resolve_issue_number,
    utc_now_iso,
)

# 对账等待：ref 已登记但评论暂时读不到时，先给“登记的赢家”一点时间把评论
# 写完；超时仍未出现才补发（覆盖“登记后进程崩溃 / 评论超时”）。
DEFAULT_RECONCILE_TIMEOUT = 2.0
POLL_INTERVAL = 0.05


def _labels_of(issue: dict[str, Any]) -> list[str]:
    return [lb.get("name", "") for lb in issue.get("labels", []) or []]


def _comment_bodies(client: GitHubClient, number: int) -> list[str]:
    """全量评论正文（分页跟随 Link）。失败直接抛出，不静默当成“没有”。"""
    return [c.get("body") or "" for c in client.list_issue_comments(number)]


def find_dispatch_comment(
    client: GitHubClient, number: int, dispatch_id: str
) -> bool:
    """分页扫描该 Issue 全部评论，判断某个 dispatch_id 是否已发过分派评论。"""
    return any(
        comment_has_dispatch_id(body, dispatch_id)
        for body in _comment_bodies(client, number)
    )


def ensure_dispatch_comment(
    client: GitHubClient,
    number: int,
    dispatch_id: str,
    body: str,
    *,
    wait_timeout: float = 0.0,
    poll_interval: float = POLL_INTERVAL,
    sleep=time.sleep,
) -> str:
    """保证该 dispatch 恰好有一条 TASK_ASSIGNED 评论。

    返回值：`exists` / `posted` / `reconciled`（超时后对账发现已生效）/
    `posted-after-timeout`（超时后确认没生效，补发一次）。

    - `wait_timeout > 0`：留给“另一个并发运行正在发评论”的观察窗口，窗口
      内出现评论就不再发（避免并发重复）；
    - 发送抛传输层超时：请求可能已经生效，先重扫评论对账，找不到才补发
      一次 —— 这是“API 成功但客户端超时不得产生第二条有效分派”的落点。
    """
    return ensure_comment_once(
        client,
        number,
        lambda comment_body: comment_has_dispatch_id(comment_body, dispatch_id),
        body,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        sleep=sleep,
    )


def ensure_todo_state(client: GitHubClient, number: int) -> None:
    """幂等回写标签：补 state:todo、摘残留 state:blocked。

    两个操作本身幂等（加标签去重、摘不存在的标签把 404 当成功），所以不先
    查标签再写 —— 没有 check-then-act 的竞态窗口。
    """
    client.add_issue_labels(number, ["state:todo"])
    client.remove_issue_label(number, "state:blocked")


def is_ready(client: GitHubClient, issue: dict[str, Any]) -> tuple[bool, str]:
    """四条就绪条件（见模块注释），返回 (是否就绪, 不就绪原因)。"""
    number = int(issue["number"])
    labels = _labels_of(issue)
    if "state:todo" not in labels:
        return False, "无 state:todo 标签"
    other_states = [
        lb for lb in labels
        if lb.startswith("state:") and lb not in ("state:todo", "state:blocked")
    ]
    if other_states:
        return False, f"已有其他状态标签 {other_states}"
    if not agent_of(labels):
        return False, "无 agent:muse-2/3 标签"
    for dep in parse_dependencies(issue.get("body") or ""):
        dep_issue = client.get_issue(dep)  # 查询失败直接抛错，不当成“未完成”或“已完成”
        if dep_issue.get("state") != "closed":
            return False, f"依赖 #{dep} 未完成"
    bodies = _comment_bodies(client, number)  # 全量分页
    if any(marker in body for body in bodies for marker in CLAIM_MARKERS):
        return False, "已有认领评论"
    if any(ASSIGNED_MARKER in body for body in bodies):
        return False, "已有 TASK_ASSIGNED 分派评论"
    return True, ""


def freed_builder(client: GitHubClient, pr: dict[str, Any]) -> str:
    """判定本次合并空闲出来的 builder：PR 标签 → 关联 Issue 标签。

    这个结果只影响“优先派给谁”的排序，查询失败只记警告、不阻断分派。
    """
    agent = agent_of(_labels_of(pr))
    if agent:
        return agent
    issue_no = resolve_issue_number(
        pr.get("body") or "", (pr.get("head") or {}).get("ref") or ""
    )
    if not issue_no:
        return ""
    try:
        issue = client.get_issue(issue_no)
    except GitHubAPIError as exc:
        print(f"[dispatch] 关联 Issue #{issue_no} 查询失败（仅影响优先级排序）：{exc}", flush=True)
        return ""
    return agent_of(_labels_of(issue))


def collect_ready(
    client: GitHubClient,
) -> list[tuple[dict[str, Any], str]]:
    """分页读取全部 state:todo Issue，筛出就绪任务。"""
    ready: list[tuple[dict[str, Any], str]] = []
    for issue in client.list_open_issues("state:todo"):
        number = issue.get("number")
        if number is None or "pull_request" in issue:
            continue
        ok, reason = is_ready(client, issue)
        if ok:
            agent = agent_of(_labels_of(issue))
            ready.append((issue, agent))
            print(f"[dispatch] #{number} 就绪（{agent}）", flush=True)
        else:
            print(f"[dispatch] #{number} 不就绪：{reason}", flush=True)
    return ready


def build_assignment_comment(
    number: int,
    agent_name: str,
    pr_number: int,
    dispatch_id: str,
    *,
    at: str | None = None,
) -> str:
    """TASK_ASSIGNED 评论：协议机器可读头（带 dispatch_id）+ 人可读正文。"""
    header = build_agent_header(
        "TASK_ASSIGNED",
        task=number,
        sender="github-actions",
        to=agent_name,
        at=at or utc_now_iso(),
        dispatch_id=dispatch_id,
    )
    return (
        f"{header}\n"
        f"任务已自动分派：PR #{pr_number} 合并后，{agent_name} 空闲，"
        f"本任务自动派给 {agent_name}。\n\n"
        f"请按协议认领：发认领评论并打 `state:claimed` 标签。"
        f"任务说明与验收标准见本 Issue 正文；依赖已全部满足，可以认领。\n"
    )


def reconcile_task(
    client: GitHubClient,
    registry: DispatchRegistry,
    number: int,
    sha: str,
    pr_number: int,
    *,
    reconcile_timeout: float = DEFAULT_RECONCILE_TIMEOUT,
) -> bool:
    """对账：登记过但状态可能没回写完 → 只补评论 / 标签，绝不重复分派。

    返回 True 表示该任务此前确实已登记（本次只对齐了状态）。补发评论时
    负责人从 Issue 标签重新解析，PR 号用本次触发 PR，保证补发内容与正常
    分派一致。
    """
    dispatch_id = registry.make_id("issue", number, sha, SOURCE_AUTO_DISPATCH)
    if not registry.exists("issue", number, sha, SOURCE_AUTO_DISPATCH):
        print(f"[dispatch] #{number} 的登记 ref 不存在，本次不做对账", flush=True)
        return False
    print(f"[dispatch] #{number} 已登记（dispatch_id={dispatch_id}），只做对账", flush=True)
    agent = agent_of(_labels_of(client.get_issue(number)))
    agent_name = agent.split(":", 1)[1] if agent else "未知"
    ensure_dispatch_comment(
        client,
        number,
        dispatch_id,
        build_assignment_comment(number, agent_name, pr_number, dispatch_id),
        wait_timeout=reconcile_timeout,
    )
    ensure_todo_state(client, number)
    return True


def reconcile_prior_dispatches(
    client: GitHubClient,
    registry: DispatchRegistry,
    sha: str,
    pr_number: int,
    *,
    reconcile_timeout: float,
) -> list[int]:
    """按触发（来源 + merge sha）找出已登记的分派并逐个对账。

    返回对账成功的目标 Issue 编号；空列表表示该触发还没有任何分派。
    """
    reconciled: list[int] = []
    for item in registry.find_for_trigger(sha, SOURCE_AUTO_DISPATCH):
        if item.get("kind") != "issue":
            continue
        number = int(item["number"])
        if reconcile_task(
            client, registry, number, sha, pr_number, reconcile_timeout=reconcile_timeout
        ):
            reconciled.append(number)
    return reconciled


def dispatch_next(
    client: GitHubClient,
    repo: str,
    pr_number: int,
    *,
    merge_sha: str | None = None,
    reconcile_timeout: float = DEFAULT_RECONCILE_TIMEOUT,
    at: str | None = None,
) -> dict[str, Any]:
    """主流程：校验 PR → 对账 → 选就绪任务 → CAS 登记 → 发分派评论。

    返回结构化结果（便于单测与日志）：
    `{"status": "skip"|"no-ready"|"dispatched"|"reconciled", ...}`。
    任何 GitHub 查询失败都向上抛，由 workflow 显式失败。
    """
    if pr_number <= 0:
        return {"status": "skip", "reason": "未取到 PR 编号"}
    pr = client.get_pull(pr_number)
    if pr.get("merged") is not True or (pr.get("base") or {}).get("ref") != "main":
        return {
            "status": "skip",
            "reason": (
                f"PR #{pr_number} 未合入 main（merged={pr.get('merged')}，"
                f"base={(pr.get('base') or {}).get('ref')}）"
            ),
        }
    sha = str(merge_sha or pr.get("merge_commit_sha") or "")
    if not sha:
        raise GitHubAPIError(
            f"PR #{pr_number} 已合入但拿不到 merge_commit_sha，无法生成稳定分派键"
        )

    registry = DispatchRegistry(client, repo)
    # ① 触发级对账：同一个合并（逻辑来源 + merge sha）此前已登记过分派？
    #    找到说明这是重复投递 / workflow 重试，只对齐状态，不再选新任务。
    reconciled = reconcile_prior_dispatches(
        client, registry, sha, pr_number, reconcile_timeout=reconcile_timeout
    )
    if reconciled:
        return {
            "status": "reconciled",
            "merge_sha": sha,
            "issues": reconciled,
            "reason": "该合并此前已分派过，本次只对账状态（幂等）",
        }

    # ② 正常路径：选一个就绪任务。
    freed = freed_builder(client, pr)
    print(f"[dispatch] PR #{pr_number} 已合入（{sha[:12]}），空闲 builder：{freed or '未知'}", flush=True)
    ready = collect_ready(client)
    if not ready:
        # 竞态兜底：本次扫描期间可能有并发运行刚好完成分派——它发完评论后
        # 任务就不再“就绪”，直接报 no-ready 会掩盖“已经派过一次”。再查一次
        # 触发登记，有就按对账返回，保证重试语义诚实（不派第二个任务）。
        raced = reconcile_prior_dispatches(
            client, registry, sha, pr_number, reconcile_timeout=reconcile_timeout
        )
        if raced:
            return {
                "status": "reconciled",
                "merge_sha": sha,
                "issues": raced,
                "reason": "扫描期间并发运行已完成分派，本次只对账状态（幂等）",
            }
        return {"status": "no-ready", "merge_sha": sha, "reason": "没有可分派的就绪任务"}

    def sort_key(item: tuple[dict[str, Any], str]) -> tuple[int, int]:
        issue, agent = item
        return (0 if agent == freed else 1, int(issue["number"]))

    ready.sort(key=sort_key)
    issue, agent = ready[0]
    number = int(issue["number"])
    agent_name = agent.split(":", 1)[1]

    # ③ 原子登记（CAS）。created=False 表示并发输家 / 已登记过：只对账。
    #    ref 锚到 merge commit（触发提交，一定存在于仓库）——名字里的 sha 才是
    #    语义来源，锚点只是 git ref 的合法性要求。
    registration = registry.register(
        "issue", number, sha, SOURCE_AUTO_DISPATCH, anchor_sha=sha
    )
    body = build_assignment_comment(number, agent_name, pr_number, registration.dispatch_id, at=at)
    outcome = ensure_dispatch_comment(
        client,
        number,
        registration.dispatch_id,
        body,
        wait_timeout=0.0 if registration.created else reconcile_timeout,
    )
    ensure_todo_state(client, number)
    return {
        "status": "dispatched" if registration.created else "reconciled",
        "merge_sha": sha,
        "issue": number,
        "agent": agent,
        "dispatch_id": registration.dispatch_id,
        "comment": outcome,
    }


def run_from_env(env: dict[str, str], client: GitHubClient | None = None) -> dict[str, Any]:
    """workflow 入口的纯逻辑部分（可注入客户端单测）。"""
    repo = env.get("REPO") or env.get("GITHUB_REPOSITORY") or ""
    if not repo:
        raise GitHubAPIError("缺少 REPO 环境变量")
    event = env.get("EVENT") or ""
    pr_number = int(env.get("PR_NUMBER_EVENT") or env.get("PR_NUMBER_INPUT") or "0")
    merge_sha = env.get("MERGE_SHA_EVENT") or ""
    print(f"[dispatch] event={event or '未知'} pr={pr_number}", flush=True)
    client = client or client_from_env(env)
    return dispatch_next(client, repo, pr_number, merge_sha=merge_sha)


def main() -> int:
    result = run_from_env(dict(os.environ))
    print(f"[dispatch] 结果：{result}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
