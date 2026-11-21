"""TASK-01：GitHub REST 全量分页与显式失败（不访问真实网络）。

用 `FakeGitHub` 假 opener 走**真实分页代码路径**：50/100/101/150/250 issues、
150 comments、完成/认领记录落在第 2 页、第 2 页失败、空页、重复页去重、
Link rel=next、page 递增兜底、max_pages 上限、403/429/5xx 重试与 Retry-After。
"""

from __future__ import annotations

import pytest

import sched_testkit as tk

BASE = "/repos/o/r"
ISSUES = f"{BASE}/issues"


def comments_path(number: int) -> str:
    return f"{BASE}/issues/{number}/comments"


def issue_path(number: int) -> str:
    return f"{BASE}/issues/{number}"


def make_transport(gi, pages, *, fail=None, sleeps=None, **kwargs):
    sleeps = sleeps if sleeps is not None else []
    fake = tk.FakeGitHub(pages, fail)
    transport = gi.RestGitHubTransport(
        "o", "r", token="test", sleep=sleeps.append, http_open=fake, **kwargs
    )
    return transport, fake, sleeps


def issue_list(count: int) -> list[dict]:
    return [tk.issue_json(number) for number in range(1, count + 1)]


@pytest.mark.parametrize(
    ("count", "expected_calls"),
    [(50, [1]), (100, [1, 2]), (101, [1, 2]), (150, [1, 2]), (250, [1, 2, 3])],
)
def test_p01_p02_issues_full_pagination(adapter_module, count, expected_calls):
    transport, fake, _ = make_transport(
        adapter_module, {ISSUES: tk.chunks(issue_list(count))}
    )
    issues = transport.list_issues()
    assert [issue.number for issue in issues] == list(range(1, count + 1))
    assert [call["page"] for call in fake.calls] == expected_calls
    assert all(call["query"]["per_page"] == "100" for call in fake.calls)


def test_p03_one_hundred_items_without_link_uses_page_increment_fallback(adapter_module):
    transport, fake, _ = make_transport(adapter_module, {ISSUES: tk.chunks(issue_list(100))})
    issues = transport.list_issues()
    assert len(issues) == 100
    assert [call["page"] for call in fake.calls] == [1, 2]  # 满页兜底再探一次空页


def test_p04_comments_full_pagination_150(adapter_module):
    comments = [tk.comment_json(index, f"评论 {index}") for index in range(1, 151)]
    transport, fake, _ = make_transport(adapter_module, {comments_path(42): tk.chunks(comments)})
    result = transport.list_comments(42)
    assert len(result) == 150
    assert result[0].id == 700001 and result[-1].id == 700150
    assert [call["page"] for call in fake.calls] == [1, 2]


def test_p05_completion_record_on_page_two_is_found(adapter_module):
    # GitHub 原生权威层的完成标记（ref_completion 使用；这里只验证分页能读到它）
    marker = "<!-- ref-completion-id: key-page2 -->"
    comments = [tk.comment_json(index, f"进展 {index}") for index in range(1, 151)]
    comments[120]["body"] = f"TASK_COMPLETED\n{marker}"
    transport, fake, _ = make_transport(adapter_module, {comments_path(42): tk.chunks(comments)})
    bodies = [comment.body for comment in transport.list_comments(42)]
    assert sum(marker in body for body in bodies) == 1
    assert fake.calls[-1]["page"] == 2


def test_p06_claim_record_on_page_two_is_parsed_by_adapter(adapter_module, clock):
    claim_body = (
        f"{adapter_module.claim_line('muse-2', clock())}\n"
        f"{adapter_module.msg_header('TASK_ACCEPTED', 42, 'muse-2', clock())}\n"
        "muse-2 认领本任务。"
    )
    progress_body = (
        f"{adapter_module.msg_header('TASK_PROGRESS', 42, 'muse-2', clock())}\n"
        "开始执行。\nattempt: a-9"
    )
    comments = [tk.comment_json(index, f"历史进展 {index}") for index in range(1, 101)]
    comments += [tk.comment_json(101, claim_body), tk.comment_json(102, progress_body)]
    transport, fake, _ = make_transport(adapter_module, {comments_path(42): tk.chunks(comments)})
    adapter = adapter_module.GitHubIssueAdapter(transport, "muse-2", clock=clock)
    lease = adapter.lease_of(42)
    assert lease is not None
    assert lease.holder == "muse-2"
    assert lease.attempt_id == "a-9"
    assert fake.calls[-1]["page"] == 2  # 第 2 页的认领记录确实被读到


