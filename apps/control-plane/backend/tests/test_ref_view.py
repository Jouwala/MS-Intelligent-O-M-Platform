"""PR-1：`ref_view.py` 纯函数单测（评论标记解析 + 状态派生），无网络无状态。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scheduler import ref_api
from scheduler.api import (
    CANCEL_REQUESTED,
    CANCELLED,
    CLAIMED,
    COMPLETED,
    EXPIRED,
    QUEUED,
    RETRYABLE,
    RUNNING,
    SchedulerError,
)
from scheduler.ref_view import (
    build_view,
    derive_state,
    issue_number_for,
    parse_completions,
    parse_state_markers,
    state_comment_body,
    task_id_of,
    terminal_state_of,
)

TS = datetime(2026, 11, 14, 2, 0, 0, tzinfo=timezone.utc)
NOW_MS = int(TS.timestamp() * 1000)


def test_issue_number_parsing():
    assert issue_number_for("issue-42") == 42
    assert task_id_of(42) == "issue-42"
    with pytest.raises(SchedulerError):
        issue_number_for("task-42")


def test_state_marker_roundtrip_and_single_line():
    body = state_comment_body(
        issue_number=7,
        token=3,
        state=RUNNING,
        timestamp=TS,
        error_code="E_HOOK",
        error_message="第一行\n第二行",
        reason="测试\n原因",
    )
    markers = parse_state_markers([body])
    assert len(markers) == 1
    marker = markers[0]
    assert (marker.state, marker.token) == (RUNNING, 3)
    assert marker.at == TS
    assert marker.error_code == "E_HOOK"
    assert "\n" not in marker.error_message and "第二行" in marker.error_message
    assert terminal_state_of(markers) is None


def test_terminal_state_of_looks_at_latest_marker():
    bodies = [
        state_comment_body(issue_number=1, token=1, state=RUNNING, timestamp=TS),
        state_comment_body(issue_number=1, token=1, state=COMPLETED, timestamp=TS),
    ]
    markers = parse_state_markers(bodies)
    assert terminal_state_of(markers) == COMPLETED
    # 最新一条不是终态 → 不算终态（旧终态不会压住新标记）
    assert terminal_state_of(parse_state_markers(bodies[::-1])) is None


def test_parse_completions_from_contract_body():
    body = ref_api.completion_comment_body(
        issue_number=5,
        token=9,
        attempt_id="abc123",
        terminal_status="failed",
        idempotency_key="key-1",
        summary="失败",
        timestamp="2026-11-14T02:00:00Z",
        agent_id="muse-2",
    )
    records = parse_completions([body])
    assert len(records) == 1
    record = records[0]
    assert record.idempotency_key == "key-1"
    assert record.token == 9
    assert record.attempt_id == "abc123"
    assert record.status == "failed"
    assert record.at == TS


def _lease(token: int, epoch_ms: int = NOW_MS, instance: str = "1a2b3c4d") -> ref_api.LeaseRef:
    ref = ref_api.lease_ref(1, token, epoch_ms, "muse-2", instance, "a1b2c3d4e5f6")
    parsed = ref_api.parse_lease(ref)
    assert parsed is not None
    return parsed


def test_derive_state_priority_table():
    live = _lease(1)
    # 完成标记优先于一切
    completion = [{"idempotency_key": "k", "token": 1, "status": COMPLETED}]
    from scheduler.ref_view import CompletionRecord, StateMarker

    completion_records = [CompletionRecord(**completion[0])]
    assert derive_state(
        max_token=1, live=live, has_expired_lease=False,
        completions=completion_records, markers=[],
    ) == (COMPLETED, COMPLETED)
    # 存活租约 + running 标记
    markers = [StateMarker(state=RUNNING, token=1, at=TS)]
    assert derive_state(
        max_token=1, live=live, has_expired_lease=False, completions=[], markers=markers
    ) == (RUNNING, None)
    # 存活租约 + 无标记 → claimed
    assert derive_state(
        max_token=1, live=live, has_expired_lease=False, completions=[], markers=[]
    ) == (CLAIMED, None)
    # 过期未回收 → expired
    assert derive_state(
        max_token=1, live=None, has_expired_lease=True, completions=[], markers=[]
    ) == (EXPIRED, None)
    # 租约已被回收 + 旧 claimed 标记 → retryable（必须可重新认领）
    assert derive_state(
        max_token=1, live=None, has_expired_lease=False, completions=[],
        markers=[StateMarker(state=CLAIMED, token=1, at=TS)],
    ) == (RETRYABLE, None)
    # 取消请求 + 租约已不在 → cancelled（终态，不可复活）
    assert derive_state(
        max_token=1, live=None, has_expired_lease=False, completions=[],
        markers=[StateMarker(state=CANCEL_REQUESTED, token=1, at=TS)],
    ) == (CANCELLED, CANCELLED)
    # 什么都没发生 → queued
    assert derive_state(
        max_token=0, live=None, has_expired_lease=False, completions=[], markers=[]
    ) == (QUEUED, None)


def test_build_view_fills_lease_identity_and_times():
    lease = _lease(2)
    view = build_view(
        task_id="issue-1",
        issue_number=1,
        max_token=2,
        lease_refs=[lease],
        live=lease,
        comment_bodies=[],
        repo="o/r",
        title="标题",
        scopes=("a.py",),
    )
    assert view.state == CLAIMED
    assert (view.attempt_id, view.agent_id, view.instance_id) == (
        "a1b2c3d4e5f6", "muse-2", "1a2b3c4d"
    )
    assert view.claimed_at == datetime.fromtimestamp(NOW_MS / 1000, timezone.utc)
    assert view.max_token == 2
    assert view.scopes == ("a.py",)
    # 租约过期但尚未被 Reaper 清掉：仍显示最后一次状态（状态机不因过期改变）
    expired_view = build_view(
        task_id="issue-1",
        issue_number=1,
        max_token=2,
        lease_refs=[lease],
        live=None,
        comment_bodies=[state_comment_body(issue_number=1, token=2, state=RUNNING, timestamp=TS)],
        repo="o/r",
    )
    assert expired_view.state == RUNNING
    # 过期且没有状态标记 → expired（历史数据兜底）
    bare = build_view(
        task_id="issue-1",
        issue_number=1,
        max_token=2,
        lease_refs=[lease],
        live=None,
        comment_bodies=[],
        repo="o/r",
    )
    assert bare.state == EXPIRED


def test_helpful_constants_are_stable():
    # 防止误改：这些字面量是 ref 名前缀与派生规则的约定
    assert ref_api.LEASE_ROOT.endswith("/agent-lease")
    assert timedelta(minutes=10) == ref_api.DEFAULT_REF_LEASE_TTL
