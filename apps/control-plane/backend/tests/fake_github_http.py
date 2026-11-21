"""本地 fake GitHub HTTP 服务（测试专用；标准库 http.server，无真实网络）。

为什么是**真 HTTP server** 而不是内存假实现：ref-CAS 的安全性完全依赖 GitHub
服务端的原子唯一性。要让测试有意义，必须让并发（多线程 / 两个真实子进程）
真的走 TCP 打到同一个「服务端」，由服务端在锁内裁决 ref 唯一性、返回 422、
分页返回、注入故障。这样断言的 `refs()` 与请求计数才是真实行为。

接口：`POST /git/refs`（唯一性 422）、`GET /git/ref/{heads/...}`（404）、
`GET /git/matching-refs/{heads/...}`（Link 分页）、`DELETE /git/refs/{heads/...}`、
Issue 读写 + 评论（GET/POST 分页）+ 标签（PUT）。

故障注入（处理前统一拦截）：
- `fail_next(path, status=500, times=1, page=None, method=None)`：命中返回该状态码；
- `fail_next(path, transport_abort=True, apply_first=True)`：先真实应用请求再断开
  TCP（客户端看到传输异常）——用于验证「POST 结果未知时先 GET 消歧、按失败者处理」。

所有状态访问都在一把 `threading.RLock` 内完成：并发创建同一 ref 时，服务端
只会让一个请求拿到 201，其余拿到 422（与真实 GitHub 的裁决语义一致）。
"""

from __future__ import annotations

import json
import re
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, Sequence

DEFAULT_ANCHOR_SHA = "a" * 40
_TS = "2026-11-14T02:00:00Z"