def test_p07_second_page_failure_is_explicit(adapter_module):
    comments = [tk.comment_json(index, f"评论 {index}") for index in range(1, 251)]
    transport, fake, sleeps = make_transport(
        adapter_module,
        {comments_path(42): tk.chunks(comments)},
        fail={(comments_path(42), 2): 503},
        max_retries=2,
    )
    with pytest.raises(adapter_module.TransportError) as info:
        transport.list_comments(42)
    assert "HTTP 503" in str(info.value)
    assert [call["page"] for call in fake.calls] == [1, 2, 2, 2]  # 1 次成功 + 3 次重试
    assert len(sleeps) == 2  # 有限重试，最终显式放弃，不返回半截数据


def test_p08_empty_second_page_stops(adapter_module):
    transport, fake, _ = make_transport(
        adapter_module, {ISSUES: [tk.chunks(issue_list(100))[0], []]}
    )
    issues = transport.list_issues()
    assert len(issues) == 100
    assert [call["page"] for call in fake.calls] == [1, 2]


def test_p09_duplicate_pages_and_ids_are_deduplicated(adapter_module):
    page = issue_list(100)
    transport, fake, _ = make_transport(adapter_module, {ISSUES: [page, page]})
    issues = transport.list_issues()
    assert len(issues) == 100
    assert [issue.number for issue in issues] == list(range(1, 101))
    assert [call["page"] for call in fake.calls] == [1, 2, 3]


def test_p10_link_next_with_custom_page_size(adapter_module):
    pages = [[tk.issue_json(1), tk.issue_json(2)], [tk.issue_json(3), tk.issue_json(4)], [tk.issue_json(5)]]
    transport, fake, _ = make_transport(adapter_module, {ISSUES: pages}, page_size=2)
    issues = transport.list_issues()
    assert [issue.number for issue in issues] == [1, 2, 3, 4, 5]
    assert [call["page"] for call in fake.calls] == [1, 2, 3]
    assert all(call["query"]["per_page"] == "2" for call in fake.calls)


def test_p11_max_pages_exceeded_is_explicit(adapter_module):
    transport, _, _ = make_transport(
        adapter_module, {ISSUES: tk.chunks(issue_list(101))}, max_pages=1
    )
    with pytest.raises(adapter_module.TransportError) as info:
        transport.list_issues()
    assert "max_pages=1" in str(info.value)


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 504])
def test_p12_retryable_statuses_are_retried(adapter_module, status):
    sleeps: list[float] = []
    transport, fake, _ = make_transport(
        adapter_module,
        {ISSUES: tk.chunks(issue_list(2))},
        fail={ISSUES: [status]},
        sleeps=sleeps,
    )
    issues = transport.list_issues()
    assert len(issues) == 2
    assert len(fake.calls) == 2
    assert len(sleeps) == 1


def test_p13_retry_after_header_is_respected(adapter_module):
    transport, fake, sleeps = make_transport(
        adapter_module,
        {ISSUES: tk.chunks(issue_list(2))},
        fail={ISSUES: [{"status": 429, "retry_after": "2"}]},
    )
    assert len(transport.list_issues()) == 2
    assert sleeps == [2.0]
    assert len(fake.calls) == 2


def test_p14_retry_exhausted_raises(adapter_module):
    transport, fake, sleeps = make_transport(
        adapter_module, {ISSUES: tk.chunks(issue_list(2))}, fail={ISSUES: 503}, max_retries=2
    )
    with pytest.raises(adapter_module.TransportError):
        transport.list_issues()
    assert len(fake.calls) == 3
    assert len(sleeps) == 2


def test_p15_non_retryable_404_raises_immediately(adapter_module):
    transport, fake, sleeps = make_transport(
        adapter_module, {ISSUES: tk.chunks(issue_list(2))}, fail={ISSUES: 404}
    )
    with pytest.raises(adapter_module.TransportError) as info:
        transport.list_issues()
    assert "HTTP 404" in str(info.value)
    assert len(fake.calls) == 1 and sleeps == []


