"""GitHubIssueAdapter —— 以 GitHub Issues 为任务中心的适配器（TASK-03）。

语义来源：docs/workspace/agent-protocol.md（第一阶段，PR #16 落地）：
- 任务只写在 Issue 里；状态只用标签表示（state:todo → claimed → in-review …）；
- 进展只用评论汇报，评论第一行是机器可读头（HTML 注释）；
- 认领评论固定写法 `<!-- agent-claim: <agent>, <ISO 8601 UTC> -->`；
- 租约：认领后 4 小时无进展（无 push、无评论）即过期，可被重派。

本模块把上面语义收敛成一组可测的 Python 接口：
- Transport 层隔离 GitHub 访问方式：RestGitHubTransport（真实 API）、
  InMemoryGitHubTransport（离线自测 / 仿真）。Worker 与测试只依赖 Transport。
- GitHubIssueAdapter 在 Transport 之上实现注册、心跳、认领、租约、
  执行尝试（attempt）防重复、文件范围锁（重叠串行化）。

时间一律走注入的 clock（返回 aware datetime，UTC），便于离线仿真断网与过期。
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol, Sequence

# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """ISO 8601（UTC，秒精度，Z 后缀）——协议机器可读头的固定时间格式。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: tuple[str, ...] = ()
    state: str = "open"
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class Comment:
    id: int
    author: str
    body: str
    created_at: datetime = field(default_factory=utcnow)


class TransportError(Exception):
    """传输层失败（断网、超时、API 错误）。Worker 据此走断线恢复路径。"""


class LeaseConflict(Exception):
    """认领/续租时租约已被他人持有且未过期。"""


class StaleAttempt(Exception):
    """执行尝试已过期或不属于当前持有者——拒绝回传，防重复执行。"""


class ScopeConflict(Exception):
    """文件范围与另一在途任务重叠，需串行化等待。"""

    def __init__(self, holder_issue: int, holder_agent: str, overlap: Sequence[str]):
        super().__init__(
            f"文件范围与 Issue #{holder_issue}（{holder_agent}）重叠：{', '.join(overlap)}"
        )
        self.holder_issue = holder_issue
        self.holder_agent = holder_agent
        self.overlap = tuple(overlap)


# ---------------------------------------------------------------------------
# Transport 层
# ---------------------------------------------------------------------------


class Transport(Protocol):
    def list_issues(self, labels: Sequence[str] = (), state: str = "open") -> list[Issue]:
        ...

    def get_issue(self, number: int) -> Issue:
        ...

    def list_comments(self, number: int) -> list[Comment]:
        ...

    def add_comment(self, number: int, body: str, author: str = "") -> Comment:
        ...

    def set_labels(self, number: int, labels: Sequence[str]) -> None:
        ...


#: 需要重试的 HTTP 状态：限流（403/429）与瞬时服务端错误（5xx）。
RETRYABLE_HTTP_STATUS = frozenset({403, 429, 500, 502, 503, 504})

#: 只有**幂等**方法才自动重试。POST（发评论）刻意不重试：服务端可能已经应用，
#: 只是客户端超时/连接中断，自动重发会写出第二条评论；这条恢复路径交给 mirror
#: 的 `<!-- completion-id: KEY -->` 标记做「下轮先查再补发」对账。
RETRYABLE_METHODS = frozenset({"GET", "PUT", "DELETE"})

#: 分页单页条数（GitHub 上限 100）。
DEFAULT_PAGE_SIZE = 100

#: 默认最多翻多少页；超过则显式报错，绝不静默返回不完整数据。
DEFAULT_MAX_PAGES = 100


