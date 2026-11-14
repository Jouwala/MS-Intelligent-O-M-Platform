"""内存假 GitHub API：让分页 / 重试 / 并发 / 故障注入都变成真实执行。

实现的是 `GitHubClient._raw_request` 这一层：真实的 `paginate()`（Link 跟随、
去重、空页停止、上限）、`_request_full()`（重试策略）与业务模块（CAS 登记、
评论对账）全部走生产代码，只有 HTTP 传输被换掉。这样：

- **ref 唯一性**由假 API 强制：重复 `POST /git/refs` 返回 422，并发测试才有
  意义；
- **分页**按 `per_page` / `page` 切页并生成 `Link: rel="next"`；
- **故障注入**支持：指定第几页 `500`、`403` 限流、`429 Retry-After`，
  以及“请求已生效后客户端超时”（先写入再抛 `GitHubTransportError`）。

仅测试使用，不进生产路径。
"""

from __future__ import annotations

import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from dispatch.github_client import GitHubClient, GitHubTransportError

# 测试常用 SHA（main / merge commit / PR head）；集中在假 API 里避免各测试重复定义。
MAIN_SHA = "1" * 40
MERGE_SHA = "2" * 40
HEAD_SHA = "3" * 40


@dataclass
class _Failure:
    """一条故障注入规则；times 用尽后自动失效。"""

    method: str | None = None
    path_contains: str | None = None
    times: int = 1
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    apply_first: bool = False

    def matches(self, method: str, url: str) -> bool:
        if self.method and self.method != method:
            return False
        if self.path_contains and self.path_contains not in url:
            return False
        return True


