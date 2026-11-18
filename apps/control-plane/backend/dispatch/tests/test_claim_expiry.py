"""认领 / 分派超时过期：4 小时无进展 → CLAIM_EXPIRED + 标签回 state:todo。

同时验证 lease-reaper CLI 的 `--claim-expiry` 接线（注入假客户端，不访问网络）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dispatch import lease_reaper
from dispatch.claim_expiry import claim_expiry_marker, expire_stale_claims
from dispatch.tests.fake_github import FakeGitHub

NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)
CLAIM_AT = "2026-11-16T07:00:00Z"  # NOW 前 5 小时，超过 4h 租约


def _claim_body(moment: str) -> str:
    return f"<!-- agent-claim: muse-2, {moment} -->"


def _seed_claimed(fake: FakeGitHub, number: int = 7, moment: str = CLAIM_AT) -> None:
    fake.seed_issue(number, labels=("state:claimed", "agent:muse-2"))
    fake.seed_comment(
        number, _claim_body(moment), author="Jouwala", created_at=moment
    )


def _expired_markers(fake: FakeGitHub, number: int) -> list[str]:
    return [
        body
        for body in fake.comments_for(number)
        if "agent-msg: CLAIM_EXPIRED" in body
    ]


def test_stale_claim_is_expired_and_labels_converge(fake: FakeGitHub) -> None:
    _seed_claimed(fake)
    report = expire_stale_claims(fake, now=NOW)
    assert report["checked"] == 1
    assert report["expired"] == [7]
    labels = fake.labels_of(7)
    assert "state:claimed" not in labels
    assert "state:todo" in labels
    comments = _expired_markers(fake, 7)
    assert len(comments) == 1
    assert "CLAIM_EXPIRED" in comments[0]
    assert claim_expiry_marker(7, datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc)) in comments[0]


def test_fresh_claim_is_left_alone(fake: FakeGitHub) -> None:
    _seed_claimed(fake, moment="2026-11-16T10:00:00Z")  # 仅 2 小时
    report = expire_stale_claims(fake, now=NOW)
    assert report["expired"] == []
    assert "state:claimed" in fake.labels_of(7)
    assert _expired_markers(fake, 7) == []


def test_comment_after_claim_counts_as_progress(fake: FakeGitHub) -> None:
    _seed_claimed(fake)
    fake.seed_comment(
        7,
        "进展：已定位问题，正在改。",
        author="Jouwala",
        created_at="2026-11-16T09:30:00Z",
    )
    report = expire_stale_claims(fake, now=NOW)
    assert report["expired"] == []
    assert "state:claimed" in fake.labels_of(7)


def test_bot_comment_does_not_count_as_progress(fake: FakeGitHub) -> None:
    _seed_claimed(fake)
    fake.seed_comment(
        7, "CI 通过", author="github-actions", created_at="2026-11-16T09:30:00Z"
    )
    assert expire_stale_claims(fake, now=NOW)["expired"] == [7]


def test_open_pr_related_to_issue_counts_as_progress(fake: FakeGitHub) -> None:
    _seed_claimed(fake)
    fake.seed_pull(41, "a" * 40, body="Closes #7", merged=False)
    report = expire_stale_claims(fake, now=NOW)
    assert report["expired"] == []
    assert "state:claimed" in fake.labels_of(7)


def test_second_run_does_not_duplicate_expiry_comment(fake: FakeGitHub) -> None:
    _seed_claimed(fake)
    expire_stale_claims(fake, now=NOW)
    # 模拟“标签回写成功但评论已发过”的重复巡检：把标签再种回 claimed。
    fake.issues[7]["labels"] = ["state:claimed", "agent:muse-2"]
    expire_stale_claims(fake, now=NOW)
    assert len(_expired_markers(fake, 7)) == 1


def test_dry_run_does_not_write(fake: FakeGitHub) -> None:
    _seed_claimed(fake)
    report = expire_stale_claims(fake, now=NOW, dry_run=True)
    assert report["expired"] == [7]
    assert "state:claimed" in fake.labels_of(7)
    assert _expired_markers(fake, 7) == []


def test_claim_without_parsable_time_is_skipped(fake: FakeGitHub) -> None:
    fake.seed_issue(7, labels=("state:claimed", "agent:muse-2"))
    fake.seed_comment(7, "认领（老格式，没有时间头）")
    report = expire_stale_claims(fake, now=NOW)
    assert report["checked"] == 1
    assert report["expired"] == []
    assert "state:claimed" in fake.labels_of(7)


def test_log_line_matches_ops_contract(fake: FakeGitHub) -> None:
    _seed_claimed(fake)
    logs: list[str] = []
    expire_stale_claims(fake, now=NOW, log=logs.append)
    assert logs[-1] == "[claim-expiry] 巡检 1 个 Issue，过期 1 个"


def test_partial_failure_is_explicit(fake: FakeGitHub) -> None:
    _seed_claimed(fake)
    fake.inject(method="POST", path_contains="/comments", status=500, times=5)
    fake.retry_limit = 0
    with pytest.raises(RuntimeError):
        expire_stale_claims(fake, now=NOW)


# ---------------------------------------------------------------------------
# lease-reaper CLI 接线（--claim-expiry）
# ---------------------------------------------------------------------------


def test_cli_claim_expiry_flag(monkeypatch) -> None:
    fake = FakeGitHub()
    fake.seed_issue(7, labels=("state:claimed", "agent:muse-2"))
    fake.seed_comment(
        7,
        "<!-- agent-claim: muse-2, 2020-01-01T00:00:00Z -->",
        author="Jouwala",
        created_at="2020-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        lease_reaper, "reap", lambda *args, **kwargs: {"dry_run": False}
    )
    logs: list[str] = []
    code = lease_reaper.main(
        ["--claim-expiry"],
        env={"REPO": "acme/demo", "GH_TOKEN": "t"},
        ref_store=object(),
        github_client=fake,
        log=logs.append,
    )
    assert code == 0
    assert "state:claimed" not in fake.labels_of(7)
    assert any("巡检 1 个 Issue，过期 1 个" in line for line in logs)


def test_cli_claim_expiry_posts_no_progress_alert(monkeypatch) -> None:
    """--claim-expiry 过期的任务同时往 Muse 1 收件箱发 no-progress 告警（#143）。"""
    from dispatch.alert_inbox import INBOX_TITLE, find_inbox

    fake = FakeGitHub()
    _seed_claimed(fake)
    monkeypatch.setattr(
        lease_reaper, "reap", lambda *args, **kwargs: {"dry_run": False}
    )
    logs: list[str] = []
    code = lease_reaper.main(
        ["--claim-expiry"],
        env={"REPO": "acme/demo", "GH_TOKEN": "t"},
        ref_store=object(),
        github_client=fake,
        log=logs.append,
    )
    assert code == 0
    inbox = find_inbox(fake, "acme/demo")
    assert inbox is not None
    assert fake.issues[inbox]["title"] == INBOX_TITLE
    bodies = fake.comments_for(inbox)
    assert len(bodies) == 1
    assert "muse1-alert: no-progress:issue-7" in bodies[0]
    assert "2026-11-16T07:00:00Z" in bodies[0]