_REF_PATH_RE = re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/ref/(?P<ref>.+)$")
_MATCHING_RE = re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/matching-refs/?(?P<prefix>.*)$")
_GIT_REFS_RE = re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/git/refs/?(?P<ref>.*)$")
_ISSUE_RE = re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)$")
_COMMENTS_RE = re.compile(
    r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/comments$"
)
_LABELS_RE = re.compile(
    r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/labels$"
)
_ISSUES_RE = re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues$")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeGitHubServer:
    """线程安全的本地 fake GitHub；`with FakeGitHubServer() as srv:` 自动起停。"""

    def __init__(
        self,
        owner: str = "o",
        repo: str = "r",
        *,
        anchor_sha: str = DEFAULT_ANCHOR_SHA,
        page_size: int = 100,
        page_size_override: Optional[int] = None,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.anchor_sha = anchor_sha
        self.default_page_size = max(1, int(page_size))
        #: 若设置则忽略客户端 per_page，强制按该值分页（便于小数据量测分页）。
        self.page_size_override = page_size_override
        self.lock = threading.RLock()
        self.refs: dict[str, str] = {f"refs/heads/main": anchor_sha}
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self._next_comment_id = 1
        #: 请求计数：key 为 ("METHOD", 归一化路径)。
        self.counters: dict[tuple[str, str], int] = {}
        #: 每个 ref 的创建尝试次数（断言「失败者只尝试一次 POST」等）。
        self.create_attempts: dict[str, int] = {}
        #: 分页请求明细：(归一化路径, page) 次数。
        self.page_calls: dict[tuple[str, int], int] = {}
        #: 故障规则队列（见 fail_next）。
        self.faults: list[dict[str, Any]] = []
        self._httpd: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    # -- 生命周期 ------------------------------------------------------------
    def start(self) -> "FakeGitHubServer":
        server = self

        class Handler(_Handler):
            fake = server

        self._httpd = _Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="fake-github", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def __enter__(self) -> "FakeGitHubServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("fake GitHub 尚未启动")
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def repo_full(self) -> str:
        return f"{self.owner}/{self.repo}"

    # -- 测试侧状态构造 / 断言 ----------------------------------------------
    def add_issue(
        self,
        number: int,
        *,
        labels: Sequence[str] = (),
        title: str = "任务",
        body: str = "",
        state: str = "open",
    ) -> dict[str, Any]:
        with self.lock:
            issue = {
                "number": int(number),
                "id": 900000 + int(number),
                "title": title,
                "body": body,
                "labels": [{"name": name} for name in labels],
                "state": state,
                "created_at": _TS,
                "updated_at": _TS,
            }
            self.issues[int(number)] = issue
            self.comments.setdefault(int(number), [])
            return issue

    def add_comment(self, number: int, body: str, author: str = "muse-2") -> dict[str, Any]:
        with self.lock:
            if int(number) not in self.issues:
                raise KeyError(f"Issue #{number} 不存在")
            comment = {
                "id": self._next_comment_id,
                "user": {"login": author},
                "body": body,
                "created_at": _TS,
            }
            self._next_comment_id += 1
            self.comments[int(number)].append(comment)
            return comment

    def set_labels(self, number: int, labels: Sequence[str]) -> None:
        with self.lock:
            self.issues[int(number)]["labels"] = [{"name": name} for name in labels]

    def issue_labels(self, number: int) -> tuple[str, ...]:
        with self.lock:
            return tuple(label["name"] for label in self.issues[int(number)]["labels"])

    def comment_bodies(self, number: int) -> list[str]:
        with self.lock:
            return [comment["body"] for comment in self.comments.get(int(number), [])]

    def seed_ref(self, ref: str, sha: Optional[str] = None) -> None:
        """直接写入一条 ref（测试准备数据，不经过 HTTP、不计创建次数）。"""
        with self.lock:
            self.refs[ref] = sha or self.anchor_sha

    def refs_snapshot(self) -> dict[str, str]:
        with self.lock:
            return dict(self.refs)

    def stats(self) -> dict[str, Any]:
        with self.lock:
            return {
                "total": sum(self.counters.values()),
                "by_method_path": dict(self.counters),
                "create_attempts": dict(self.create_attempts),
                "page_calls": dict(self.page_calls),
                "faults_left": len(self.faults),
            }

    def create_attempts_of(self, ref: str) -> int:
        with self.lock:
            return int(self.create_attempts.get(ref, 0))

    def page_calls_of(self, path_substr: str, page: int) -> int:
        with self.lock:
            return sum(
                count
                for (path, called_page), count in self.page_calls.items()
                if path_substr in path and called_page == page
            )

    def reset_stats(self) -> None:
        with self.lock:
            self.counters.clear()
            self.create_attempts.clear()
            self.page_calls.clear()

    # -- 故障注入 ------------------------------------------------------------
    def fail_next(
        self,
        path_substr: str,
        *,
        status: Optional[int] = 500,
        times: int = 1,
        page: Optional[int] = None,
        method: Optional[str] = None,
        transport_abort: bool = False,
        apply_first: bool = False,
    ) -> None:
        """注入故障：命中 `path_substr`（可再限定 method/page）的下一批请求。

        - `status`：返回该 HTTP 状态（None 表示不返回状态、只看 transport_abort）；
        - `transport_abort`：直接断开连接（客户端看到传输异常）；
        - `apply_first`：先真实处理并应用状态变更，再断连（模拟「服务端已生效、
          客户端超时」的 POST 场景）。
        """
        with self.lock:
            self.faults.append(
                {
                    "path": str(path_substr),
                    "status": None if status is None else int(status),
                    "times": max(1, int(times)),
                    "page": None if page is None else int(page),
                    "method": method.upper() if method else None,
                    "transport_abort": bool(transport_abort),
                    "apply_first": bool(apply_first),
                }
            )

    def _take_fault(self, method: str, path: str, page: int) -> Optional[dict[str, Any]]:
        with self.lock:
            for rule in self.faults:
                if rule["times"] <= 0 or rule["path"] not in path:
                    continue
                if rule["method"] and rule["method"] != method:
                    continue
                if rule["page"] is not None and rule["page"] != page:
                    continue
                rule["times"] -= 1
                return dict(rule)
        return None

    # -- 供 Handler 使用的原子状态操作 --------------------------------------
    def _list_refs(self, prefix: str) -> list[str]:
        with self.lock:
            return sorted(ref for ref in self.refs if ref.startswith(prefix))

    def _add_ref(self, ref: str, sha: str) -> bool:
        with self.lock:
            if ref in self.refs:
                return False
            self.refs[ref] = sha
            return True

    def _delete_ref(self, ref: str) -> bool:
        with self.lock:
            return self.refs.pop(ref, None) is not None

    def _count(self, method: str, path: str, page: int) -> None:
        with self.lock:
            key = (method, _path_template(path))
            self.counters[key] = self.counters.get(key, 0) + 1
            if method == "GET":
                self.page_calls[(key[1], page)] = self.page_calls.get((key[1], page), 0) + 1


def _path_template(path: str) -> str:
    """把具体编号/ref 路径归一成模板，便于计数断言。"""
    for pattern, name in (
        (_COMMENTS_RE, "comments"),
        (_LABELS_RE, "labels"),
        (_ISSUE_RE, "issue"),
        (_ISSUES_RE, "issues"),
    ):
        if pattern.match(path):
            return f"issues/{name}"
    if _REF_PATH_RE.match(path):
        return "git/ref"
    if _MATCHING_RE.match(path):
        return "git/matching-refs"
    if _GIT_REFS_RE.match(path):
        return "git/refs"
    return path


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fake: FakeGitHubServer
    #: 本次请求已读取的 JSON 请求体（故障注入不进入业务处理时也要先读掉，
    #: 否则未读 body 会污染 keep-alive 连接；同时供 create 计数与业务共用）。
    _body: Optional[dict[str, Any]] = None

    # -- HTTP 入口 -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # 静默：测试输出不刷 HTTP 日志

    # -- 统一处理 ------------------------------------------------------------
    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        page = max(1, int(query.get("page", "1") or 1))
        # 先读完请求体：故障注入可能不进入业务处理，未读的 body 会污染连接
        self._body = self._read_json() if method in ("POST", "PUT") else None
        self.fake._count(method, path, page)
        # 创建 ref 的尝试次数按「请求到达」计数（即使被故障注入拦截也算一次 POST）
        create_match = _GIT_REFS_RE.match(path)
        if method == "POST" and create_match and not create_match.group("ref"):
            ref = str((self._body or {}).get("ref") or "")
            if ref:
                with self.fake.lock:
                    self.fake.create_attempts[ref] = self.fake.create_attempts.get(ref, 0) + 1
        fault = self.fake._take_fault(method, path, page)
        if fault is not None and fault["transport_abort"]:
            if fault["apply_first"]:
                self._dispatch(method, path, query, page, write=False)
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        if fault is not None and fault["status"] is not None:
            self._send_json(
                fault["status"], {"message": "injected failure", "status": fault["status"]}
            )
            return
        self._dispatch(method, path, query, page, write=True)

    def _dispatch(
        self, method: str, path: str, query: dict[str, str], page: int, *, write: bool
    ) -> None:
        match = _REF_PATH_RE.match(path)
        if method == "GET" and match:
            ref = match.group("ref")
            full = "refs/" + ref if not ref.startswith("refs/") else ref
            with self.fake.lock:
                sha = self.fake.refs.get(full)
            if sha is None:
                self._not_found(write)
                return
            self._send_json(
                200,
                {"ref": full, "object": {"sha": sha, "type": "commit"}},
                write=write,
            )
            return

        match = _MATCHING_RE.match(path)
        if method == "GET" and match:
            prefix = "refs/" + match.group("prefix")
            names = self.fake._list_refs(prefix)
            self._send_paged(
                [{"ref": name, "object": {"sha": self.fake.refs[name], "type": "commit"}}
                 for name in names],
                query,
                page,
                write=write,
            )
            return

        match = _GIT_REFS_RE.match(path)
        if match:
            ref = match.group("ref")
            full = "refs/" + ref if not ref.startswith("refs/") else ref
            if method == "DELETE":
                removed = self.fake._delete_ref(full)
                if removed:
                    self._send_empty(204, write)
                else:
                    self._send_json(422, {"message": "Reference does not exist"}, write=write)
                return
            if method == "POST" and not ref:
                self._create_ref(write)
                return

        match = _COMMENTS_RE.match(path)
        if match:
            number = int(match.group("number"))
            with self.fake.lock:
                known = number in self.fake.issues
            if not known:
                self._not_found(write)
                return
            if method == "GET":
                with self.fake.lock:
                    items = [dict(c) for c in self.fake.comments.get(number, [])]
                self._send_paged(items, query, page, write=write)
                return
            if method == "POST":
                payload = self._body or {}
                with self.fake.lock:
                    if number not in self.fake.issues:
                        self._not_found(write)
                        return
                    comment = {
                        "id": self.fake._next_comment_id,
                        "user": {"login": "muse-2"},
                        "body": payload.get("body") or "",
                        "created_at": _TS,
                    }
                    self.fake._next_comment_id += 1
                    self.fake.comments.setdefault(number, []).append(comment)
                self._send_json(201, comment, write=write)
                return

        match = _LABELS_RE.match(path)
        if method == "PUT" and match:
            number = int(match.group("number"))
            payload = self._body or {}
            with self.fake.lock:
                issue = self.fake.issues.get(number)
                if issue is None:
                    self._not_found(write)
                    return
                issue["labels"] = [{"name": name} for name in payload.get("labels") or []]
                body = list(issue["labels"])
            self._send_json(200, body, write=write)
            return

        match = _ISSUE_RE.match(path)
        if method == "GET" and match:
            number = int(match.group("number"))
            with self.fake.lock:
                issue = self.fake.issues.get(number)
                payload = dict(issue) if issue is not None else None
            if payload is None:
                self._not_found(write)
                return
            self._send_json(200, payload, write=write)
            return

        if method == "GET" and _ISSUES_RE.match(path):
            labels = set((query.get("labels") or "").split(",")) - {""}
            with self.fake.lock:
                items = [
                    dict(issue)
                    for issue in self.fake.issues.values()
                    if not labels or labels.issubset({l["name"] for l in issue["labels"]})
                ]
            self._send_paged(sorted(items, key=lambda i: i["number"]), query, page, write=write)
            return

        self._not_found(write)

    # -- 具体动作 ------------------------------------------------------------
    def _create_ref(self, write: bool) -> None:
        payload = self._body or {}
        ref = str(payload.get("ref") or "")
        sha = str(payload.get("sha") or "")
        if not ref.startswith("refs/") or not sha:
            self._send_json(422, {"message": "invalid ref payload"}, write=write)
            return
        # 锚点必须存在（GitHub 对不存在的 sha 也是 422）；锚点可以是任意已存在 ref 的 sha。
        with self.fake.lock:
            known = {self.fake.anchor_sha} | set(self.fake.refs.values())
        if sha not in known:
            self._send_json(422, {"message": "Object does not exist"}, write=write)
            return
        if self.fake._add_ref(ref, sha):
            self._send_json(201, {"ref": ref, "object": {"sha": sha, "type": "commit"}}, write=write)
        else:
            self._send_json(422, {"message": "Reference already exists"}, write=write)

    def _read_json(self) -> Optional[dict[str, Any]]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    # -- 响应 ----------------------------------------------------------------
    def _send_paged(
        self,
        items: list[Any],
        query: dict[str, str],
        page: int,
        *,
        write: bool,
    ) -> None:
        if not write:
            return
        size = self.fake.page_size_override or int(query.get("per_page", self.fake.default_page_size))
        size = max(1, min(size, 100))
        start = (page - 1) * size
        chunk = items[start : start + size]
        headers: dict[str, str] = {}
        if start + size < len(items):
            host = self.headers.get("Host") or "127.0.0.1"
            next_query = dict(query)
            next_query["page"] = str(page + 1)
            next_query["per_page"] = str(size)
            link = f'<http://{host}{urllib.parse.urlsplit(self.path).path}?{urllib.parse.urlencode(next_query)}>; rel="next"'
            headers["Link"] = link
        self._send_json(200, chunk, headers=headers, write=write)

    def _send_json(
        self,
        status: int,
        payload: Any,
        *,
        headers: Optional[dict[str, str]] = None,
        write: bool = True,
    ) -> None:
        if not write:
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int, write: bool) -> None:
        if not write:
            return
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _not_found(self, write: bool) -> None:
        self._send_json(404, {"message": "Not Found"}, write=write)
