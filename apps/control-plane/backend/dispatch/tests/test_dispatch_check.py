"""dispatch-check：分页、幂等标签/评论、失败显式、依赖方搜索。"""

from __future__ import annotations

import pytest

from dispatch.dispatch_check import (
    BLOCKED_MARKER,
    MISSING_MARKER,
    UNBLOCKED_MARKER,
    check_issue,
    find_dependents,
    run_dispatch_check,
)
from dispatch.github_client import GitHubAPIError

from dispatch.tests.fake_github import FakeGitHub

FULL_BODY = (
    "## 任务说明\n实现分派。\n"
    "## 负责人\nagent:muse-2\n"
    "## 优先级\nP1\n"
    "## 文件修改范围\ndocs/\n"
    "## 依赖任务\n无\n"
    "## 验收标准\n测试绿。\n"
    "## 所属分支\nagent/task-9\n"
)


def _dep_body(dep: int) -> str:
    return f"## 任务说明\n依赖 {dep}。\n\n## 依赖任务\ndepends on #{dep}\n"


# --------------------------------------------------------------------- 缺字段提醒
def test_missing_fields_reminder_is_sent_once(fake: FakeGitHub) -> None:
    fake.seed_issue(1, labels=("state:todo",), body="## 任务说明\n有内容\n")
    check_issue(fake, 1)
    assert len(fake.markers(1, MISSING_MARKER)) == 1
    comment = fake.markers(1, MISSING_MARKER)[0]
    assert "验收标准" in comment
    check_issue(fake, 1)
    assert len(fake.markers(1, MISSING_MARKER)) == 1


def test_full_body_gets_no_reminder(fake: FakeGitHub) -> None:
    fake.seed_issue(1, labels=("state:todo",), body=FULL_BODY)
    result = check_issue(fake, 1)
    assert fake.markers(1, MISSING_MARKER) == []
    assert result["number"] == 1


def test_reminder_skipped_for_claimed_state(fake: FakeGitHub) -> None:
    fake.seed_issue(1, labels=("state:claimed",), body="## 任务说明\n有内容\n")
    check_issue(fake, 1)
    assert fake.markers(1, MISSING_MARKER) == []


def test_marker_on_second_comment_page_prevents_duplicate(fake: FakeGitHub) -> None:
    fake.seed_issue(1, labels=("state:todo",), body="## 任务说明\n有内容\n")
    fake.seed_comments(1, [f"历史评论 {i}" for i in range(140)])
    fake.seed_comment(1, f"{MISSING_MARKER}\n## 缺必填字段提醒\n（早先已提醒）")
    check_issue(fake, 1)
    assert len(fake.comments_for(1)) == 141


def test_comment_scan_page_two_failure_is_explicit(fake: FakeGitHub) -> None:
    fake.seed_issue(7, labels=("state:todo",), body="## 任务说明\n有内容\n")
    fake.seed_comments(7, [f"c{i}" for i in range(150)])
    fake.retry_limit = 0
    fake.inject(method="GET", path_contains="page=2", status=500, times=5)
    with pytest.raises(GitHubAPIError):
        check_issue(fake, 7)


# --------------------------------------------------------------------- 依赖阻塞
def test_blocked_then_unblocked_is_idempotent(fake: FakeGitHub) -> None:
    fake.seed_issue(1, labels=("state:todo",), body=FULL_BODY)
    fake.seed_issue(2, labels=("state:todo",), body=_dep_body(1))

    check_issue(fake, 2)
    labels = fake.labels_of(2)
    assert labels.count("state:blocked") == 1
    assert len(fake.markers(2, BLOCKED_MARKER)) == 1

    # 重复执行：不重复评论、不重复标签。
    check_issue(fake, 2)
    assert fake.labels_of(2).count("state:blocked") == 1
    assert len(fake.markers(2, BLOCKED_MARKER)) == 1

    # 依赖关闭 → 摘标签 + 只发一条解封评论。
    fake.issues[1]["state"] = "closed"
    check_issue(fake, 2)
    assert "state:blocked" not in fake.labels_of(2)
    assert len(fake.markers(2, UNBLOCKED_MARKER)) == 1

    check_issue(fake, 2)
    assert len(fake.markers(2, UNBLOCKED_MARKER)) == 1


def test_unblocked_comment_not_sent_if_never_blocked(fake: FakeGitHub) -> None:
    fake.seed_issue(1, labels=("state:todo",), body=FULL_BODY)
    fake.seed_issue(2, labels=("state:todo",), body=_dep_body(1))
    fake.issues[1]["state"] = "closed"
    check_issue(fake, 2)
    assert fake.markers(2, UNBLOCKED_MARKER) == []


# --------------------------------------------------------------------- 分页扫描
def test_hourly_scan_paginates_all_todo_issues(fake: FakeGitHub) -> None:
    for number in range(1, 151):
        fake.seed_issue(number, labels=("state:todo",), body="")
    result = run_dispatch_check(fake, fake.repo)
    assert len(result["checked"]) == 150
    assert {item["number"] for item in result["checked"]} == set(range(1, 151))


def test_hourly_scan_also_checks_blocked_label(fake: FakeGitHub) -> None:
    fake.seed_issue(5, labels=("state:blocked",), body=FULL_BODY)
    result = run_dispatch_check(fake, fake.repo)
    assert [item["number"] for item in result["checked"]] == [5]


def test_event_single_issue_only_checks_that_issue(fake: FakeGitHub) -> None:
    fake.seed_issue(1, labels=("state:todo",), body="")
    fake.seed_issue(2, labels=("state:todo",), body=FULL_BODY)
    result = run_dispatch_check(fake, fake.repo, issue_number=2, action="edited")
    assert [item["number"] for item in result["checked"]] == [2]
    assert fake.markers(1, MISSING_MARKER) == []


def test_dependents_search_paginates(fake: FakeGitHub) -> None:
    fake.seed_issue(1, labels=("state:todo",), body=FULL_BODY)
    fake.issues[1]["state"] = "closed"
    for number in range(100, 251):
        fake.seed_issue(number, labels=("state:todo",), body=_dep_body(1))
    fake.seed_issue(999, labels=("state:blocked", "state:todo"), body=_dep_body(1))

    dependents = find_dependents(fake, fake.repo, 1)
    assert 100 in dependents and 250 in dependents and 999 in dependents
    assert len(dependents) == 152

    result = run_dispatch_check(fake, fake.repo, issue_number=1, action="closed")
    checked = {item["number"] for item in result["checked"]}
    assert {100, 250, 999}.issubset(checked)
    # 依赖关闭 → 第 2 页之后的依赖方也被解封。
    assert "state:blocked" not in fake.labels_of(250)
    assert len(fake.markers(999, UNBLOCKED_MARKER)) == 1


def test_not_agent_task_skipped(fake: FakeGitHub) -> None:
    fake.seed_issue(1, body="随便聊聊")
    result = check_issue(fake, 1)
    assert result["status"] == "skip"


def test_pull_request_skipped(fake: FakeGitHub) -> None:
    fake.seed_issue(1, labels=("state:todo",), pull_request=True)
    result = check_issue(fake, 1)
    assert result["status"] == "skip"


def test_closed_issue_skipped(fake: FakeGitHub) -> None:
    fake.seed_issue(1, labels=("state:todo",), state="closed")
    result = check_issue(fake, 1)
    assert result["status"] == "skip"
