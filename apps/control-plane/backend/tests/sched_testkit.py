"""控制面测试工具箱：假时钟、假 GitHub 传输、分页 opener、任务构造 helper。

只依赖标准库；不 Mock 掉被测的原子性（SQLite 是真的、线程/进程是真的），
这里替代的只有「外部的 GitHub HTTP 与时间」。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from scheduler import SchedulerService

T0 = datetime(2026, 11, 14, 2, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """可推进的假时钟（aware UTC）。"""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# 任务 / 认领 helper
# ---------------------------------------------------------------------------


def claim(
    svc: SchedulerService,
    task_id: str = "issue-1",
    *,
    agent: str = "muse-2",
    instance: str = "i-1",
    attempt: str = "a-1",
    lease_ttl: Optional[timedelta] = None,
):
    return svc.claim(
        task_id,
        agent_id=agent,
        instance_id=instance,
        attempt_id=attempt,
        lease_ttl=lease_ttl,
    )


def prepare_running(
    svc: SchedulerService,
    task_id: str = "issue-1",
    *,
    issue_number: int = 1,
    agent: str = "muse-2",
    instance: str = "i-1",
    attempt: str = "a-1",
    scopes: Sequence[str] = (),
):
    """upsert → claim → transition(running)，返回租约。"""
    svc.upsert_task(task_id, issue_number, repo="o/r", title="任务", scopes=scopes)
    lease = claim(svc, task_id, agent=agent, instance=instance, attempt=attempt)
    svc.transition(
        task_id,
        attempt_id=attempt,
        fencing_token=lease.fencing_token,
        state="running",
    )
    return lease


# ---------------------------------------------------------------------------
# 记录型 GitHub 传输（mirror 测试用；可注入失败）
# ---------------------------------------------------------------------------


class FakeTransportError(Exception):
    """模拟 GitHub 写失败。"""


@dataclass
class FakeComment:
    id: int
    body: str
    author: str = "muse-2"


@dataclass
class FakeIssue:
    number: int
    labels: tuple[str, ...] = ()
    body: str = ""
    state: str = "open"


class RecordingTransport:
    """内存 GitHub：记录评论/标签写操作，可按次数注入失败。"""

    def __init__(self, *, labels: Sequence[str] = ("agent:muse-2", "state:claimed")) -> None:
        self.issues: dict[int, FakeIssue] = {}
        self.comments: dict[int, list[FakeComment]] = {}
        self.fail_comment_times = 0
        self.fail_labels_times = 0
        self.add_comment_calls = 0
        self.set_labels_calls = 0
        self.default_labels = tuple(labels)
        self.next_comment_id = 1

    def add_issue(self, number: int, *, labels: Optional[Sequence[str]] = None) -> FakeIssue:
        issue = FakeIssue(
            number=number,
            labels=tuple(self.default_labels if labels is None else labels),
        )
        self.issues[number] = issue
        self.comments.setdefault(number, [])
        return issue

    # -- Transport 协议 ------------------------------------------------------
    def list_comments(self, number: int) -> list[FakeComment]:
        return list(self.comments.get(number, []))

    def add_comment(self, number: int, body: str, author: str = "") -> FakeComment:
        self.add_comment_calls += 1
        if self.fail_comment_times > 0:
            self.fail_comment_times -= 1
            raise FakeTransportError("模拟发评论失败")
        comment = FakeComment(self.next_comment_id, body, author or "muse-2")
        self.next_comment_id += 1
        self.comments.setdefault(number, []).append(comment)
        return comment

    def get_issue(self, number: int) -> FakeIssue:
        if number not in self.issues:
            raise FakeTransportError(f"Issue #{number} 不存在")
        return self.issues[number]

    def set_labels(self, number: int, labels: Sequence[str]) -> None:
        self.set_labels_calls += 1
        if self.fail_labels_times > 0:
            self.fail_labels_times -= 1
            raise FakeTransportError("模拟改标签失败")
        self.get_issue(number).labels = tuple(labels)

    # -- 断言辅助 ------------------------------------------------------------
    def comment_bodies(self, number: int) -> list[str]:
        return [c.body for c in self.comments.get(number, [])]

    def has_marker(self, number: int, marker: str) -> bool:
        return any(marker in body for body in self.comment_bodies(number))


# ---------------------------------------------------------------------------
# RestGitHubTransport 的假 opener：走真实分页代码，只替换 HTTP 字节
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: Any, headers: Optional[dict[str, str]] = None) -> None:
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = dict(headers or {})

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class FakeGitHub:
    """按「URL 路径 + page」返回预设页；可注入 HTTP 失败。

    pages: ``{url_path: [第1页, 第2页, ...]}``；页 list 中每个元素是 GitHub JSON 条目。
    fail: ``{(url_path, page): 失败}`` 或 ``{url_path: 失败}``，失败可以是：
    - ``int``：该 HTTP 状态码（如 503）；
    - ``Exception``：直接抛出；
    - ``{"status": 429, "retry_after": "2"}``：带 Retry-After 的状态码；
    - ``list``：按第 N 次调用依次取用（重试/恢复场景）；
    - ``callable``：``fn(调用序号) -> 失败或 None``。
    返回的响应按「还有下一页」自动带 Link 头。
    """

    def __init__(
        self,
        pages: dict[str, list[list[dict]]],
        fail: Optional[dict[Any, Any]] = None,
    ) -> None:
        self.pages = pages
        self.fail = fail or {}
        self.calls: list[dict[str, Any]] = []
        self._fail_counts: dict[str, int] = {}

    def _pick_failure(self, path: str, page: int) -> Any:
        configured = self.fail.get((path, page), self.fail.get(path))
        index = self._fail_counts.get(path, 0)
        self._fail_counts[path] = index + 1
        if callable(configured):
            return configured(index)
        if isinstance(configured, list):
            return configured[index] if index < len(configured) else None
        return configured

    def __call__(self, request, timeout=None):  # noqa: ANN001 - 模拟 urlopen
        url = urllib.parse.urlparse(request.full_url)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        page = int(query.get("page", "1"))
        method = request.get_method() if hasattr(request, "get_method") else "GET"
        self.calls.append(
            {"path": url.path, "page": page, "query": query, "method": method}
        )
        failure = self._pick_failure(url.path, page)
        if failure is not None:
            if isinstance(failure, Exception):
                raise failure
            retry_after = "0"
            if isinstance(failure, dict):
                status = int(failure.get("status", 500))
                retry_after = str(failure.get("retry_after", "0"))
            else:
                status = int(failure)
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                "injected",
                {"Retry-After": retry_after},
                None,
            )
        pages = self.pages.get(url.path, [])
        payload = pages[page - 1] if page - 1 < len(pages) else []
        headers: dict[str, str] = {}
        if payload and page < len(pages):
            headers["Link"] = (
                f'<{self._link(request.full_url, page + 1)}>; rel="next"'
            )
        return FakeResponse(payload, headers)

    @staticmethod
    def _link(url: str, page: int) -> str:
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        query["page"] = str(page)
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


# ---------------------------------------------------------------------------
# GitHub JSON 数据构造
# ---------------------------------------------------------------------------

_TS = "2026-11-14T02:00:00Z"


def issue_json(
    number: int,
    *,
    title: str = "任务",
    body: str = "",
    labels: Sequence[str] = ("agent:muse-2", "state:todo"),
    state: str = "open",
) -> dict:
    return {
        "number": number,
        "id": 900000 + number,
        "title": title,
        "body": body,
        "labels": [{"name": name} for name in labels],
        "state": state,
        "created_at": _TS,
        "updated_at": _TS,
    }


def comment_json(
    number: int, body: str = "", *, login: str = "muse-2"
) -> dict:
    return {
        "id": 700000 + number,
        "user": {"login": login},
        "body": body,
        "created_at": _TS,
    }


def chunks(items: list[dict], size: int = 100) -> list[list[dict]]:
    """把条目切成每页 size 条（模拟 GitHub per_page=100 的返回）。"""
    return [items[index : index + size] for index in range(0, len(items), size)]