def test_p16_list_issues_query_params_and_pull_request_filter(adapter_module):
    pull = tk.issue_json(99, title="PR")
    pull["pull_request"] = {"url": "https://example/pr/99"}
    transport, fake, _ = make_transport(
        adapter_module, {ISSUES: [[tk.issue_json(1), pull, tk.issue_json(2)]]}
    )
    issues = transport.list_issues(labels=("agent:muse-2", "state:todo"), state="open")
    assert [issue.number for issue in issues] == [1, 2]
    query = fake.calls[0]["query"]
    assert query["state"] == "open"
    assert query["labels"] == "agent:muse-2,state:todo"
    assert query["per_page"] == "100"


def test_p17_register_heartbeat_instance_identity(adapter_module, clock):
    transport = adapter_module.InMemoryGitHubTransport(clock=clock)
    transport.create_issue(1, "注册表")
    adapter = adapter_module.GitHubIssueAdapter(
        transport,
        "muse-2",
        clock=clock,
        registry_issue=1,
        instance_id="i-7",
        hostname="host-a",
        platform="linux",
    )
    info = adapter.register(capabilities=("python", "git"), vm="VM-A")
    assert info["instance_id"] == "i-7" and info["hostname"] == "host-a"
    body = transport.list_comments(1)[0].body
    assert "worker-registry: muse-2" in body
    assert "worker-instance: i-7" in body and "platform: linux" in body

    clock.advance(601)
    assert adapter.heartbeat("持续在线") is True
    heartbeat_body = transport.list_comments(1)[-1].body
    assert "worker-heartbeat: muse-2" in heartbeat_body
    assert "worker-instance: i-7" in heartbeat_body


def test_p18_register_backward_compatible_without_instance(adapter_module, clock):
    transport = adapter_module.InMemoryGitHubTransport(clock=clock)
    transport.create_issue(1, "注册表")
    adapter = adapter_module.GitHubIssueAdapter(transport, "muse-3", clock=clock, registry_issue=1)
    info = adapter.register(capabilities=("python",), vm="VM-B")
    assert info["agent"] == "muse-3"
    body = transport.list_comments(1)[0].body
    assert "worker-registry: muse-3" in body
    assert "worker-instance:" not in body  # 不传实例身份时保持旧协议写法


def test_p20_default_opener_resolves_at_call_time(adapter_module, monkeypatch):
    """未注入 http_open 时也应该走当前 urllib.request.urlopen（便于故障注入测试）。"""
    fake = tk.FakeGitHub({ISSUES: tk.chunks(issue_list(3))})
    monkeypatch.setattr(adapter_module.urllib.request, "urlopen", fake)
    transport = adapter_module.RestGitHubTransport("o", "r", token="t", sleep=lambda s: None)
    assert len(transport.list_issues()) == 3
    assert fake.calls


@pytest.mark.parametrize("failure", [500, TimeoutError("模拟超时")])
def test_p21_post_is_not_auto_retried(adapter_module, failure):
    """POST（发评论）不自动重试：服务端可能已应用而客户端超时，重发会重复评论。"""
    sleeps: list[float] = []
    transport, fake, _ = make_transport(
        adapter_module,
        {comments_path(42): []},
        fail={comments_path(42): failure},
        sleeps=sleeps,
        max_retries=3,
    )
    with pytest.raises(adapter_module.TransportError):
        transport.add_comment(42, "TASK_COMPLETED 评论")
    posts = [call for call in fake.calls if call["method"] == "POST"]
    assert len(posts) == 1  # 只发一次，没有自动重发
    assert sleeps == []


def test_p22_get_retries_after_500(adapter_module):
    sleeps: list[float] = []
    transport, fake, _ = make_transport(
        adapter_module,
        {ISSUES: tk.chunks(issue_list(2))},
        fail={ISSUES: [500]},
        sleeps=sleeps,
    )
    assert len(transport.list_issues()) == 2
    assert [call["page"] for call in fake.calls] == [1, 1]  # 同一页重试
    assert len(sleeps) == 1


def test_p23_put_retries_idempotent_label_write(adapter_module):
    labels_path = issue_path(42) + "/labels"
    sleeps: list[float] = []
    transport, fake, _ = make_transport(
        adapter_module,
        {},
        fail={labels_path: [503]},
        sleeps=sleeps,
    )
    transport.set_labels(42, ["state:in-review"])
    assert [call["method"] for call in fake.calls] == ["PUT", "PUT"]
    assert len(sleeps) == 1