class FakeGitHub(GitHubClient):
    """内存版 GitHub；所有状态线程安全（并发分派测试会同时打进来）。"""

    def __init__(
        self,
        repo: str = "acme/demo",
        *,
        main_sha: str = "0" * 40,
        base_url: str = "https://api.github.test",
        retry_limit: int = 2,
    ) -> None:
        super().__init__(
            repo,
            token="fake-token",
            base_url=base_url,
            retry_limit=retry_limit,
            retry_base_delay=0.0,
            sleep=lambda _seconds: None,
        )
        self.lock = threading.RLock()
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.refs: dict[str, str] = {"refs/heads/main": main_sha}
        self.known_shas: set[str] = {main_sha, MERGE_SHA, HEAD_SHA}
        self.failures: list[_Failure] = []
        self._next_id = 5000

    # ------------------------------------------------------------------ 造数据
    def seed_issue(
        self,
        number: int,
        *,
        labels: tuple[str, ...] | list[str] = (),
        body: str = "",
        state: str = "open",
        title: str | None = None,
        pull_request: bool = False,
    ) -> dict[str, Any]:
        with self.lock:
            self.issues[number] = {
                "state": state,
                "body": body,
                "labels": list(labels),
                "title": title or f"Issue {number}",
                "pull_request": pull_request,
            }
            self.comments.setdefault(number, [])
            return self.issues[number]

    def seed_ready_issue(
        self,
        number: int,
        *,
        agent: str = "agent:muse-2",
        body: str = "## 任务说明\n可认领。\n",
    ) -> dict[str, Any]:
        return self.seed_issue(number, labels=("state:todo", agent), body=body)

    def seed_pull(
        self,
        number: int,
        head_sha: str,
        *,
        head_ref: str = "agent/task-1-demo",
        base: str = "main",
        body: str = "",
        merged: bool = True,
        merge_sha: str | None = None,
        labels: tuple[str, ...] | list[str] = (),
        draft: bool = False,
    ) -> dict[str, Any]:
        with self.lock:
            pull = {
                "number": number,
                "state": "closed" if merged else "open",
                "merged": merged,
                "merge_commit_sha": merge_sha,
                "draft": draft,
                "body": body,
                "head": {"ref": head_ref, "sha": head_sha},
                "base": {"ref": base},
                "labels": [{"name": lb} for lb in labels],
            }
            self.pulls[number] = pull
            self.known_shas.add(head_sha)
            if merge_sha:
                self.known_shas.add(merge_sha)
            return pull

    def seed_comment(self, number: int, body: str) -> None:
        with self.lock:
            if number not in self.issues:
                # 便利默认：只造评论时自动补一个空 Issue，避免每处都先 seed_issue。
                self.seed_issue(number)
            self._next_id += 1
            self.comments.setdefault(number, []).append(
                {"id": self._next_id, "body": body, "user": {"login": "github-actions"}}
            )

    def seed_comments(self, number: int, bodies: list[str]) -> None:
        for body in bodies:
            self.seed_comment(number, body)

    # ------------------------------------------------------------------ 故障注入
    def inject(
        self,
        *,
        method: str | None = None,
        path_contains: str | None = None,
        times: int = 1,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
        apply_first: bool = False,
    ) -> None:
        """注入故障：status → 返回错误码；否则抛传输超时（apply_first=先应用再抛）。"""
        with self.lock:
            self.failures.append(
                _Failure(
                    method=method.upper() if method else None,
                    path_contains=path_contains,
                    times=times,
                    status=status,
                    headers=dict(headers or {}),
                    body=body,
                    apply_first=apply_first,
                )
            )

    def _take_failure(self, method: str, url: str) -> _Failure | None:
        for failure in self.failures:
            if failure.times <= 0 or not failure.matches(method, url):
                continue
            failure.times -= 1
            return failure
        return None

    # ------------------------------------------------------------------ 断言辅助
    def labels_of(self, number: int) -> list[str]:
        with self.lock:
            return list(self.issues[number]["labels"])

    def comments_for(self, number: int) -> list[str]:
        with self.lock:
            return [c["body"] for c in self.comments.get(number, [])]

    def markers(self, number: int, marker: str) -> list[str]:
        return [body for body in self.comments_for(number) if marker in body]

    def refs_with_prefix(self, prefix: str) -> list[str]:
        with self.lock:
            return sorted(ref for ref in self.refs if ref.startswith(prefix))

    # ------------------------------------------------------------------ 传输入口
    def _raw_request(
        self,
        method: str,
        url: str,
        payload: Any,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, str], Any]:
        method = method.upper()
        with self.lock:
            failure = self._take_failure(method, url)
            if failure is not None:
                if failure.status is not None:
                    return (
                        failure.status,
                        dict(failure.headers),
                        failure.body or {"message": "injected failure"},
                    )
                if failure.apply_first:
                    self._route(method, url, payload)
                raise GitHubTransportError(f"模拟客户端超时：{method} {url}")
            return self._route(method, url, payload)

    # ------------------------------------------------------------------ 路由
    def _route(self, method: str, url: str, payload: Any) -> tuple[int, dict[str, str], Any]:
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        repo_prefix = f"/repos/{self.repo}/"
        rest = path[len(repo_prefix):] if path.startswith(repo_prefix) else None
        if rest is not None:
            match = re.fullmatch(r"issues/(\d+)/comments", rest)
            if match:
                return self._comments(method, int(match.group(1)), query, payload)
            match = re.fullmatch(r"issues/(\d+)/labels", rest)
            if match:
                return self._labels(method, int(match.group(1)), query, payload)
            match = re.fullmatch(r"issues/(\d+)/labels/(.+)", rest)
            if match:
                return self._delete_label(
                    method, int(match.group(1)), urllib.parse.unquote(match.group(2))
                )
            match = re.fullmatch(r"issues/(\d+)", rest)
            if match:
                return self._issue_get(method, int(match.group(1)))
            if rest == "issues":
                return self._issues_list(method, query)
            match = re.fullmatch(r"pulls/(\d+)", rest)
            if match:
                return self._pull_get(method, int(match.group(1)))
            if rest == "pulls":
                return self._pulls_list(method, query)
            match = re.fullmatch(r"git/ref/(.+)", rest)
            if match:
                return self._ref_get(method, urllib.parse.unquote(match.group(1)))
            if rest == "git/refs":
                return self._ref_create(method, payload)
            match = re.fullmatch(r"git/refs/(.+)", rest)
            if match:
                return self._ref_delete(method, urllib.parse.unquote(match.group(1)))
            match = re.fullmatch(r"git/matching-refs/(.+)", rest)
            if match:
                return self._matching_refs(method, urllib.parse.unquote(match.group(1)))
        if path == "/search/issues":
            return self._search(query)
        return 404, {}, {"message": f"假 API 未实现路由：{method} {url}"}

    # ------------------------------------------------------------------ 分页
    def _page(
        self,
        path: str,
        query: dict[str, str],
        items: list[Any],
        *,
        wrap_search: bool = False,
    ) -> tuple[int, dict[str, str], Any]:
        per_page = max(1, int(query.get("per_page", "100")))
        page = max(1, int(query.get("page", "1")))
        start = (page - 1) * per_page
        chunk = items[start:start + per_page]
        headers: dict[str, str] = {}
        if start + per_page < len(items):
            params = {k: v for k, v in query.items() if k not in ("page", "per_page")}
            params.update({"page": str(page + 1), "per_page": str(per_page)})
            next_url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
            headers["link"] = f'<{next_url}>; rel="next"'
        if wrap_search:
            return 200, headers, {"total_count": len(items), "items": chunk}
        return 200, headers, chunk

    # ------------------------------------------------------------------ Issue
    def _issue_view(self, number: int) -> dict[str, Any]:
        issue = self.issues[number]
        view: dict[str, Any] = {
            "number": number,
            "title": issue.get("title") or f"Issue {number}",
            "state": issue["state"],
            "body": issue.get("body") or "",
            "labels": [{"name": label} for label in issue["labels"]],
        }
        if issue.get("pull_request"):
            view["pull_request"] = {}
        return view

    def _issue_get(self, method: str, number: int) -> tuple[int, dict[str, str], Any]:
        if method != "GET":
            return 405, {}, {"message": "method not allowed"}
        if number not in self.issues:
            return 404, {}, {"message": "Not Found"}
        return 200, {}, self._issue_view(number)

    def _issues_list(self, method: str, query: dict[str, str]) -> tuple[int, dict[str, str], Any]:
        if method != "GET":
            return 405, {}, {"message": "method not allowed"}
        state = query.get("state", "open")
        labels = [lb for lb in (query.get("labels") or "").split(",") if lb]
        items = []
        for number in sorted(self.issues):
            issue = self.issues[number]
            if state not in ("all", issue["state"]):
                continue
            if labels and not all(label in issue["labels"] for label in labels):
                continue
            items.append(self._issue_view(number))
        return self._page(f"/repos/{self.repo}/issues", query, items)

    def _comments(
        self, method: str, number: int, query: dict[str, str], payload: Any
    ) -> tuple[int, dict[str, str], Any]:
        if number not in self.issues:
            return 404, {}, {"message": "Not Found"}
        if method == "GET":
            return self._page(
                f"/repos/{self.repo}/issues/{number}/comments",
                query,
                self.comments.get(number, []),
            )
        if method == "POST":
            body = payload.get("body") if isinstance(payload, dict) else None
            if not isinstance(body, str):
                return 422, {}, {"message": "body 必填"}
            self._next_id += 1
            comment = {
                "id": self._next_id,
                "body": body,
                "user": {"login": "github-actions"},
            }
            self.comments.setdefault(number, []).append(comment)
            return 201, {}, comment
        return 405, {}, {"message": "method not allowed"}

    def _labels(
        self, method: str, number: int, query: dict[str, str], payload: Any
    ) -> tuple[int, dict[str, str], Any]:
        if number not in self.issues:
            return 404, {}, {"message": "Not Found"}
        if method != "POST":
            return 405, {}, {"message": "method not allowed"}
        wanted = payload.get("labels") if isinstance(payload, dict) else None
        if not isinstance(wanted, list):
            return 422, {}, {"message": "labels 必填"}
        for label in wanted:  # 幂等：已存在的标签不重复加
            if label not in self.issues[number]["labels"]:
                self.issues[number]["labels"].append(label)
        return 200, {}, self._issue_view(number)

    def _delete_label(self, method: str, number: int, label: str) -> tuple[int, dict[str, str], Any]:
        if method != "DELETE" or number not in self.issues:
            return 404, {}, {"message": "Not Found"}
        if label not in self.issues[number]["labels"]:
            return 404, {}, {"message": "Label does not exist"}
        self.issues[number]["labels"].remove(label)
        return 200, {}, self._issue_view(number)

    # ------------------------------------------------------------------ PR
    def _pull_view(self, number: int) -> dict[str, Any]:
        return dict(self.pulls[number])

    def _pull_get(self, method: str, number: int) -> tuple[int, dict[str, str], Any]:
        if method != "GET" or number not in self.pulls:
            return 404, {}, {"message": "Not Found"}
        return 200, {}, self._pull_view(number)

    def _pulls_list(self, method: str, query: dict[str, str]) -> tuple[int, dict[str, str], Any]:
        if method != "GET":
            return 405, {}, {"message": "method not allowed"}
        items = [
            self._pull_view(number)
            for number in sorted(self.pulls)
            if self.pulls[number].get("state") == "open"
        ]
        return self._page(f"/repos/{self.repo}/pulls", query, items)

    # ------------------------------------------------------------------ git refs
    def _normalize_ref(self, ref: str) -> str:
        """单 ref 接口路径参数是 `heads/<branch>`（不带 refs/），内部统一存完整名。"""
        return ref if ref.startswith("refs/") else f"refs/{ref}"

    def _ref_get(self, method: str, ref: str) -> tuple[int, dict[str, str], Any]:
        if method != "GET":
            return 405, {}, {"message": "method not allowed"}
        ref = self._normalize_ref(ref)
        if ref not in self.refs:
            return 404, {}, {"message": "Not Found"}
        return 200, {}, {"ref": ref, "object": {"sha": self.refs[ref], "type": "commit"}}

    def _ref_create(self, method: str, payload: Any) -> tuple[int, dict[str, str], Any]:
        if method != "POST":
            return 405, {}, {"message": "method not allowed"}
        ref = payload.get("ref") if isinstance(payload, dict) else None
        sha = payload.get("sha") if isinstance(payload, dict) else None
        if not isinstance(ref, str) or not ref.startswith("refs/"):
            return 422, {}, {"message": "ref 格式非法"}
        if ref in self.refs:
            # 强制唯一：并发 CAS 的“输家”必然看到 422。
            return 422, {}, {"message": "Reference already exists"}
        if not isinstance(sha, str) or not sha:
            return 422, {}, {"message": "sha 必填"}
        if sha not in self.known_shas:
            return 422, {}, {"message": "Object does not exist"}
        self.refs[ref] = sha
        return 201, {}, {"ref": ref, "object": {"sha": sha, "type": "commit"}}

    def _ref_delete(self, method: str, ref: str) -> tuple[int, dict[str, str], Any]:
        if method != "DELETE":
            return 405, {}, {"message": "method not allowed"}
        ref = self._normalize_ref(ref)
        if ref not in self.refs:
            return 422, {}, {"message": "Reference does not exist"}
        del self.refs[ref]
        return 204, {}, None

    def _matching_refs(self, method: str, prefix: str) -> tuple[int, dict[str, str], Any]:
        if method != "GET":
            return 405, {}, {"message": "method not allowed"}
        full = f"refs/{prefix}"
        matched = [
            {"ref": ref, "object": {"sha": sha, "type": "commit"}}
            for ref, sha in sorted(self.refs.items())
            if ref.startswith(full)
        ]
        return 200, {}, matched

    # ------------------------------------------------------------------ search
    def _search(self, query: dict[str, str]) -> tuple[int, dict[str, str], Any]:
        text = query.get("q", "")
        dep = re.search(r"depends on #(\d+)", text)
        want_open = "state:open" in text
        items = []
        for number in sorted(self.issues):
            issue = self.issues[number]
            if want_open and issue["state"] != "open":
                continue
            if dep and f"depends on #{dep.group(1)}" not in (issue.get("body") or ""):
                continue
            items.append(self._issue_view(number))
        return self._page("/search/issues", query, items, wrap_search=True)
