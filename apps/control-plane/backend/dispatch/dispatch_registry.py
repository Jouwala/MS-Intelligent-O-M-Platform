"""分派幂等登记：用 GitHub git ref 创建做原子 CAS（TASK-07）。

**为什么不用“先查评论再发评论”**：那是 check-then-act。两个 workflow 并发
运行时都能读到“还没有分派记录”，于是各发一条 TASK_ASSIGNED，同一个任务被
分派两次。评论接口没有“不存在才创建”的语义，而 `POST /git/refs` 有：
ref 名全局唯一，后到的请求拿到 422，天然互斥。

流程（见 `auto_dispatch.py`）：

1. 由稳定键生成 `dispatch_id` = `repo + 目标(kind/number) + sha + 逻辑触发来源`；
2. `POST /git/refs` 创建 `refs/heads/dispatch/<source>/<dispatch_id>`：
   - 201 → 本次是唯一赢家，去发 TASK_ASSIGNED 评论；
   - 422 且该 ref 确实已存在 → 别人/上次已登记，进入对账（见第 3 步）；
   - 422 但 ref 不存在 → 说明锚点 SHA 无效等真实错误，**显式抛错**；
3. 对账：分页扫评论找 `dispatch_id:`，已有则不重复发；缺失（上次登记后
   进程崩溃 / 评论超时没写成）才补发一次。

**来源为什么用逻辑值**：`pull_request closed` 与 auto-merge 的
`workflow_dispatch` 是同一个逻辑事件（PR 合入 main）的两条投递路径。把
“来源”记成 workflow 事件名会让两条路径算出不同 dispatch_id，同一合并被
分派两次。这里统一记逻辑来源 `pr-merged` / `qa-trigger` / `cron-sweep`，重复投递自然
命中同一个 ref。

**残余风险**（已写进交接报告）：并发双投递若在“选择目标任务”之后才错开，
两次运行可能选中不同 Issue、各登记一次（各自 ref 不同）。这不是“同一任务
被分派两次”，但一次合并会派出两个任务。缓解办法是交付方后续给触发级加
独立登记；当前每次合并只派一个任务的行为未变。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .github_client import GitHubAPIError, GitHubClient

# 逻辑触发来源（不是 workflow 事件名，见模块注释）。
SOURCE_AUTO_DISPATCH = "pr-merged"
SOURCE_QA = "qa-trigger"
#: 每小时 :17 的定时巡检（没有 PR 合入时的兜底分派）。
SOURCE_CRON_SWEEP = "cron-sweep"
#: Issue 事件触发（新 Issue 创建 / 依赖 Issue 关闭）：即时分派，不等巡检。
SOURCE_ISSUE_EVENT = "issue-event"

REF_ROOT = "dispatch"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DISPATCH_ID_RE = re.compile(r"^(?P<kind>issue|pr)-(?P<number>\d+)-(?P<sha>[a-z0-9]+)-(?P<digest>[0-9a-f]{10})$")


def _slug(value: str, limit: int = 40) -> str:
    """把任意字符串收敛成 ref 安全的小写 slug。"""
    return _SLUG_RE.sub("-", str(value).lower()).strip("-")[:limit]


def _sha_token(sha: str) -> str:
    token = _slug(sha, limit=12)
    if not token:
        raise ValueError(f"分派键需要非空 sha，实际：{sha!r}")
    return token


def make_dispatch_id(repo: str, kind: str, number: int, sha: str, source: str) -> str:
    """稳定 dispatch_id：同 repo + 目标 + sha + 来源必然得到同一个值。

    末尾 10 位摘要是 repo / source 等字段的哈希，保证 dispatch_id 自身就
    唯一标识“这次分派”；前缀保留人可读信息，方便在 ref 列表里排查。
    """
    if kind not in ("issue", "pr"):
        raise ValueError(f"kind 只能是 issue/pr，实际：{kind!r}")
    sha_token = _sha_token(sha)
    canonical = f"{repo}|{kind}|{number}|{sha}|{source}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    return f"{kind}-{int(number)}-{sha_token}-{digest}"


def dispatch_ref(dispatch_id: str, source: str) -> str:
    """dispatch_id → 完整 ref 名。"""
    return f"refs/heads/{REF_ROOT}/{_slug(source)}/{dispatch_id}"


def parse_dispatch_id(dispatch_id: str) -> dict[str, object] | None:
    """解析 dispatch_id；格式不匹配返回 None（ref 列表里可能有历史脏数据）。"""
    match = _DISPATCH_ID_RE.match(dispatch_id or "")
    if not match:
        return None
    return {
        "kind": match.group("kind"),
        "number": int(match.group("number")),
        "sha_token": match.group("sha"),
        "dispatch_id": dispatch_id,
    }


@dataclass(frozen=True)
class DispatchRegistration:
    """一次 CAS 登记的结果。created=True 表示本次是唯一赢家。"""

    dispatch_id: str
    ref: str
    created: bool


class DispatchRegistry:
    """基于 git ref 的分派登记簿（同一 GitHub 仓库内全局唯一）。"""

    def __init__(self, client: GitHubClient, repo: str) -> None:
        self.client = client
        self.repo = repo

    # ---------------------------------------------------------------- 键
    def make_id(self, kind: str, number: int, sha: str, source: str) -> str:
        return make_dispatch_id(self.repo, kind, number, sha, source)

    @staticmethod
    def ref_for(dispatch_id: str, source: str) -> str:
        return dispatch_ref(dispatch_id, source)

    # ---------------------------------------------------------------- 锚点
    def resolve_anchor_sha(self, preferred: str | None = None) -> str:
        """ref 必须指向一个已存在的 commit：优先用触发提交，否则用 main 头。"""
        if preferred:
            return preferred
        return self.client.get_branch_sha("main")

    # ---------------------------------------------------------------- CAS
    def register(
        self,
        kind: str,
        number: int,
        sha: str,
        source: str,
        *,
        anchor_sha: str | None = None,
    ) -> DispatchRegistration:
        """原子登记；422 表示已登记，返回 created=False。"""
        dispatch_id = self.make_id(kind, number, sha, source)
        ref = self.ref_for(dispatch_id, source)
        anchor = self.resolve_anchor_sha(anchor_sha)
        try:
            self.client.create_git_ref(ref, anchor)
        except GitHubAPIError as exc:
            if exc.status not in (422, 409):
                raise
            # 422 有两种含义：ref 已存在（幂等）或 sha 无效（真错误）。
            # 用一次读确认，避免把“锚点无效”误当成“已登记”而静默丢分派。
            if self.client.get_git_ref(ref) is None:
                raise GitHubAPIError(
                    f"创建分派登记 ref 返回 HTTP {exc.status}，但 {ref} 不存在"
                    f"（可能是锚点 SHA {anchor[:12]} 无效），不能当作已登记继续",
                    status=exc.status,
                    method=exc.method,
                    path=exc.path,
                    body=exc.body,
                ) from exc
            print(f"[dispatch] {ref} 已存在，按已登记处理（幂等）", flush=True)
            return DispatchRegistration(dispatch_id=dispatch_id, ref=ref, created=False)
        print(f"[dispatch] 登记成功 {ref}（anchor={anchor[:12]}）", flush=True)
        return DispatchRegistration(dispatch_id=dispatch_id, ref=ref, created=True)

    def release(self, dispatch_id: str, source: str) -> bool:
        """回滚登记（用于“登记后动作明确失败、需要允许重试”的场景）。"""
        ref = self.ref_for(dispatch_id, source)
        return self.client.delete_git_ref(ref)

    def exists(self, kind: str, number: int, sha: str, source: str) -> bool:
        ref = self.ref_for(self.make_id(kind, number, sha, source), source)
        return self.client.get_git_ref(ref) is not None

    # ---------------------------------------------------------------- 对账
    def find_for_trigger(self, sha: str, source: str) -> list[dict[str, object]]:
        """找出该触发（来源 + sha）已登记过的分派。

        用于“评论已发、状态回写失败，workflow 重试”时的对齐：重试运行先
        按触发找登记记录 → 找到目标任务 → 只补状态，不再分派新任务。
        """
        prefix = f"heads/{REF_ROOT}/{_slug(source)}"
        sha_token = _sha_token(sha)
        results: list[dict[str, object]] = []
        for item in self.client.list_matching_refs(prefix):
            ref = item.get("ref") or ""
            dispatch_id = ref.rsplit("/", 1)[-1]
            parsed = parse_dispatch_id(dispatch_id)
            if parsed is None or f"-{sha_token}-" not in dispatch_id:
                continue
            results.append({**parsed, "ref": ref, "object": item.get("object") or {}})
        return results
