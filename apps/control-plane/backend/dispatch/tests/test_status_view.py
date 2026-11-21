"""统一多 Muse 状态视图（#142）：指标、渲染与就地更新。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dispatch import status_view
from dispatch.status_collect import collect_prs, collect_tasks, collect_workers
from dispatch.status_view import STATUS_MARKER, STATUS_TITLE, ensure_status_issue, find_status_issue, render_status, update_status
from dispatch.tests.fake_github import MAIN_SHA, FakeGitHub
from scheduler import ref_api, ref_heartbeat

REPO = "acme/demo"
NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _beat(fake: FakeGitHub, agent: str, instance: str, state: str, when: datetime) -> None:
    ref = ref_heartbeat.heartbeat_ref(
        agent, instance, ref_heartbeat.epoch_ms_of(when), state
    )
    fake.create_git_ref(ref, MAIN_SHA)


def _lease(
    fake: FakeGitHub,
    issue: int,
    agent: str,
    instance_hex: str,
    when: datetime,
) -> None:
    ref = ref_api.lease_ref(
        issue, 1, ref_heartbeat.epoch_ms_of(when), agent, instance_hex, "aabbccdd"
    )
    fake.create_git_ref(ref, MAIN_SHA)


def _labeled(number: int, label: str, when: datetime, event_id: int) -> dict:
    return {
        "id": event_id,
        "event": "labeled",
        "label": {"name": label},
        "created_at": _iso(when),
    }


# --------------------------------------------------------------------- Worker 七态
def test_workers_online_idle_and_counts(fake: FakeGitHub) -> None:
    _beat(fake, "agent-muse-2", "worker-1", "online", NOW - timedelta(seconds=30))
    _beat(fake, "agent-muse-3", "worker-1", "idle", NOW - timedelta(seconds=10))
    result = collect_workers(fake, NOW)
    assert result["counts"]["online"] == 1
    assert result["counts"]["idle"] == 1
    assert result["counts"]["working"] == 0
    assert [w["agent"] for w in result["workers"]] == ["agent-muse-2", "agent-muse-3"]


def test_workers_stale_beat_is_offline(fake: FakeGitHub) -> None:
    _beat(fake, "agent-muse-2", "worker-1", "online", NOW - timedelta(hours=2))
    result = collect_workers(fake, NOW)
    assert result["workers"][0]["state"] == "offline"


def test_workers_live_lease_upgrades_idle_to_working(fake: FakeGitHub) -> None:
    _beat(fake, "agent-muse-2", "a1b2c3d4", "idle", NOW - timedelta(seconds=30))
    _lease(fake, 11, "agent-muse-2", "a1b2c3d4", NOW - timedelta(minutes=2))
    result = collect_workers(fake, NOW)
    assert result["workers"][0]["state"] == "working"
    assert result["workers"][0]["has_live_lease"] is True


def test_workers_blocked_and_failed_win_over_lease(fake: FakeGitHub) -> None:
    _beat(fake, "agent-muse-2", "a1b2c3d4", "blocked", NOW - timedelta(seconds=30))
    _beat(fake, "agent-muse-3", "a1b2c3d4", "failed", NOW - timedelta(seconds=30))
    _lease(fake, 11, "agent-muse-2", "a1b2c3d4", NOW - timedelta(minutes=2))
    _lease(fake, 12, "agent-muse-3", "a1b2c3d4", NOW - timedelta(minutes=2))
    result = collect_workers(fake, NOW)
    states = {w["agent"]: w["state"] for w in result["workers"]}
    assert states == {"agent-muse-2": "blocked", "agent-muse-3": "failed"}


def test_workers_linux_hex_identity_matches_lease(fake: FakeGitHub) -> None:
    """Linux Worker：心跳用人类 instance 名，租约用 hex；两者要归一到同一人。"""
    instance = "worker-1"
    _beat(fake, "agent-muse-2", instance, "idle", NOW - timedelta(seconds=30))
    _lease(
        fake,
        11,
        "agent-muse-2",
        ref_heartbeat.ref_instance_id(instance),
        NOW - timedelta(minutes=2),
    )
    result = collect_workers(fake, NOW)
    assert result["workers"][0]["state"] == "working"


def test_workers_lease_without_beat_is_not_listed(fake: FakeGitHub) -> None:
    """只有租约、没有心跳 beat 的身份不凭空造出一个 Worker（与 list_workers 一致）。"""
    _lease(fake, 11, "agent-muse-2", "a1b2c3d4", NOW - timedelta(minutes=2))
    result = collect_workers(fake, NOW)
    assert result["workers"] == []


# --------------------------------------------------------------------- 任务指标
def _seed_tasks(fake: FakeGitHub) -> None:
    # #10 未认领：创建于 2 小时前，等 2 小时。
    fake.seed_issue(
        10,
        labels=("state:todo", "agent:muse-2"),
        created_at=_iso(NOW - timedelta(hours=2)),
    )
    # #11 执行中：创建 5 小时前，3 小时前认领 → 等 2 小时，跑 3 小时。
    fake.seed_issue(
        11,
        labels=("state:claimed", "agent:muse-3"),
        created_at=_iso(NOW - timedelta(hours=5)),
    )
    fake.seed_events(11, [_labeled(11, "state:claimed", NOW - timedelta(hours=3), 1)])
    # #12 已关闭：创建 10 小时前，8 小时前认领，1 小时前关闭 → 等 2 小时，跑 7 小时。
    fake.seed_issue(
        12,
        labels=("state:merged", "agent:muse-2"),
        state="closed",
        created_at=_iso(NOW - timedelta(hours=10)),
        closed_at=_iso(NOW - timedelta(hours=1)),
    )
    fake.seed_events(12, [_labeled(12, "state:claimed", NOW - timedelta(hours=8), 2)])
    # #13 曾 QA 失败：6 小时前认领，4 小时前贴 qa-failed，1 小时前关闭 → 恢复 3 小时。
    fake.seed_issue(
        13,
        labels=("state:merged", "agent:muse-4"),
        state="closed",
        created_at=_iso(NOW - timedelta(hours=7)),
        closed_at=_iso(NOW - timedelta(hours=1)),
    )
    fake.seed_events(
        13,
        [
            _labeled(13, "state:claimed", NOW - timedelta(hours=6), 3),
            _labeled(13, "state:qa-failed", NOW - timedelta(hours=4), 4),
        ],
    )
    # 非任务 Issue（无 agent 标签）不应计入。
    fake.seed_issue(14, labels=("state:todo",))


def test_tasks_classification_and_times(fake: FakeGitHub) -> None:
    _seed_tasks(fake)
    result = collect_tasks(fake, NOW)
    assert result["total"] == 4
    assert result["unclaimed"] == 1
    assert result["running"] == 1
    assert result["claimed_ever"] == 3
    assert result["qa_failed_ever"] == 1
    assert result["median_wait"] == 2 * 3600
    # 已关闭且认领过的任务：#12 跑 7 小时，#13 跑 5 小时，中位 6 小时。
    assert result["median_exec"] == 6 * 3600
    assert result["median_recovery"] == 3 * 3600
    assert result["longest_waiter"]["number"] == 10
    assert result["longest_running"]["number"] == 11


def test_tasks_blocked_counts(fake: FakeGitHub) -> None:
    fake.seed_issue(10, labels=("state:blocked", "agent:muse-2"))
    fake.seed_issue(11, labels=("state:qa-failed", "agent:muse-3"))
    result = collect_tasks(fake, NOW)
    assert result["blocked"] == 1
    assert result["qa_failed_now"] == 1
    assert result["by_state"]["state:blocked"] == 1


# --------------------------------------------------------------------- PR 指标
def test_prs_review_smoke_merge_and_link(fake: FakeGitHub) -> None:
    sha = "a" * 40
    fake.seed_pull(
        21, sha, merged=False, body="Closes #11", head_ref="agent/task-11-x"
    )
    fake.seed_comments(
        21,
        [
            f"<!-- review-result: pr-21 sha-{sha} reviewer-muse-1 verdict=passed -->",
            f"<!-- smoke-result: pr-21 sha-{sha} result=passed -->",
        ],
    )
    fake.seed_pull(22, "b" * 40, merged=False, body="task: #12")
    fake.seed_comments(
        22,
        [
            f"<!-- review-result: pr-22 sha-{'b' * 40} reviewer-muse-1 verdict=changes-requested -->",
            f"<!-- smoke-result: pr-22 sha-{'b' * 40} result=failed -->",
            "<!-- auto-fix: for-issue-12 root-issue-12 round-1 sha-bbbb -->",
        ],
    )
    fake.seed_pull(
        20,
        "c" * 40,
        merged=True,
        merged_by="github-actions[bot]",
        merged_at=_iso(NOW - timedelta(hours=2)),
        body="Closes #12",
    )
    fake.seed_comments(
        20,
        [f"<!-- review-result: pr-20 sha-{'c' * 40} reviewer-muse-1 verdict=passed -->"],
    )
    fake.seed_pull(
        19,
        "d" * 40,
        merged=True,
        merged_by="Jouwala",
        merged_at=_iso(NOW - timedelta(days=40)),
        body="",
    )
    result = collect_prs(fake, NOW)
    assert result["open_count"] == 2
    assert result["reviewed_count"] == 3  # open 2 + 窗口内合入 1（#19 超 30 天不计）
    assert result["rejected_count"] == 1
    assert result["auto_merged"] == 1
    assert result["manual_merged"] == 1
    assert result["auto_fix_rounds"] == 1
    assert result["by_issue"][11]["review"]["verdict"] == "passed"
    assert result["by_issue"][11]["smoke"]["result"] == "passed"
    assert result["by_issue"][12]["review"]["verdict"] == "changes-requested"


def test_rates_empty_show_dash_not_zero(fake: FakeGitHub) -> None:
    workers = collect_workers(fake, NOW)
    tasks = collect_tasks(fake, NOW)
    prs = collect_prs(fake, NOW)
    body = render_status(workers, tasks, prs, NOW)
    assert "QA 失败率：—" in body
    assert "审核退回率：—" in body
    assert "无任务。" in body
    assert "无进行中的 PR。" in body


def test_render_contains_sections_and_task_row(fake: FakeGitHub) -> None:
    _seed_tasks(fake)
    sha = "a" * 40
    fake.seed_pull(21, sha, merged=False, body="Closes #11")
    fake.seed_comments(
        21,
        [f"<!-- review-result: pr-21 sha-{sha} reviewer-muse-1 verdict=passed -->"],
    )
    workers = collect_workers(fake, NOW)
    tasks = collect_tasks(fake, NOW)
    prs = collect_prs(fake, NOW)
    body = render_status(workers, tasks, prs, NOW)
    assert body.startswith(STATUS_MARKER)
    for section in ("## 在线 Muse", "## 任务总览", "## 质量", "## 进行中的 PR", "## 每任务行", "## 口径说明"):
        assert section in body
    # #11 行含审核结论与等待/执行耗时。
    row = [line for line in body.splitlines() if line.startswith("| #11 |")][0]
    assert "passed（muse-1）" in row
    assert "2小时0分" in row
    assert "3小时0分" in row


# --------------------------------------------------------------------- 就地更新流程
def test_update_creates_then_unchanged_then_updated(fake: FakeGitHub) -> None:
    assert find_status_issue(fake, REPO) is None
    result = update_status(fake, REPO, now=NOW, log=lambda *a: None)
    assert result["action"] in ("created", "updated")
    number = ensure_status_issue(fake, REPO)
    assert find_status_issue(fake, REPO) == number
    assert STATUS_TITLE in fake.issues[number]["title"]

    def patch_count() -> int:
        return sum(
            1
            for method, url in fake.requests
            if method == "PATCH" and f"/issues/{number}" in url
        )

    first_patches = patch_count()
    result = update_status(fake, REPO, now=NOW, log=lambda *a: None)
    assert result["action"] == "unchanged"
    assert patch_count() == first_patches  # 正文相同，不 PATCH

    fake.seed_issue(10, labels=("state:todo", "agent:muse-2"))
    result = update_status(fake, REPO, now=NOW, log=lambda *a: None)
    assert result["action"] == "updated"
    assert patch_count() == first_patches + 1


def test_cli_update_ok_and_config_error(fake: FakeGitHub) -> None:
    env = {"REPO": REPO, "GH_TOKEN": "test-token"}
    assert status_view.main(["update"], env=env, client=fake) == 0
    assert status_view.main(["update"], env={}, client=fake) == 2
