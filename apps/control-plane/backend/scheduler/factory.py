"""调度器工厂（ADR-7 §五 / §八 PR-5：Worker 一次性切到 GitHub 权威层）。

**权威唯一性**（ADR-7 §零）：GitHub refs 是唯一权威。本工厂只组装
`ref_service.GitHubRefScheduler`，不提供 SQLite 回退——本地 SQLite 调度器已在 PR-6
删除（代码/测试/文档一并退场），任何「两个权威并存」都会导致状态分叉，禁止保留。

构造：`make_scheduler(transport=..., repo=...)`
- `transport`：`adapters/github_issue.RestGitHubTransport`（评论/标签通道，duck-typed）；
- `repo`：`owner/name`，缺省时从 `transport.owner/transport.repo` 推导；
- 可选 `ref_store`：测试注入或复用连接；不传则以 `transport` 的 token/api_base
  新建 `github_refs.RestRefStore`；
- `make_ref_store(...)` 单独导出给 cron Reaper（`dispatch/lease_reaper.py`）复用同一
  refs 网络通道构造逻辑。

实现层全部延迟导入：只在真正构造时才 import `github_refs` / `ref_service`，缺失时给出
可执行的清晰错误，而不是裸 ImportError 栈。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from . import ref_api
from .api import utcnow

DEFAULT_REF_TTL = ref_api.DEFAULT_REF_LEASE_TTL


def _repo_from_transport(transport) -> str:
    """从 `adapters.RestGitHubTransport`（duck-typed）推导 owner/name。"""
    owner = str(getattr(transport, "owner", "") or "").strip()
    repo = str(getattr(transport, "repo", "") or "").strip()
    if owner and repo and "/" not in repo:
        return f"{owner}/{repo}"
    if repo and "/" in repo:
        return repo
    return ""


def make_ref_store(
    repo: str,
    *,
    token: Optional[str] = None,
    api_base: str = "https://api.github.com",
    http_open=None,
    sleep=None,
):
    """构造 GitHub refs 权威层存储（`github_refs.RestRefStore`，延迟导入）。"""
    text = str(repo or "").strip()
    if not text or "/" not in text:
        raise ValueError(f"github 权威层需要 owner/name 形式的 repo，实际：{repo!r}")
    from .github_refs import RestRefStore  # noqa: PLC0415 - 实现层延迟导入

    return RestRefStore(
        text,
        token=token,
        api_base=api_base,
        http_open=http_open,
        sleep=sleep,
    )


def make_scheduler(
    *,
    transport=None,
    repo: str = "",
    clock=utcnow,
    lease_ttl: timedelta = DEFAULT_REF_TTL,
    registry_issue: Optional[int] = None,
    ref_store=None,
    case_insensitive_scopes: bool = False,
):
    """构造唯一的 GitHub 权威调度器（满足 `api.Scheduler` 协议）。

    参数缺失（无 transport / 无 repo）时抛 `ValueError`；实现层未就绪时抛
    `RuntimeError`。**没有 sqlite 分支**：回滚靠 revert PR，不靠运行时切换权威。

    `GitHubRefScheduler` 冻结构造签名（Lead 2026-11-14 确认）：
    `(ref_store, transport, *, repo="", clock=utcnow, lease_ttl=..., registry_issue=None,
    case_insensitive_scopes=False)`——这里只传必需的冻结参数；未传的可选参数取实现层
    默认值（`case_insensitive_scopes=False` 与 ADR-4 大小写敏感策略一致）。
    """
    if transport is None:
        raise ValueError(
            "github 权威层需要 transport（adapters/github_issue.RestGitHubTransport），"
            "它负责评论/标签回写与注册表心跳"
        )
    resolved_repo = str(repo or "").strip() or _repo_from_transport(transport)
    if not resolved_repo or "/" not in resolved_repo:
        raise ValueError(
            "github 权威层需要 repo（owner/name）：请显式传 repo=，"
            f"或提供带 owner/repo 属性的 transport；当前 repo={repo!r}"
        )
    if ref_store is None:
        ref_store = make_ref_store(
            resolved_repo,
            token=getattr(transport, "token", None),
            api_base=str(getattr(transport, "api_base", "") or "https://api.github.com"),
            sleep=getattr(transport, "_sleep", None),
        )

    try:
        from .ref_service import GitHubRefScheduler  # noqa: PLC0415 - 实现层延迟导入
    except ImportError as exc:  # pragma: no cover - 仅实现层缺失时触发
        raise RuntimeError(
            "GitHub 权威调度器实现 scheduler/ref_service.py 不可用；"
            f"该实现由 PR-5 提供，请确认已合入（{exc}）"
        ) from exc

    return GitHubRefScheduler(
        ref_store,
        transport,
        repo=resolved_repo,
        clock=clock,
        lease_ttl=lease_ttl,
        registry_issue=registry_issue,
        case_insensitive_scopes=case_insensitive_scopes,
    )
