"""GitHub REST 客户端：全量分页、重试、显式失败（TASK-01）。

设计要点（都是对原内嵌脚本缺陷的直接修复）：

1. **分页必须跟 Link**：GitHub 的列表接口用 `Link: <url>; rel="next"` 翻页。
   原脚本只取 `per_page=100` 的第一页，评论 >100 条时漏掉认领 / 分派记录，
   Issue >100 条时漏掉第 2 页之后的任务。这里 `paginate()` 逐页跟随 Link，
   按条目 id 去重，空页停止，页数超上限**显式报错**而不是返回半截数据。
2. **只对幂等请求重试**：GET 遇到 5xx / 429 / 限流 403 时重试；POST 这类
   非幂等写操作不自动重试（重试可能产生第二条评论），由调用方做对账。
3. **失败显式**：任何 4xx 非限流、重试耗尽的 5xx、分页超限都抛
   `GitHubAPIError` / `GitHubPaginationError`，禁止 `except: pass` 后用
   “没记录”继续做分派决定。
4. **可注入传输层**：`_raw_request` 是唯一网络入口，测试用内存假 API 覆写
   它，就能对真实的分页 / 重试 / 对账逻辑做真实并发与故障注入测试。

仅用标准库（与仓库守卫的零第三方依赖要求一致）。
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

DEFAULT_BASE_URL = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 200
RETRY_STATUSES = frozenset({500, 502, 503, 504})
MAX_RETRY_AFTER = 30.0

_WHITESPACE = re.compile(r"\s+")


class GitHubError(RuntimeError):
    """本模块所有错误的基类（调用方可按需分别捕获）。"""


class GitHubAPIError(GitHubError):
    """GitHub 返回非 2xx，或重试耗尽。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        method: str = "",
        path: str = "",
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path
        self.body = body


class GitHubTransportError(GitHubError):
    """传输层失败（超时 / 连接失败）：请求可能已生效，也可能没有。"""


class GitHubPaginationError(GitHubAPIError):
    """分页失败或超过上限：绝不能用不完整数据继续做决定。"""


def parse_link_next(header_value: str | None) -> str | None:
    """从 `Link` 头里取 `rel="next"` 的 URL；没有则 None。

    GitHub 格式：`<https://api.github.com/...?page=2>; rel="next", <...>; rel="last"`。
    这里按逗号切分再用正则取，兼容带空格 / 不带引号的写法。
    """
    if not header_value:
        return None
    for part in header_value.split(","):
        match = re.search(r"<([^>]+)>", part)
        if not match:
            continue
        rel = re.search(r'rel\s*=\s*"?([^";]+)"?', part)
        if rel and "next" in rel.group(1).split():
            return match.group(1).strip()
    return None


def _dedupe_key(item: Any) -> tuple[str, str]:
    """分页去重的稳定键：优先 id / number / ref / sha，其次整条 JSON。"""
    if isinstance(item, Mapping):
        for field in ("id", "number", "ref", "sha"):
            value = item.get(field)
            if value is not None:
                return (field, str(value))
    try:
        return ("json", json.dumps(item, sort_keys=True, ensure_ascii=False, default=str))
    except (TypeError, ValueError):  # pragma: no cover - 正常 GitHub JSON 不会走到
        return ("repr", repr(item))


def ref_param(ref: str) -> str:
    """单 ref 接口的路径参数形式：`heads/<branch>`（文档口径，不带 `refs/` 前缀）。

    GitHub 的 `GET /git/ref/{ref}` 与 `DELETE /git/refs/{ref}` 示例都是
    `heads/feature-a`；`POST /git/refs` 的 payload 才要求完整 `refs/heads/...`。
    这里统一转换，避免拿完整 ref 去 DELETE / GET 拿到 404 后误判“没登记”。
    """
    return ref[len("refs/"):] if ref.startswith("refs/") else ref


