"""GitHub 客户端：Link 分页、去重、重试、显式失败（TASK-01 的传输层保证）。"""

from __future__ import annotations

from typing import Any

import pytest

from dispatch.github_client import (
    GitHubAPIError,
    GitHubClient,
    GitHubPaginationError,
    parse_link_next,
    ref_param,
)

from dispatch.tests.fake_github import FakeGitHub


class _StubClient(GitHubClient):
    """按预设页表返回响应，用来单测分页循环本身。"""

    def __init__(self, pages: list[tuple[int, dict[str, str], Any]], **kwargs: Any) -> None:
        super().__init__("acme/demo", retry_limit=0, sleep=lambda _s: None, **kwargs)
        self.pages = pages
        self.calls = 0

    def _raw_request(self, method, url, payload, headers):  # noqa: ANN001
        status, headers_out, body = self.pages[self.calls]
        self.calls += 1
        return status, headers_out, body


def _link(url: str) -> dict[str, str]:
    return {"link": f'<{url}>; rel="next"'}


def test_parse_link_next_variants() -> None:
    assert parse_link_next(None) is None
    assert parse_link_next("") is None
    header = (
        '<https://api.github.com/x?page=2>; rel="next", '
        '<https://api.github.com/x?page=5>; rel="last"'
    )
    assert parse_link_next(header) == "https://api.github.com/x?page=2"
    assert parse_link_next('<https://a>; rel="prev"') is None


def test_paginate_follows_link_and_collects_all(fake: FakeGitHub) -> None:
    fake.seed_comments(1, [f"comment {i}" for i in range(150)])
    comments = fake.list_issue_comments(1)
    assert len(comments) == 150
    # 2 页 → 至少 2 次 GET；第一页带 Link，第二页没有。
    assert len([r for r in fake.requests if r[0] == "GET"]) >= 2


def test_paginate_dedupes_overlapping_pages() -> None:
    page1 = [{"id": 1}, {"id": 2}]
    page2 = [{"id": 2}, {"id": 3}]
    stub = _StubClient(
        [
            (200, _link("https://api.github.test/repos/acme/demo/items?page=2"), page1),
            (200, {}, page2),
        ],
        per_page=2,
    )
    items = stub.paginate("/items")
    assert [item["id"] for item in items] == [1, 2, 3]


def test_paginate_stops_on_empty_page_even_if_link_present() -> None:
    stub = _StubClient(
        [
            (200, _link("https://api.github.test/repos/acme/demo/items?page=2"), [{"id": 1}]),
            (200, _link("https://api.github.test/repos/acme/demo/items?page=3"), []),
        ]
    )
    assert stub.paginate("/items") == [{"id": 1}]
    assert stub.calls == 2


def test_paginate_over_limit_is_explicit_error() -> None:
    stub = _StubClient(
        [
            (200, _link("https://api.github.test/repos/acme/demo/items?page=2"), [{"id": 1}]),
            (200, _link("https://api.github.test/repos/acme/demo/items?page=3"), [{"id": 2}]),
            (200, {}, [{"id": 3}]),
        ],
        max_pages=2,
    )
    with pytest.raises(GitHubPaginationError):
        stub.paginate("/items")


def test_get_retries_500(fake: FakeGitHub) -> None:
    fake.seed_comments(1, ["a"])
    fake.inject(method="GET", path_contains="/comments", status=500, times=1)
    assert len(fake.list_issue_comments(1)) == 1


def test_get_retries_429_with_retry_after(fake: FakeGitHub) -> None:
    fake.seed_comments(1, ["a"])
    fake.inject(
        method="GET",
        path_contains="/comments",
        status=429,
        times=1,
        headers={"retry-after": "0"},
    )
    assert len(fake.list_issue_comments(1)) == 1


def test_403_permission_denied_not_retried(fake: FakeGitHub) -> None:
    fake.seed_comments(1, ["a"])
    fake.inject(
        method="GET",
        path_contains="/comments",
        status=403,
        times=5,
        body={"message": "Resource not accessible by integration"},
    )
    with pytest.raises(GitHubAPIError) as excinfo:
        fake.list_issue_comments(1)
    assert excinfo.value.status == 403
    assert len([r for r in fake.requests if r[0] == "GET"]) == 1


def test_post_failure_is_not_retried(fake: FakeGitHub) -> None:
    fake.seed_issue(1)
    fake.inject(method="POST", path_contains="/comments", status=500, times=5)
    with pytest.raises(GitHubAPIError):
        fake.post_issue_comment(1, "hello")
    assert fake.comments_for(1) == []
    assert len([r for r in fake.requests if r[0] == "POST"]) == 1


def test_page_two_failure_is_explicit(fake: FakeGitHub) -> None:
    fake.seed_comments(1, [f"c{i}" for i in range(150)])
    fake.retry_limit = 0
    fake.inject(method="GET", path_contains="page=2", status=500, times=5)
    with pytest.raises(GitHubAPIError):
        fake.list_issue_comments(1)


def test_missing_ref_returns_none(fake: FakeGitHub) -> None:
    assert fake.get_git_ref("refs/heads/nope") is None


def test_ref_param_uses_documented_heads_form() -> None:
    """GitHub 单 ref 接口文档口径是 `heads/<branch>`，不是 `refs/heads/<branch>`。"""
    assert ref_param("refs/heads/dispatch/x") == "heads/dispatch/x"
    assert ref_param("heads/dispatch/x") == "heads/dispatch/x"


def test_get_ref_request_does_not_use_refs_prefix(fake: FakeGitHub) -> None:
    fake.get_git_ref("refs/heads/main")
    urls = [url for method, url in fake.requests if method == "GET"]
    assert urls and all("/git/ref/heads/main" in url for url in urls)


def test_search_issues_paginates(fake: FakeGitHub) -> None:
    for number in range(1, 121):
        fake.seed_issue(number, body="depends on #1")
    fake.seed_issue(999, body="no dependency")
    items = fake.search_issues('repo:acme/demo "depends on #1" type:issue state:open')
    assert len(items) == 120
    assert all(item["number"] != 999 for item in items)