class RestGitHubTransport:
    """真实 GitHub REST API 传输。令牌从参数或环境变量 GITHUB_TOKEN / GH_TOKEN 读。

    注意：仓库约定写操作走浏览器（用户 2026-11-13 定）；本 Transport 是
    Windows Worker 在目标机器上以其自身 GitHub 身份运行时用的通道，
    与 Muse 会话内的连接器写操作是两条独立路径。

    分页（TASK-01）
    ---------------
    `list_issues` / `list_comments` 都是**全量**分页：`per_page=100`，优先按
    `Link: <...>; rel="next"` 翻页；服务端不给 Link 时用「本页满 100 条就继续」
    兜底（page 递增）；空页停止；按 `id`（无 id 时按 `number`）去重。
    任何一页在重试后仍失败 → `TransportError`，**不返回半截列表**。

    重试边界（TASK-01 幂等安全）
    ----------------------------
    只对幂等方法（GET / PUT / DELETE）自动重试 403/429/5xx 与瞬时网络错误；
    **POST 不自动重试**（服务端可能已应用而客户端超时，重发会重复发评论），
    POST 失败直接抛 `TransportError`，由 mirror 的 completion-id 对账路径补发。

    可注入点（测试用假实现走真实分页代码，不 Mock 分页逻辑）：
    `http_open`（默认 `urllib.request.urlopen`）、`sleep`（默认 `time.sleep`）。

    实例身份（TASK-11/12）
    ---------------------
    `instance_id` / `hostname` / `platform` 会被写进注册/心跳评论；不传时
    hostname 取 `socket.gethostname()`、platform 取 `sys.platform`。
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        token: Optional[str] = None,
        api_base: str = "https://api.github.com",
        timeout: float = 30.0,
        *,
        instance_id: str = "",
        hostname: str = "",
        platform: str = "",
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        max_retry_wait: float = 30.0,
        sleep: Optional[Callable[[float], None]] = None,
        http_open: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.instance_id = instance_id
        self.hostname = hostname
        self.platform = platform
        self.page_size = max(1, min(int(page_size), 100))
        self.max_pages = max(1, int(max_pages))
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = max(0.0, float(backoff_base))
        self.max_retry_wait = max(0.0, float(max_retry_wait))
        self._sleep = sleep or time.sleep
        # 默认 None：调用时再取 urllib.request.urlopen，便于测试 monkeypatch 标准库入口
        self.http_open = http_open

    # -- 内部：请求与重试 -----------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.api_base}/repos/{self.owner}/{self.repo}{path}"

    def _build_request(self, method: str, url: str, payload: Optional[dict]) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        return req

    @staticmethod
    def _header_value(headers: Any, name: str) -> Optional[str]:
        if headers is None:
            return None
        try:
            value = headers.get(name)
            if value is None:
                value = headers.get(name.lower())
        except AttributeError:
            return None
        return str(value) if value else None

    def _retry_after(self, headers: Any) -> Optional[float]:
        raw = self._header_value(headers, "Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    def _retry_delay(self, attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_retry_wait)
        return min(self.backoff_base * (2 ** attempt), self.max_retry_wait)

    def _request_raw(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        *,
        http_open: Optional[Callable[..., Any]] = None,
    ) -> tuple[str, Any]:
        """发一次请求（幂等方法含有限次重试），返回 (响应文本, 响应头)。

        `path` 可以是带查询串的相对路径。仅 GET/PUT/DELETE 对 403/429/5xx 与瞬时
        网络错误做指数退避或 Retry-After 重试；POST 只发一次（防重复评论），失败
        直接 TransportError，由 outbox 对账路径处理。重试用尽仍失败一律显式报错，
        调用方据此放弃，而不是拿半截数据。
        """
        url = self._url(path)
        opener = http_open or self.http_open or urllib.request.urlopen
        idempotent = method.upper() in RETRYABLE_METHODS
        attempts = self.max_retries + 1 if idempotent else 1
        for attempt in range(attempts):
            request = self._build_request(method, url, payload)
            try:
                with opener(request, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    headers = getattr(resp, "headers", None)
                return raw, headers
            except urllib.error.HTTPError as exc:
                retry_after = self._retry_after(getattr(exc, "headers", None))
                if exc.code in RETRYABLE_HTTP_STATUS and attempt + 1 < attempts:
                    self._sleep(self._retry_delay(attempt, retry_after))
                    continue
                raise TransportError(
                    f"GitHub API {method} {path} 失败：HTTP {exc.code} {exc.reason}"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - 断网/超时/协议错误统一收敛
                if attempt + 1 < attempts:
                    self._sleep(self._retry_delay(attempt, None))
                    continue
                raise TransportError(f"GitHub API {method} {path} 失败：{exc}") from exc
        raise TransportError(f"GitHub API {method} {path} 失败：重试后仍不可用")

    def _request(self, method: str, path: str, payload: Optional[dict] = None):
        raw, _headers = self._request_raw(method, path, payload)
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError as exc:
            raise TransportError(f"GitHub API {method} {path} 返回非法 JSON") from exc

    # -- 内部：全量分页 -------------------------------------------------------
    @classmethod
    def _has_next_link(cls, headers: Any) -> bool:
        link = cls._header_value(headers, "Link")
        return bool(link and 'rel="next"' in link)

    def _get_json_pages(
        self,
        path: str,
        params: Optional[dict] = None,
        *,
        http_open: Optional[Callable[..., Any]] = None,
        max_pages: Optional[int] = None,
    ) -> list[dict]:
        """按页拉全量列表；任一分页失败都会抛 TransportError（不返回不完整数据）。"""
        limit = self.max_pages if max_pages is None else max(1, int(max_pages))
        base_params = dict(params or {})
        page = 1
        collected: list[dict] = []
        seen: set[Any] = set()
        while True:
            query = dict(base_params)
            query["per_page"] = self.page_size
            query["page"] = page
            separator = "&" if "?" in path else "?"
            raw, headers = self._request_raw(
                "GET",
                f"{path}{separator}{urllib.parse.urlencode(query)}",
                http_open=http_open,
            )
            try:
                items = json.loads(raw) if raw and raw.strip() else []
            except ValueError as exc:
                raise TransportError(f"GitHub API 分页返回非法 JSON：{path} page={page}") from exc
            if not isinstance(items, list):
                raise TransportError(f"GitHub API 分页返回非列表：{path} page={page}")
            if not items:
                break  # 空页 = 结束（也覆盖服务端多给的重复页之后的终止页）
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = item.get("id", item.get("number"))
                if key is not None:
                    if key in seen:
                        continue  # 重复页 / 重复 id 去重
                    seen.add(key)
                collected.append(item)
            if self._has_next_link(headers):
                next_page = page + 1
            elif len(items) >= self.page_size:
                next_page = page + 1  # 服务端没给 Link 时按满页兜底继续
            else:
                break
            if next_page > limit:
                raise TransportError(
                    f"分页超过上限 max_pages={limit}：{path}（可能仍有未读页）"
                )
            page = next_page
        return collected

    # -- 转换 ----------------------------------------------------------------
    @staticmethod
    def _parse_ts(text: str) -> datetime:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    @classmethod
    def _to_issue(cls, raw: dict) -> Issue:
        return Issue(
            number=raw["number"],
            title=raw.get("title", ""),
            body=raw.get("body") or "",
            labels=tuple(l["name"] for l in raw.get("labels", [])),
            state=raw.get("state", "open"),
            created_at=cls._parse_ts(raw["created_at"]),
            updated_at=cls._parse_ts(raw["updated_at"]),
        )

    @classmethod
    def _to_comment(cls, raw: dict, *, fallback_author: str = "") -> Comment:
        return Comment(
            id=raw["id"],
            author=(raw.get("user") or {}).get("login", fallback_author),
            body=raw.get("body") or "",
            created_at=cls._parse_ts(raw["created_at"]),
        )

    # -- Transport 接口 -------------------------------------------------------
    def list_issues(self, labels: Sequence[str] = (), state: str = "open") -> list[Issue]:
        params: dict[str, str] = {"state": state}
        if labels:
            params["labels"] = ",".join(labels)
        rows = self._get_json_pages("/issues", params)
        # /issues 会把 PR 也返回；任务中心只认 Issue
        return [self._to_issue(r) for r in rows if "pull_request" not in r]

    def get_issue(self, number: int) -> Issue:
        return self._to_issue(self._request("GET", f"/issues/{number}"))

    def list_comments(self, number: int) -> list[Comment]:
        rows = self._get_json_pages(f"/issues/{number}/comments")
        return [self._to_comment(r) for r in rows]

    def add_comment(self, number: int, body: str, author: str = "") -> Comment:
        raw = self._request("POST", f"/issues/{number}/comments", {"body": body})
        return self._to_comment(raw, fallback_author=author)

    def set_labels(self, number: int, labels: Sequence[str]) -> None:
        self._request("PUT", f"/issues/{number}/labels", {"labels": list(labels)})


class InMemoryGitHubTransport:
    """内存版传输：与 REST 版语义一致，供离线自测与断网/重派仿真。"""

    def __init__(self, clock: Callable[[], datetime] = utcnow) -> None:
        self.clock = clock
        self._issues: dict[int, Issue] = {}
        self._comments: dict[int, list[Comment]] = {}
        self._next_comment_id = 1

    # 测试辅助：建 Issue
    def create_issue(
        self,
        number: int,
        title: str,
        body: str = "",
        labels: Sequence[str] = (),
        state: str = "open",
    ) -> Issue:
        now = self.clock()
        issue = Issue(
            number=number,
            title=title,
            body=body,
            labels=tuple(labels),
            state=state,
            created_at=now,
            updated_at=now,
        )
        self._issues[number] = issue
        self._comments[number] = []
        return issue

    def list_issues(self, labels: Sequence[str] = (), state: str = "open") -> list[Issue]:
        out = []
        for issue in self._issues.values():
            if issue.state != state:
                continue
            if labels and not set(labels).issubset(set(issue.labels)):
                continue
            out.append(issue)
        return sorted(out, key=lambda i: i.number)

    def get_issue(self, number: int) -> Issue:
        if number not in self._issues:
            raise TransportError(f"Issue #{number} 不存在")
        return self._issues[number]

    def list_comments(self, number: int) -> list[Comment]:
        return list(self._comments.get(number, []))

    def add_comment(self, number: int, body: str, author: str = "") -> Comment:
        if number not in self._issues:
            raise TransportError(f"Issue #{number} 不存在")
        comment = Comment(
            id=self._next_comment_id, author=author, body=body, created_at=self.clock()
        )
        self._next_comment_id += 1
        self._comments[number].append(comment)
        self._issues[number].updated_at = self.clock()
        return comment

    def set_labels(self, number: int, labels: Sequence[str]) -> None:
        issue = self.get_issue(number)
        issue.labels = tuple(labels)
        issue.updated_at = self.clock()


# ---------------------------------------------------------------------------
# 协议消息头
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(
    r"<!--\s*agent-msg:\s*(?P<type>[A-Z_]+),\s*task:\s*#(?P<task>\d+),"
    r"\s*from:\s*(?P<from>[\w-]+)(?:,\s*to:\s*(?P<to>[\w-]+))?,\s*at:\s*(?P<at>[^>]+?)\s*-->"
)
_CLAIM_RE = re.compile(
    r"<!--\s*agent-claim:\s*(?P<agent>[\w-]+),\s*(?P<at>\d{4}-\d{2}-\d{2}T[^>]+?)\s*-->"
)


def msg_header(msg_type: str, task: int, frm: str, at: datetime, to: Optional[str] = None) -> str:
    base = f"<!-- agent-msg: {msg_type}, task: #{task}, from: {frm}"
    if to:
        base += f", to: {to}"
    return base + f", at: {iso(at)} -->"


def parse_header(body: str) -> Optional[dict]:
    m = _HEADER_RE.search(body or "")
    if not m:
        return None
    return {
        "type": m.group("type"),
        "task": int(m.group("task")),
        "from": m.group("from"),
        "to": m.group("to"),
        "at": parse_iso(m.group("at").strip()),
    }


def claim_line(agent: str, at: datetime) -> str:
    return f"<!-- agent-claim: {agent}, {iso(at)} -->"


def parse_file_scope(issue_body: str) -> list[str]:
    """从 Issue 正文「改动文件」一节提取反引号包裹的路径。"""
    files: list[str] = []
    in_section = False
    for line in (issue_body or "").splitlines():
        if line.strip().startswith("##"):
            in_section = "改动文件" in line
            continue
        if in_section:
            for token in re.findall(r"`([^`]+)`", line):
                files.append(token.strip())
    return files


def scopes_overlap(a: Sequence[str], b: Sequence[str]) -> list[str]:
    """判断两组文件范围是否重叠（同路径，或一方是另一方的目录前缀）。"""
    def norm(p: str) -> str:
        return p.replace("\\", "/").strip("/")

    overlap: list[str] = []
    for x in a:
        for y in b:
            nx, ny = norm(x), norm(y)
            if nx == ny or nx.startswith(ny + "/") or ny.startswith(nx + "/"):
                overlap.append(x if len(x) >= len(y) else y)
    return overlap


# ---------------------------------------------------------------------------
# 租约
# ---------------------------------------------------------------------------


@dataclass
class Lease:
    issue: int
    holder: str
    claimed_at: datetime
    last_activity: datetime
    expires_at: datetime
    attempt_id: Optional[str] = None

    def is_live(self, now: datetime) -> bool:
        return now < self.expires_at


# ---------------------------------------------------------------------------
# GitHubIssueAdapter
# ---------------------------------------------------------------------------


class GitHubIssueAdapter:
    """在 Transport 之上实现协议语义：注册 / 心跳 / 认领 / 租约 / 尝试 / 范围锁。"""

    def __init__(
        self,
        transport: Transport,
        agent_id: str,
        clock: Callable[[], datetime] = utcnow,
        lease_ttl: timedelta = timedelta(hours=4),
        registry_issue: Optional[int] = None,
        heartbeat_post_interval: timedelta = timedelta(minutes=10),
        *,
        instance_id: str = "",
        hostname: str = "",
        platform: str = "",
    ) -> None:
        self.transport = transport
        self.agent_id = agent_id
        self.clock = clock
        self.lease_ttl = lease_ttl
        self.registry_issue = registry_issue
        self.heartbeat_post_interval = heartbeat_post_interval
        # 实例身份（TASK-11/12）：agent_id 是逻辑身份，instance_id 才是运行实例身份；
        # 旧调用方不传时保持向后兼容（注册/心跳评论里不写实例行）。
        self.instance_id = instance_id
        self.hostname = hostname
        self.platform = platform
        self._last_heartbeat_post: Optional[datetime] = None
        self._capabilities: tuple[str, ...] = ()

    # -- 实例身份 -------------------------------------------------------------
    def worker_identity(
        self,
        *,
        instance_id: Optional[str] = None,
        hostname: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> dict[str, str]:
        """解析本实例身份：显式参数 > 构造参数 > 本机探测。"""
        return {
            "instance_id": instance_id if instance_id is not None else self.instance_id,
            "hostname": hostname if hostname is not None else (self.hostname or socket.gethostname()),
            "platform": platform if platform is not None else (self.platform or sys.platform),
        }

    @staticmethod
    def _instance_line(identity: dict[str, str]) -> str:
        """实例身份机器可读头；instance_id 为空时不输出（保持旧协议兼容）。"""
        if not identity.get("instance_id"):
            return ""
        return (
            f"<!-- worker-instance: {identity['instance_id']},"
            f" hostname: {identity['hostname']}, platform: {identity['platform']} -->\n"
        )

    # -- 注册与心跳 -----------------------------------------------------------
    def register(
        self,
        capabilities: Sequence[str] = (),
        vm: str = "",
        *,
        instance_id: Optional[str] = None,
        hostname: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> dict:
        """注册：向注册表 Issue 发一条 worker-registry 记录（未配置则只记本地）。"""
        self._capabilities = tuple(capabilities)
        identity = self.worker_identity(
            instance_id=instance_id, hostname=hostname, platform=platform
        )
        now = self.clock()
        info = {
            "agent": self.agent_id,
            "vm": vm,
            "capabilities": list(self._capabilities),
            "registered_at": iso(now),
            **identity,
        }
        if self.registry_issue is not None:
            body = (
                f"<!-- worker-registry: {self.agent_id}, at: {iso(now)} -->\n"
                f"{self._instance_line(identity)}"
                f"Worker 注册：`{self.agent_id}`"
                f"（实例：{identity['instance_id'] or '未注明'}，VM：{vm or '未注明'}，"
                f"主机：{identity['hostname']}，平台：{identity['platform']}）"
                f"能力：{', '.join(self._capabilities) or '未注明'}。"
            )
            self.transport.add_comment(self.registry_issue, body, author=self.agent_id)
            self._last_heartbeat_post = now
        return info

    def heartbeat(
        self,
        note: str = "",
        *,
        instance_id: Optional[str] = None,
        hostname: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> bool:
        """心跳：节流后向注册表 Issue 发心跳记录。返回是否真的发出了评论。"""
        now = self.clock()
        if self.registry_issue is None:
            return False
        if (
            self._last_heartbeat_post is not None
            and now - self._last_heartbeat_post < self.heartbeat_post_interval
        ):
            return False
        identity = self.worker_identity(
            instance_id=instance_id, hostname=hostname, platform=platform
        )
        body = (
            f"<!-- worker-heartbeat: {self.agent_id}, at: {iso(now)} -->\n"
            f"{self._instance_line(identity)}"
            f"Worker 心跳：`{self.agent_id}`"
            f"（实例：{identity['instance_id'] or '未注明'}）在线。{note}"
        )
        self.transport.add_comment(self.registry_issue, body, author=self.agent_id)
        self._last_heartbeat_post = now
        return True

    # -- 租约解析 ---------------------------------------------------------------
    def lease_of(self, issue_number: int) -> Optional[Lease]:
        """从评论流解析当前租约：认领 → 进展活动 → 完成/释放即终止。"""
        comments = self.transport.list_comments(issue_number)
        holder: Optional[str] = None
        claimed_at: Optional[datetime] = None
        last_activity: Optional[datetime] = None
        attempt_id: Optional[str] = None
        for comment in comments:
            body = comment.body or ""
            claim = _CLAIM_RE.search(body)
            if claim:
                holder = claim.group("agent")
                claimed_at = parse_iso(claim.group("at").strip())
                last_activity = claimed_at
                attempt_id = None
                continue
            header = parse_header(body)
            if not header or holder is None or header["from"] != holder:
                continue
            if header["type"] in ("TASK_PROGRESS",):
                last_activity = max(last_activity or header["at"], header["at"])
                m = re.search(r"attempt:\s*([\w-]+)", body)
                if m:
                    attempt_id = m.group(1)
            elif header["type"] in ("TASK_COMPLETED", "TASK_BLOCKED"):
                # 完成即租约终止；阻塞保留租约（等依赖时仍是持有者），但记录活动
                if header["type"] == "TASK_COMPLETED":
                    return None
                last_activity = max(last_activity or header["at"], header["at"])
        if holder is None or claimed_at is None or last_activity is None:
            return None
        return Lease(
            issue=issue_number,
            holder=holder,
            claimed_at=claimed_at,
            last_activity=last_activity,
            expires_at=last_activity + self.lease_ttl,
            attempt_id=attempt_id,
        )

    def list_claimable(self) -> list[Issue]:
        """可认领 = 打了 agent:<我> + state:todo，且不存在未过期租约。"""
        now = self.clock()
        out = []
        for issue in self.transport.list_issues(labels=(f"agent:{self.agent_id}", "state:todo")):
            lease = self.lease_of(issue.number)
            if lease is not None and lease.is_live(now) and lease.holder != self.agent_id:
                continue
            out.append(issue)
        return out

    # -- 认领 / 续租 / 回传 -------------------------------------------------------
    def claim(self, issue_number: int) -> Lease:
        now = self.clock()
        lease = self.lease_of(issue_number)
        if lease is not None and lease.is_live(now) and lease.holder != self.agent_id:
            raise LeaseConflict(
                f"Issue #{issue_number} 已被 {lease.holder} 认领，租约至 {iso(lease.expires_at)}"
            )
        body = (
            f"{claim_line(self.agent_id, now)}\n"
            f"{msg_header('TASK_ACCEPTED', issue_number, self.agent_id, now)}\n"
            f"{self.agent_id} 认领本任务。租约 {int(self.lease_ttl.total_seconds() // 3600)} 小时，"
            "无进展即过期重派。"
        )
        self.transport.add_comment(issue_number, body, author=self.agent_id)
        issue = self.transport.get_issue(issue_number)
        labels = [l for l in issue.labels if l != "state:todo"]
        if "state:claimed" not in labels:
            labels.append("state:claimed")
        self.transport.set_labels(issue_number, labels)
        return Lease(
            issue=issue_number,
            holder=self.agent_id,
            claimed_at=now,
            last_activity=now,
            expires_at=now + self.lease_ttl,
        )

    def _require_holder(self, issue_number: int) -> Lease:
        lease = self.lease_of(issue_number)
        if lease is None or lease.holder != self.agent_id:
            raise LeaseConflict(f"Issue #{issue_number} 当前租约不属于 {self.agent_id}")
        return lease

    def renew(self, issue_number: int, note: str = "租约续租") -> Lease:
        self._require_holder(issue_number)
        now = self.clock()
        body = (
            f"{msg_header('TASK_PROGRESS', issue_number, self.agent_id, now)}\n"
            f"{note}（{self.agent_id}，{iso(now)}）。"
        )
        self.transport.add_comment(issue_number, body, author=self.agent_id)
        lease = self.lease_of(issue_number)
        assert lease is not None  # 刚写入活动，租约必然可解析
        return lease

    def report_progress(self, issue_number: int, text: str, attempt_id: Optional[str] = None) -> None:
        now = self.clock()
        extra = f"\nattempt: {attempt_id}" if attempt_id else ""
        body = f"{msg_header('TASK_PROGRESS', issue_number, self.agent_id, now)}\n{text}{extra}"
        self.transport.add_comment(issue_number, body, author=self.agent_id)

    def report_blocked(self, issue_number: int, text: str) -> None:
        now = self.clock()
        body = f"{msg_header('TASK_BLOCKED', issue_number, self.agent_id, now)}\n{text}"
        self.transport.add_comment(issue_number, body, author=self.agent_id)

    def begin_attempt(self, issue_number: int, attempt_id: str) -> Lease:
        """开始一次执行尝试：必须是当前持有者，且无其他未结束的尝试。"""
        lease = self._require_holder(issue_number)
        now = self.clock()
        if lease.attempt_id is not None and lease.attempt_id != attempt_id and lease.is_live(now):
            raise StaleAttempt(
                f"Issue #{issue_number} 已有进行中的尝试 {lease.attempt_id}，拒绝重复执行"
            )
        self.report_progress(
            issue_number, f"开始执行（尝试 {attempt_id}）。", attempt_id=attempt_id
        )
        lease.attempt_id = attempt_id
        return lease

    def complete(self, issue_number: int, attempt_id: str, summary: str) -> None:
        """回传结果：仅当前持有者、当前尝试可回传；过期尝试一律拒绝（防重复执行）。"""
        lease = self._require_holder(issue_number)
        now = self.clock()
        if not lease.is_live(now):
            raise StaleAttempt(f"Issue #{issue_number} 租约已过期，拒绝回传（防重复执行）")
        if lease.attempt_id is not None and lease.attempt_id != attempt_id:
            raise StaleAttempt(
                f"尝试 {attempt_id} 不是当前尝试 {lease.attempt_id}，拒绝回传"
            )
        body = (
            f"{msg_header('TASK_COMPLETED', issue_number, self.agent_id, now)}\n"
            f"{summary}\nattempt: {attempt_id}"
        )
        self.transport.add_comment(issue_number, body, author=self.agent_id)
        issue = self.transport.get_issue(issue_number)
        labels = [l for l in issue.labels if l != "state:claimed"]
        if "state:in-review" not in labels:
            labels.append("state:in-review")
        self.transport.set_labels(issue_number, labels)

    # -- 文件范围锁（重叠串行化） --------------------------------------------------
    def _scope_lock_of(self, issue_number: int) -> Optional[tuple[str, list[str]]]:
        """解析某 Issue 当前持有的范围锁：返回 (持有者, 文件列表) 或 None。"""
        lease = self.lease_of(issue_number)
        if lease is None:
            return None
        locked: Optional[list[str]] = None
        for comment in self.transport.list_comments(issue_number):
            body = comment.body or ""
            m = re.search(r"scope-lock:\s*(.+)", body)
            if m and (parse_header(body) or {}).get("from") == lease.holder:
                locked = [p.strip() for p in m.group(1).split(",") if p.strip()]
            if "scope-unlock:" in body and (parse_header(body) or {}).get("from") == lease.holder:
                locked = None
        if locked is None:
            return None
        return lease.holder, locked

    def acquire_scope(self, issue_number: int, files: Sequence[str]) -> None:
        """申请文件范围锁：与任一在途任务重叠即 ScopeConflict（串行化等待）。"""
        self._require_holder(issue_number)
        now = self.clock()
        for other in self.transport.list_issues(state="open"):
            if other.number == issue_number:
                continue
            if not ({"state:claimed", "state:in-review"} & set(other.labels)):
                continue
            held = self._scope_lock_of(other.number)
            if held is None:
                continue
            holder, their_files = held
            overlap = scopes_overlap(files, their_files)
            if overlap:
                raise ScopeConflict(other.number, holder, overlap)
        self.report_progress(
            issue_number,
            "已锁定文件范围（重叠任务将串行化）。\nscope-lock: " + ", ".join(files),
        )
        _ = now

    def release_scope(self, issue_number: int) -> None:
        self.report_progress(issue_number, "释放文件范围锁。\nscope-unlock: released")