class GitHubClient:
    """GitHub REST 客户端基类。

    子类只需实现 `_raw_request`（返回 `(status, headers, body)`；传输失败抛
    `GitHubTransportError`）。基类负责分页、重试与显式报错。
    """

    def __init__(
        self,
        repo: str,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        per_page: int = PER_PAGE,
        max_pages: int = MAX_PAGES,
        retry_limit: int = 3,
        retry_base_delay: float = 0.5,
        timeout: float = 20.0,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not repo or "/" not in repo:
            raise GitHubError(f"REPO 必须是 owner/name 形式，实际：{repo!r}")
        self.repo = repo
        self.token = token or ""
        self.base_url = base_url.rstrip("/")
        self.per_page = per_page
        self.max_pages = max_pages
        self.retry_limit = max(0, retry_limit)
        self.retry_base_delay = retry_base_delay
        self.timeout = timeout
        self._sleep = sleep or time.sleep
        # 便于工作流日志排查：记录实际发出的请求（测试断言重试次数也用它）。
        self.requests: list[tuple[str, str]] = []

    # ------------------------------------------------------------------ 传输层
    def _raw_request(
        self,
        method: str,
        url: str,
        payload: Any,
        headers: Mapping[str, str],
    ) -> tuple[int, dict[str, str], Any]:
        raise NotImplementedError

    # ------------------------------------------------------------------ URL
    def _resolve(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        if path.startswith(("http://", "https://")):
            url = path
        else:
            url = self.base_url + ("/" + path.lstrip("/"))
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            url += ("&" if "?" in url else "?") + query
        return url

    # ------------------------------------------------------------------ 重试/请求
    @staticmethod
    def _is_rate_limited(status: int, headers: Mapping[str, str], body: Any) -> bool:
        if status == 429:
            return True
        if status != 403:
            return False
        if headers.get("x-ratelimit-remaining") == "0":
            return True
        if "retry-after" in headers:
            return True
        message = body.get("message", "") if isinstance(body, Mapping) else ""
        return "rate limit" in str(message).lower()

    def _retry_delay(self, status: int, headers: Mapping[str, str], attempt: int) -> float:
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                return min(MAX_RETRY_AFTER, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(MAX_RETRY_AFTER, self.retry_base_delay * (2 ** (attempt - 1)))

    def _request_full(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        idempotent: bool | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """发请求并返回 `(body, headers)`；失败按重试策略处理，最终显式抛错。"""
        if idempotent is None:
            idempotent = method.upper() in ("GET", "HEAD")
        url = self._resolve(path)
        self.requests.append((method.upper(), url))
        attempts = self.retry_limit + 1 if idempotent else 1
        for attempt in range(1, attempts + 1):
            try:
                status, headers, body = self._raw_request(method.upper(), url, payload, {})
            except GitHubTransportError:
                if attempt < attempts:
                    self._sleep(self._retry_delay(599, {}, attempt))
                    continue
                raise
            if 200 <= status < 300:
                return body, headers
            message = body.get("message", "") if isinstance(body, Mapping) else ""
            error = GitHubAPIError(
                f"GitHub API {method.upper()} {url} 返回 HTTP {status}：{message}",
                status=status,
                method=method.upper(),
                path=url,
                body=body,
            )
            can_retry = (
                attempt < attempts
                and (status in RETRY_STATUSES or self._is_rate_limited(status, headers, body))
            )
            if can_retry:
                self._sleep(self._retry_delay(status, headers, attempt))
                continue
            raise error
        raise GitHubTransportError(  # pragma: no cover - 循环必然 return/raise
            f"GitHub API {method.upper()} {url} 重试耗尽"
        )

    def _request(self, method: str, path: str, payload: Any = None, *, idempotent: bool | None = None) -> Any:
        body, _headers = self._request_full(method, path, payload, idempotent=idempotent)
        return body

    # ------------------------------------------------------------------ 分页
    def paginate(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        extract: Callable[[Any], Any] | None = None,
        max_pages: int | None = None,
    ) -> list[Any]:
        """全量读取列表接口。

        - 每页 `per_page=100`（可被调用方覆盖）；
        - 跟随 `Link: rel="next"`；
        - 按 id 去重（防御 GitHub 服务端重叠页）；
        - 空页停止；
        - 超过 `max_pages` 抛 `GitHubPaginationError`（不静默截断）。
        """
        limit = self.max_pages if max_pages is None else max_pages
        query = dict(params or {})
        query.setdefault("per_page", self.per_page)
        url: str | None = self._resolve(path, query)
        collected: list[Any] = []
        seen: set[tuple[str, str]] = set()
        pages = 0
        while url:
            body, headers = self._request_full("GET", url)
            page_items = extract(body) if extract else body
            if not isinstance(page_items, list):
                raise GitHubPaginationError(f"分页接口返回非列表：{url}")
            for item in page_items:
                key = _dedupe_key(item)
                if key in seen:
                    continue
                seen.add(key)
                collected.append(item)
            pages += 1
            if not page_items:
                break
            next_url = parse_link_next(headers.get("link"))
            if not next_url:
                break
            if pages >= limit:
                raise GitHubPaginationError(
                    f"分页超过上限 {limit} 页，拒绝用不完整数据继续：{url}"
                )
            url = next_url
        return collected

    # ------------------------------------------------------------------ 高层 API
    def get_issue(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self.repo}/issues/{number}")

    def list_issue_comments(self, number: int) -> list[dict[str, Any]]:
        """全量评论（不再只取第一页）。"""
        return self.paginate(f"/repos/{self.repo}/issues/{number}/comments")

    def post_issue_comment(self, number: int, body: str) -> dict[str, Any]:
        # 非幂等：不自动重试；调用方负责“超时后先对账再决定是否补发”。
        return self._request(
            "POST",
            f"/repos/{self.repo}/issues/{number}/comments",
            {"body": body},
            idempotent=False,
        )

    def update_issue_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        """就地更新一条评论（PATCH）。告警收件箱的去重就靠它：同类告警只改
        最新一条同 marker 评论的正文，不再追加新评论刷屏。"""
        return self._request(
            "PATCH",
            f"/repos/{self.repo}/issues/comments/{comment_id}",
            {"body": body},
            idempotent=True,
        )

    def update_issue(self, number: int, body: str) -> dict[str, Any]:
        """就地更新一个 Issue 的正文（PATCH）。状态视图就靠它：固定 Issue
        的正文每次刷新时整体重写，不另开 Issue、不追加评论。"""
        return self._request(
            "PATCH",
            f"/repos/{self.repo}/issues/{number}",
            {"body": body},
            idempotent=True,
        )

    def create_issue(self, title: str, body: str) -> dict[str, Any]:
        """新建 Issue（告警收件箱首次使用时建固定收件箱）。"""
        return self._request(
            "POST",
            f"/repos/{self.repo}/issues",
            {"title": title, "body": body},
            idempotent=False,
        )

    def list_issue_events(self, number: int) -> list[dict[str, Any]]:
        """Issue 时间线事件（labeled/unlabeled 等），全量分页。

        阻塞升级用它取 `state:blocked` 标签的打上时间（blocked-since），
        不靠评论时间推断。"""
        return self.paginate(f"/repos/{self.repo}/issues/{number}/events")

    def list_issues(
        self, state: str = "open", labels: str | None = None
    ) -> list[dict[str, Any]]:
        """按状态列 Issue（state: open / closed / all；含 PR 条目，调用方
        自己按 `pull_request` 键过滤）。全量分页。"""
        params: dict[str, Any] = {"state": state}
        if labels:
            params["labels"] = labels
        return self.paginate(f"/repos/{self.repo}/issues", params)

    def list_open_issues(self, labels: str | None = None) -> list[dict[str, Any]]:
        return self.list_issues(state="open", labels=labels)

    def add_issue_labels(self, number: int, labels: list[str]) -> Any:
        # POST 加标签是幂等的：同标签重复添加不会产生重复标签。
        return self._request(
            "POST",
            f"/repos/{self.repo}/issues/{number}/labels",
            {"labels": list(labels)},
            idempotent=True,
        )

    def remove_issue_label(self, number: int, label: str) -> bool:
        """摘标签；标签本来就不存在（404）视为成功（幂等写法）。"""
        encoded = urllib.parse.quote(label, safe="")
        try:
            self._request(
                "DELETE",
                f"/repos/{self.repo}/issues/{number}/labels/{encoded}",
                idempotent=True,
            )
            return True
        except GitHubAPIError as exc:
            if exc.status in (404, 422):
                return False
            raise

    def get_pull(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self.repo}/pulls/{number}")

    def list_pulls(self, state: str = "open") -> list[dict[str, Any]]:
        """按状态列 PR（state: open / closed / all）。全量分页。

        注意：GitHub 的 /pulls 列表默认只给 open；取 closed/all 时
        含已合入与未合入关闭的 PR，调用方按 `merged` 区分。"""
        return self.paginate(f"/repos/{self.repo}/pulls", {"state": state})

    def list_open_pulls(self) -> list[dict[str, Any]]:
        return self.list_pulls(state="open")

    def get_git_ref(self, ref: str) -> dict[str, Any] | None:
        """按完整 ref（如 refs/heads/x）读取；不存在返回 None。"""
        try:
            return self._request("GET", f"/repos/{self.repo}/git/ref/{ref_param(ref)}")
        except GitHubAPIError as exc:
            if exc.status == 404:
                return None
            raise

    def create_git_ref(self, ref: str, sha: str) -> dict[str, Any]:
        """创建 ref（原子 CAS 入口）。已存在时 GitHub 返回 422。"""
        return self._request(
            "POST",
            f"/repos/{self.repo}/git/refs",
            {"ref": ref, "sha": sha},
            idempotent=False,
        )

    def delete_git_ref(self, ref: str) -> bool:
        try:
            self._request(
                "DELETE", f"/repos/{self.repo}/git/refs/{ref_param(ref)}", idempotent=True
            )
            return True
        except GitHubAPIError as exc:
            if exc.status in (404, 422):
                return False
            raise

    def list_matching_refs(self, ref_prefix: str) -> list[dict[str, Any]]:
        """列出以某前缀开头的全部 ref（`heads/dispatch/...`）。用于分派对账。

        matching-refs 是列表接口，必须分页全量读取：旧实现只取第一页
        （GitHub 默认 per_page=30），调度 ref 一多就静默截断、对账漏项
        （#76）。改走 paginate()：跟随 Link、按 ref 去重、超页数上限
        显式抛 GitHubPaginationError，不拿不完整数据继续。
        """
        return self.paginate(f"/repos/{self.repo}/git/matching-refs/{ref_prefix}")

    def get_branch_sha(self, branch: str = "main") -> str:
        ref = self.get_git_ref(f"refs/heads/{branch}")
        if not ref:
            raise GitHubAPIError(f"读取分支 {branch} 的 ref 失败（不存在）")
        sha = (ref.get("object") or {}).get("sha") or ""
        if not sha:
            raise GitHubAPIError(f"分支 {branch} 的 ref 没有 object.sha")
        return sha

    def search_issues(self, query: str) -> list[dict[str, Any]]:
        """REST Search API 全量结果（items 分页，最多 1000 条由 GitHub 限制）。"""
        return self.paginate(
            "/search/issues",
            {"q": _WHITESPACE.sub(" ", query).strip()},
            extract=lambda body: (body or {}).get("items", []),
        )


class UrllibGitHubClient(GitHubClient):
    """真实 HTTP 实现（标准库 urllib），workflow 里用这个。"""

    def _raw_request(
        self,
        method: str,
        url: str,
        payload: Any,
        headers: Mapping[str, str],
    ) -> tuple[int, dict[str, str], Any]:
        request_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "noc-soc-dispatch/1.0",
        }
        if self.token:
            request_headers["Authorization"] = f"Bearer {self.token}"
        request_headers.update(headers)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                status = response.status
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            status = exc.code
            response_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        except (TimeoutError, socket.timeout) as exc:
            raise GitHubTransportError(f"{method} {url} 超时：{exc}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise GitHubTransportError(f"{method} {url} 超时：{reason}") from exc
            raise GitHubTransportError(f"{method} {url} 网络错误：{reason}") from exc
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"raw": raw}
        else:
            body = None
        return status, response_headers, body


def client_from_env(
    env: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> GitHubClient:
    """按 workflow 环境变量构造客户端（REPO + GH_TOKEN / GITHUB_TOKEN）。"""
    env = os.environ if env is None else env
    repo = env.get("REPO") or env.get("GITHUB_REPOSITORY") or ""
    if not repo:
        raise GitHubError("缺少 REPO 环境变量（形如 owner/name）")
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
    if not token:
        raise GitHubError("缺少 GH_TOKEN / GITHUB_TOKEN")
    return UrllibGitHubClient(repo=repo, token=token, **kwargs)
