"""GitHub git refs 存储：ref-CAS 权威层唯一的网络与原子原语实现（ADR-7）。

本模块只做一件事：把 `ref_api.RefStore` 协议落到 GitHub REST API 上。

安全边界（认领/续租的线性化点就在 `create`）
--------------------------------------------
- `POST /repos/{o}/{r}/git/refs` 由 GitHub **服务端**裁决 ref 名唯一性：
  201 = 本次创建成功（全局唯一赢家）；422 = 已存在（失败者）。
- **POST 绝不盲目重试**：服务端可能已经创建成功、只是客户端超时/连接中断，
  重发会得到 422，把「自己赢了」误判成「输了」，甚至产生两条租约。
  因此 POST 只发一次；传输异常时**先 GET 消歧**：
  - GET 说 ref 已存在 → 按失败者返回 False（安全侧：宁可少认领一次，
    也不产生两个赢家）；
  - GET 说 ref 不存在 → 结果确定为「没创建成功」，抛 `GitHubRefError`，
    由上层（claim 的失败者重读路径）决定下一步。
- 422 同样先用 GET 消歧：ref 存在 = 已存在（False）；ref 不存在 = 真正错误
  （典型是锚点 SHA 无效），**显式报错**，不得静默当成「已登记」。
  这与 dispatch_registry.py（PR #52）的既有约定一致。
- GET / DELETE 是幂等的，可对 5xx / 429 / 限流 403 做**有限次**指数退避重试；
  重试用尽显式报错，绝不返回半截数据。

可注入点（测试用本地 fake GitHub HTTP server 走真实网络路径，不 mock 逻辑）：
`http_open`（默认 `urllib.request.urlopen`）、`sleep`（默认 `time.sleep`）。

ref 参数形态
------------
- `create` 收完整 ref（`refs/heads/...`）；
- `get` / `delete` 收完整 ref，内部转成 GitHub 单 ref 接口的 `heads/...` 形式；
- `list` 收前缀（`refs/heads/agent-hwm/issue-1/`），内部转 `matching-refs/heads/...`
  并跟随 `Link` 分页、按 ref 名去重。前缀语义：matching-refs 返回**以该串开头**的
  所有 ref，因此前缀末尾的 `/` 很重要（避免 `issue-1` 命中 `issue-10`）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from .ref_api import RefInfo

#: GitHub REST 根地址（测试注入本地 fake server 的 http://127.0.0.1:<port>）。
DEFAULT_API_BASE = "https://api.github.com"

#: 单页条数（GitHub 上限 100）。
DEFAULT_PAGE_SIZE = 100

#: 分页页数上限；超过显式报错，不静默截断（ref 列表不完整会破坏 fencing 判定）。
DEFAULT_MAX_PAGES = 100

#: 可重试的 HTTP 状态：限流（429 / 限流 403）与瞬时服务端错误（5xx）。
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: 重试等待上限（秒），避免 Retry-After 极大的响应把进程拖死。
MAX_RETRY_WAIT = 30.0


class GitHubRefError(Exception):
    """refs 操作失败。`status` 为 HTTP 状态码（传输异常时为 None）。"""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        method: str = "",
        path: str = "",
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path
        self.body = body


def _ref_param(ref: str) -> str:
    """单 ref 接口路径参数：`refs/heads/x` → `heads/x`（GitHub 文档口径）。"""
    return ref[len("refs/"):] if ref.startswith("refs/") else ref


def _matching_param(prefix: str) -> str:
    """matching-refs 路径参数：`refs/heads/agent-hwm/issue-1/` → `heads/agent-hwm/issue-1`。

    GitHub 的 matching-refs 是**字符串前缀**语义（`issue-1` 也会命中 `issue-10`），
    而 ref 命名以 `/` 分段。服务端只负责圈出候选集，调用方前缀里末尾的 `/`
    由 `list()` 在本地再过滤一次，保证 `issue-1` 不会吃到 `issue-10` 的 ref。
    """
    text = prefix[len("refs/"):] if prefix.startswith("refs/") else prefix
    text = text.rstrip("/")
    if not text.startswith(("heads/", "tags/")):
        text = "heads/" + text
    return text


def _full_prefix(prefix: str) -> str:
    """把 list 前缀统一成完整 ref 前缀（用于本地段边界过滤，保留末尾 `/`）。"""
    if prefix.startswith("refs/"):
        return prefix
    if prefix.startswith(("heads/", "tags/")):
        return "refs/" + prefix
    return "refs/heads/" + prefix


def _has_next_link(headers: dict[str, str]) -> bool:
    link = headers.get("link") or headers.get("Link") or ""
    return 'rel="next"' in link or "rel=next" in link


class RestRefStore:
    """GitHub git refs 的 `ref_api.RefStore` 实现（真实 REST + 有限重试 + 消歧）。"""

    def __init__(
        self,
        repo: str,
        *,
        token: Optional[str] = None,
        api_base: str = DEFAULT_API_BASE,
        http_open: Optional[Callable[..., Any]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        timeout: float = 20.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        owner, _, name = str(repo or "").partition("/")
        if not owner or not name:
            raise ValueError(f"repo 必须是 owner/name 形式，实际：{repo!r}")
        self.owner = owner
        self.repo = name
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        self.api_base = str(api_base or DEFAULT_API_BASE).rstrip("/")
        self.http_open = http_open
        self._sleep = sleep or time.sleep
        self.timeout = float(timeout)
        self.page_size = max(1, min(int(page_size), 100))
        self.max_pages = max(1, int(max_pages))
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = max(0.0, float(backoff_base))
        #: 便于测试/排障断言「发了几次请求」；元素为 (METHOD, 相对 path)。
        self.requests: list[tuple[str, str]] = []

    # -- HTTP 基础 -----------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.api_base}/repos/{self.owner}/{self.repo}{path}"

    def _build_request(self, method: str, url: str, payload: Optional[dict]) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "noc-soc-ref-authority/1.0")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        return request

    @staticmethod
    def _headers_of(source: Any) -> dict[str, str]:
        try:
            items = source.items() if hasattr(source, "items") else []
        except Exception:  # noqa: BLE001 - 第三方响应对象形态不一
            return {}
        return {str(k).lower(): str(v) for k, v in items}

    def _delay(self, attempt: int, headers: dict[str, str]) -> float:
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                return min(MAX_RETRY_WAIT, max(0.0, float(retry_after)))
            except (TypeError, ValueError):
                pass
        return min(MAX_RETRY_WAIT, self.backoff_base * (2 ** attempt))

    def _open(self, request: Any) -> Any:
        opener = self.http_open or urllib.request.urlopen
        return opener(request, timeout=self.timeout)

    def _raw(
        self, method: str, path: str, payload: Optional[dict] = None, *, retryable: bool
    ) -> tuple[int, dict[str, str], bytes]:
        """发一次请求并返回 `(status, headers, raw)`；非 2xx 交由调用方做语义映射。

        可重试状态（429/5xx/限流）只在 `retryable=True` 时有限次重试；传输异常同样
        只在可重试时重试，耗尽后抛 `GitHubRefError`（POST 不重试，见模块说明）。
        """
        url = self._url(path)
        attempts = self.max_retries + 1 if retryable else 1
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            self.requests.append((method, url))
            request = self._build_request(method, url, payload)
            try:
                with self._open(request) as response:
                    status = int(getattr(response, "status", None) or getattr(response, "code", 200))
                    headers = self._headers_of(getattr(response, "headers", None))
                    return status, headers, response.read()
            except urllib.error.HTTPError as exc:
                headers = self._headers_of(getattr(exc, "headers", None))
                raw = exc.read() if hasattr(exc, "read") else b""
                retry_it = status_in(retryable, exc.code) and attempt + 1 < attempts
                if retry_it:
                    self._sleep(self._delay(attempt, headers))
                    continue
                return int(exc.code), headers, raw
            except Exception as exc:  # noqa: BLE001 - 断网/超时/连接重置统一收敛
                last_error = exc
                if retryable and attempt + 1 < attempts:
                    self._sleep(self._delay(attempt, {}))
                    continue
                break
        raise GitHubRefError(
            f"GitHub refs 请求 {method} {path} 传输失败：{last_error!r}",
            method=method,
            path=path,
        )

    @staticmethod
    def _json(raw: bytes, method: str, path: str) -> Any:
        if not raw or not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise GitHubRefError(
                f"GitHub refs {method} {path} 返回非法 JSON", method=method, path=path
            ) from exc

    @staticmethod
    def _error(status: int, method: str, path: str, raw: bytes) -> GitHubRefError:
        detail = raw.decode("utf-8", "replace")[:200] if raw else ""
        return GitHubRefError(
            f"GitHub refs {method} {path} 失败：HTTP {status} {detail}".strip(),
            status=status,
            method=method,
            path=path,
        )

    # -- RefStore 协议：创建（唯一线性化点） -----------------------------------
    def create(self, ref: str, sha: str) -> bool:
        """创建 ref：True=本次创建成功；False=已存在（唯一赢家已另有其人）。

        POST 不重试；传输异常先 GET 消歧：已存在按 False（安全侧），否则报错。
        422 也先 GET 消歧：不存在说明是真实错误（如锚点 SHA 无效），显式抛错。
        """
        path = "/git/refs"
        payload = {"ref": ref, "sha": sha}
        try:
            status, _headers, raw = self._raw("POST", path, payload, retryable=False)
        except GitHubRefError as exc:
            # 结果未知：GET 消歧。存在 → 本次确实建成了（或别人先建），按失败者处理。
            if self.get(ref) is not None:
                return False
            raise GitHubRefError(
                f"创建 ref {ref} 传输失败且读回不存在（按未创建处理）：{exc}",
                method="POST",
                path=path,
            ) from exc
        if 200 <= status < 300:
            return True
        if status in (409, 422):
            if self.get(ref) is not None:
                return False
            raise GitHubRefError(
                f"创建 ref {ref} 返回 HTTP {status}，但 ref 不存在"
                f"（可能是锚点 SHA {sha[:12]} 无效），不能当作已存在继续",
                status=status,
                method="POST",
                path=path,
            )
        raise self._error(status, "POST", path, raw)

    # -- RefStore 协议：读取 --------------------------------------------------
    def get(self, ref: str) -> Optional[RefInfo]:
        path = f"/git/ref/{_ref_param(ref)}"
        status, _headers, raw = self._raw("GET", path, retryable=True)
        if status == 404:
            return None
        if not 200 <= status < 300:
            raise self._error(status, "GET", path, raw)
        data = self._json(raw, "GET", path) or {}
        obj = data.get("object") or {}
        return RefInfo(
            ref=data.get("ref") or ref,
            sha=obj.get("sha") or "",
            object_type=obj.get("type") or "commit",
        )

    def list(self, prefix: str) -> list[RefInfo]:
        """列出前缀下全部 ref（跟随 Link 分页、按 ref 名去重、空页停止）。

        服务端 matching-refs 是字符串前缀语义，本地再按完整前缀过滤一次：
        调用方传 `refs/heads/agent-hwm/issue-1/` 时不会命中 `issue-10` 的 ref。
        """
        base = f"/git/matching-refs/{_matching_param(prefix)}"
        boundary = _full_prefix(prefix)
        collected: list[RefInfo] = []
        seen: set[str] = set()
        page = 1
        while page <= self.max_pages:
            separator = "&" if "?" in base else "?"
            path = f"{base}{separator}per_page={self.page_size}&page={page}"
            status, headers, raw = self._raw("GET", path, retryable=True)
            if status == 404:
                return collected
            if not 200 <= status < 300:
                raise self._error(status, "GET", path, raw)
            items = self._json(raw, "GET", path)
            if not isinstance(items, list):
                raise GitHubRefError(
                    f"GitHub refs matching-refs 返回非列表：{path}", method="GET", path=path
                )
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("ref") or ""
                if not name or name in seen or not name.startswith(boundary):
                    continue
                seen.add(name)
                obj = item.get("object") or {}
                collected.append(
                    RefInfo(
                        ref=name,
                        sha=obj.get("sha") or "",
                        object_type=obj.get("type") or "commit",
                    )
                )
            if _has_next_link(headers) or len(items) >= self.page_size:
                page += 1
                continue
            break
        if page > self.max_pages:
            raise GitHubRefError(
                f"refs 分页超过上限 max_pages={self.max_pages}：{base}"
                "（可能仍有未读页，拒绝用不完整数据做 fencing 判定）",
                method="GET",
                path=base,
            )
        return collected

    # -- RefStore 协议：删除 --------------------------------------------------
    def delete(self, ref: str) -> bool:
        """删除 ref；404/422 幂等返回 False。传输异常先 GET 消歧。"""
        path = f"/git/refs/{_ref_param(ref)}"
        try:
            status, _headers, raw = self._raw("DELETE", path, retryable=True)
        except GitHubRefError as exc:
            if self.get(ref) is None:
                return True  # 已经不存在：幂等成功
            raise exc
        if 200 <= status < 300:
            return True
        if status in (404, 422):
            return False
        raise self._error(status, "DELETE", path, raw)

    # -- RefStore 协议：锚点 SHA ---------------------------------------------
    def branch_sha(self, name: str = "main") -> str:
        """取分支头 SHA（创建 ref 必须指向一个已存在的 git 对象）。"""
        path = f"/git/ref/heads/{name}"
        status, _headers, raw = self._raw("GET", path, retryable=True)
        if status == 404:
            raise GitHubRefError(
                f"分支 {name} 不存在（创建 ref 需要锚点 SHA）", status=404, method="GET", path=path
            )
        if not 200 <= status < 300:
            raise self._error(status, "GET", path, raw)
        data = self._json(raw, "GET", path) or {}
        sha = str((data.get("object") or {}).get("sha") or "")
        if not sha:
            raise GitHubRefError(
                f"分支 {name} 的 ref 没有 object.sha", status=status, method="GET", path=path
            )
        return sha


def status_in(retryable: bool, status: int) -> bool:
    """某状态码在当前请求是否可重试（限流 403 也算可重试）。"""
    return retryable and (int(status) in RETRYABLE_STATUS or int(status) == 403)