def test_cli_dry_run_does_not_post_alert(monkeypatch) -> None:
    from dispatch.alert_inbox import find_inbox

    fake = FakeGitHub()
    _seed_claimed(fake)
    monkeypatch.setattr(
        lease_reaper, "reap", lambda *args, **kwargs: {"dry_run": True}
    )
    code = lease_reaper.main(
        ["--claim-expiry", "--dry-run"],
        env={"REPO": "acme/demo", "GH_TOKEN": "t"},
        ref_store=object(),
        github_client=fake,
        log=lambda *_: None,
    )
    assert code == 0
    assert find_inbox(fake, "acme/demo") is None


def test_cli_alert_blocked_flag_escalates(monkeypatch) -> None:
    """--alert-blocked：P0 阻塞任务立即升级到收件箱（#143）。"""
    from dispatch.alert_inbox import find_inbox

    fake = FakeGitHub()
    fake.seed_issue(
        8,
        labels=("state:blocked", "agent:muse-2"),
        body="## 任务说明\n\n## 优先级\n\nP0\n",
    )
    monkeypatch.setattr(
        lease_reaper, "reap", lambda *args, **kwargs: {"dry_run": False}
    )
    code = lease_reaper.main(
        ["--alert-blocked"],
        env={"REPO": "acme/demo", "GH_TOKEN": "t"},
        ref_store=object(),
        github_client=fake,
        log=lambda *_: None,
    )
    assert code == 0
    inbox = find_inbox(fake, "acme/demo")
    assert inbox is not None
    bodies = fake.comments_for(inbox)
    assert len(bodies) == 1
    assert "muse1-alert: blocked-escalation:issue-8" in bodies[0]


def test_cli_rejects_non_positive_claim_ttl(monkeypatch) -> None:
    monkeypatch.setattr(lease_reaper, "reap", lambda *args, **kwargs: {})
    code = lease_reaper.main(
        ["--claim-expiry", "--claim-ttl-hours", "0"],
        env={"REPO": "acme/demo", "GH_TOKEN": "t"},
        ref_store=object(),
        github_client=FakeGitHub(),
        log=lambda *_: None,
    )
    assert code == 2
