"""告警收件箱（#143）：五类告警统一路由给 Muse 1，同类去重就地更新。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dispatch import alert_inbox
from dispatch.alert_inbox import (
    INBOX_TITLE,
    Alert,
    blocked_escalation_alert,
    blocked_since_from_events,
    ci_failure_alert,
    ensure_inbox,
    escalate_blocked_tasks,
    find_inbox,
    no_progress_alert,
    parse_priority,
    post_alert,
    reaper_failure_alert,
    scan_ownerless_tasks,
    should_escalate_blocked,
)
from dispatch.tests.fake_github import FakeGitHub

REPO = "acme/demo"
NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
AT = "2026-11-18T12:00:00Z"


def _ci_alert(pr: int = 145, run: str = "run-1") -> Alert:
    return ci_failure_alert(
        pr_number=pr,
        run_id=run,
        run_url=f"https://github.com/{REPO}/actions/runs/{run}",
        jobs="pytest 离线集",
        head_sha="a" * 40,
        at=AT,
        issue_number=141,
    )


def _blocked_issue(
    fake: FakeGitHub,
    number: int,
    *,
    priority: str = "P2",
    blocked_minutes_ago: int | None = None,
    is_pr: bool = False,
) -> None:
    fake.seed_issue(
        number,
        labels=("state:blocked", "agent:muse-2"),
        body=f"## 任务说明\n\n## 优先级\n\n{priority}\n",
        pull_request=is_pr,
    )
    if blocked_minutes_ago is not None:
        moment = NOW - timedelta(minutes=blocked_minutes_ago)
        fake.seed_events(
            number,
            [
                {
                    "id": 9000 + number,
                    "event": "labeled",
                    "label": {"name": "state:blocked"},
                    "created_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ],
        )


def test_inbox_created_when_missing(fake: FakeGitHub) -> None:
    result = post_alert(fake, REPO, _ci_alert())
    assert result["action"] == "created"
    inbox = result["issue"]
    assert fake.issues[inbox]["title"] == INBOX_TITLE
    assert len(fake.comments_for(inbox)) == 1
    assert "muse1-alert: ci-failure:pr-145" in fake.comments_for(inbox)[0]


def test_existing_inbox_reused_and_similar_title_ignored(fake: FakeGitHub) -> None:
    fake.seed_issue(7, title="[告警收件箱] Muse 1（旧）")
    inbox = ensure_inbox(fake, REPO)
    assert inbox != 7  # 标题不精确相等的不算收件箱
    assert find_inbox(fake, REPO) == inbox
    assert len(fake.issues) == 2  # 旧 Issue + 新建的收件箱，没有重复建


def test_same_alert_updates_in_place(fake: FakeGitHub) -> None:
    first = post_alert(fake, REPO, _ci_alert(run="run-1"))
    second = post_alert(fake, REPO, _ci_alert(run="run-2"))
    assert first["action"] == "created"
    assert second["action"] == "updated"
    assert first["comment_id"] == second["comment_id"]
    inbox = first["issue"]
    assert len(fake.comments_for(inbox)) == 1
    assert "run-2" in fake.comments_for(inbox)[0]


def test_identical_alert_body_is_unchanged(fake: FakeGitHub) -> None:
    first = post_alert(fake, REPO, _ci_alert())
    second = post_alert(fake, REPO, _ci_alert())
    assert first["action"] == "created"
    assert second["action"] == "unchanged"
    assert len(fake.comments_for(first["issue"])) == 1


def test_different_keys_are_separate_comments(fake: FakeGitHub) -> None:
    post_alert(fake, REPO, _ci_alert(pr=145))
    post_alert(fake, REPO, _ci_alert(pr=146))
    post_alert(
        fake,
        REPO,
        reaper_failure_alert(
            run_id="run-9", run_url="https://example.invalid/r/9", at=AT
        ),
    )
    inbox = find_inbox(fake, REPO)
    assert inbox is not None
    assert len(fake.comments_for(inbox)) == 3


def test_no_progress_alert_content(fake: FakeGitHub) -> None:
    alert = no_progress_alert(
        issue_number=122,
        claimed_at="2026-11-18T07:00:00Z",
        ttl_hours=4,
        at=AT,
    )
    result = post_alert(fake, REPO, alert)
    assert result["action"] == "created"
    body = fake.comments_for(result["issue"])[0]
    assert "muse1-alert: no-progress:issue-122" in body
    assert "Issue #122" in body
    assert "2026-11-18T07:00:00Z" in body
    assert "CLAIM_EXPIRED" in body


def test_alert_body_has_no_sensitive_values(fake: FakeGitHub) -> None:
    body = fake.comments_for(
        post_alert(fake, REPO, _ci_alert())["issue"]
    )[0]
    for banned in ("GH_TOKEN", "GITHUB_TOKEN", "Bearer", "ghp_", "password"):
        assert banned not in body


def test_parse_priority() -> None:
    assert parse_priority("## 优先级\n\nP0\n") == "P0"
    assert parse_priority("优先级：P1") == "P1"
    assert parse_priority("优先级： P2 ") == "P2"
    assert parse_priority("没有优先级字段") is None


def test_blocked_since_from_events() -> None:
    events = [
        {"id": 1, "event": "labeled", "label": {"name": "agent:muse-2"},
         "created_at": "2026-11-18T10:00:00Z"},
        {"id": 2, "event": "labeled", "label": {"name": "state:blocked"},
         "created_at": "2026-11-18T11:00:00Z"},
        {"id": 3, "event": "unlabeled", "label": {"name": "state:blocked"},
         "created_at": "2026-11-18T11:30:00Z"},
        {"id": 4, "event": "labeled", "label": {"name": "state:blocked"},
         "created_at": "2026-11-18T11:45:00Z"},
    ]
    assert blocked_since_from_events(events) == datetime(
        2026, 11, 18, 11, 45, tzinfo=timezone.utc
    )
    assert blocked_since_from_events([]) is None


def test_should_escalate_blocked() -> None:
    assert should_escalate_blocked("P0", None, NOW) is True
    assert should_escalate_blocked("P1", None, NOW) is True
    assert should_escalate_blocked("P2", None, NOW) is False
    assert should_escalate_blocked(None, None, NOW) is False
    assert (
        should_escalate_blocked("P2", NOW - timedelta(minutes=29), NOW) is False
    )
    assert (
        should_escalate_blocked("P2", NOW - timedelta(minutes=30), NOW) is True
    )
    assert (
        should_escalate_blocked("P2", NOW - timedelta(minutes=45), NOW) is True
    )


def test_blocked_high_priority_escalates_immediately(fake: FakeGitHub) -> None:
    _blocked_issue(fake, 10, priority="P0", blocked_minutes_ago=None)
    assert escalate_blocked_tasks(fake, REPO, now=NOW) == [10]
    inbox = find_inbox(fake, REPO)
    assert inbox is not None
    body = fake.comments_for(inbox)[0]
    assert "muse1-alert: blocked-escalation:issue-10" in body
    assert "P0" in body


def test_blocked_normal_under_30min_skipped(fake: FakeGitHub) -> None:
    _blocked_issue(fake, 11, priority="P2", blocked_minutes_ago=10)
    assert escalate_blocked_tasks(fake, REPO, now=NOW) == []
    assert find_inbox(fake, REPO) is None  # 无需升级时不建收件箱


def test_blocked_normal_over_30min_escalated(fake: FakeGitHub) -> None:
    _blocked_issue(fake, 12, priority="P2", blocked_minutes_ago=45)
    assert escalate_blocked_tasks(fake, REPO, now=NOW) == [12]


def test_blocked_normal_unknown_since_skipped(fake: FakeGitHub) -> None:
    _blocked_issue(fake, 13, priority="P2", blocked_minutes_ago=None)
    assert escalate_blocked_tasks(fake, REPO, now=NOW) == []


def test_blocked_pr_is_excluded(fake: FakeGitHub) -> None:
    _blocked_issue(fake, 14, priority="P0", blocked_minutes_ago=None, is_pr=True)
    assert escalate_blocked_tasks(fake, REPO, now=NOW) == []


def test_blocked_repeat_run_does_not_duplicate(fake: FakeGitHub) -> None:
    """时间前进的重复扫描也不重复：正文时间戳取阻塞事件时间（blocked_since），
    不是扫描时间（复审 P1：原实现用扫描时间，每 5 分钟重写一次评论）。"""
    _blocked_issue(fake, 15, priority="P2", blocked_minutes_ago=45)
    first = escalate_blocked_tasks(fake, REPO, now=NOW)
    second = escalate_blocked_tasks(
        fake, REPO, now=NOW + timedelta(minutes=5)
    )
    assert first == [15]
    assert second == []  # 正文相同，post_alert 直接跳过
    inbox = find_inbox(fake, REPO)
    assert inbox is not None
    bodies = fake.comments_for(inbox)
    assert len(bodies) == 1
    # 发生时间是阻塞打上时间（NOW 前 45 分钟），不是两次扫描时间。
    assert "发生时间：2026-11-18T11:15:00Z" in bodies[0]


def test_cli_ci_failure_uses_injected_client(fake: FakeGitHub) -> None:
    code = alert_inbox.main(
        [
            "ci-failure",
            "--pr", "145",
            "--run-id", "run-1",
            "--run-url", "https://example.invalid/r/1",
            "--jobs", "pytest 离线集",
            "--head-sha", "a" * 40,
            "--issue", "141",
            "--at", AT,
        ],
        env={"REPO": REPO, "GH_TOKEN": "fake-token"},
        client=fake,
    )
    assert code == 0
    inbox = find_inbox(fake, REPO)
    assert inbox is not None
    assert "muse1-alert: ci-failure:pr-145" in fake.comments_for(inbox)[0]


def test_cli_reaper_failure_uses_injected_client(fake: FakeGitHub) -> None:
    code = alert_inbox.main(
        [
            "reaper-failure",
            "--run-id", "run-9",
            "--run-url", "https://example.invalid/r/9",
            "--at", AT,
        ],
        env={"REPO": REPO, "GH_TOKEN": "fake-token"},
        client=fake,
    )
    assert code == 0
    inbox = find_inbox(fake, REPO)
    assert inbox is not None
    assert "muse1-alert: reaper-failure:latest" in fake.comments_for(inbox)[0]


def test_cli_missing_env_returns_2(fake: FakeGitHub) -> None:
    code = alert_inbox.main(
        ["ci-failure", "--pr", "1", "--run-id", "r",
         "--run-url", "u", "--head-sha", "a" * 40],
        env={},
        client=fake,
    )
    assert code == 2


def _ownerless_issue(
    fake: FakeGitHub,
    number: int,
    *,
    agent: str | None = None,
    created_at: str = "2026-11-18T00:00:00Z",
    is_pr: bool = False,
) -> None:
    labels = ["state:todo"] + ([agent] if agent else [])
    fake.seed_issue(
        number,
        labels=tuple(labels),
        title=f"任务 {number}",
        pull_request=is_pr,
        created_at=created_at,
    )


def test_ownerless_scan_posts_single_rolling_alert(fake: FakeGitHub) -> None:
    _ownerless_issue(fake, 20, created_at="2026-11-18T01:00:00Z")
    _ownerless_issue(fake, 21, created_at="2026-11-18T02:00:00Z")
    _ownerless_issue(fake, 22, agent="agent:muse-2")  # 有负责人，排除
    _ownerless_issue(fake, 23, is_pr=True)  # PR 条目，排除
    assert scan_ownerless_tasks(fake, REPO, now=NOW) == [20, 21]
    inbox = find_inbox(fake, REPO)
    assert inbox is not None
    bodies = fake.comments_for(inbox)
    assert len(bodies) == 1
    assert "muse1-alert: ownerless-task:current" in bodies[0]
    assert "#20" in bodies[0] and "#21" in bodies[0]
    assert "#22" not in bodies[0] and "#23" not in bodies[0]
    # 发生时间取清单里最新任务的创建时间（事件时间），不是扫描时间。
    assert "发生时间：2026-11-18T02:00:00Z" in bodies[0]


def test_ownerless_scan_repeat_does_not_duplicate(fake: FakeGitHub) -> None:
    """#176 回归：旧 bash 实现每轮用时间戳 marker + Search 标题查找，
    间歇落空就新建 Issue（#154/#164/#165/#167 四个重复）。收件箱路由下
    清单不变时重复扫描必须零写操作：不新建 Issue、不重写评论。"""
    _ownerless_issue(fake, 20)
    first = scan_ownerless_tasks(fake, REPO, now=NOW)
    second = scan_ownerless_tasks(fake, REPO, now=NOW + timedelta(minutes=5))
    assert first == [20]
    assert second == []  # 正文相同，post_alert 直接跳过
    inbox = find_inbox(fake, REPO)
    assert inbox is not None
    assert len(fake.comments_for(inbox)) == 1
    assert len(fake.issues) == 2  # 任务 Issue + 收件箱，没有多建


def test_ownerless_scan_updates_in_place_when_set_changes(
    fake: FakeGitHub,
) -> None:
    _ownerless_issue(fake, 20, created_at="2026-11-18T01:00:00Z")
    scan_ownerless_tasks(fake, REPO, now=NOW)
    inbox = find_inbox(fake, REPO)
    assert inbox is not None
    comment_id = fake.comments[inbox][0]["id"]
    _ownerless_issue(fake, 24, created_at="2026-11-18T03:00:00Z")
    assert scan_ownerless_tasks(fake, REPO, now=NOW) == [20, 24]
    bodies = fake.comments_for(inbox)
    assert len(bodies) == 1  # 仍是同一条评论，就地 PATCH
    assert fake.comments[inbox][0]["id"] == comment_id
    assert "#24" in bodies[0]


def test_ownerless_scan_empty_is_quiet(fake: FakeGitHub) -> None:
    _ownerless_issue(fake, 25, agent="agent:muse-3")
    assert scan_ownerless_tasks(fake, REPO, now=NOW) == []
    assert find_inbox(fake, REPO) is None  # 无需告警时不建收件箱


def test_cli_ownerless_scan_uses_injected_client(fake: FakeGitHub) -> None:
    _ownerless_issue(fake, 26)
    code = alert_inbox.main(
        ["ownerless-scan"],
        env={"REPO": REPO, "GH_TOKEN": "fake-token"},
        client=fake,
    )
    assert code == 0
    inbox = find_inbox(fake, REPO)
    assert inbox is not None
    assert "muse1-alert: ownerless-task:current" in fake.comments_for(inbox)[0]
