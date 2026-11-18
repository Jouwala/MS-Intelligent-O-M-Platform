"""独立代码审核记录门禁（review_gate.py）的判定规则测试。"""

from __future__ import annotations

import pytest

from dispatch.review_gate import (
    format_review_marker,
    latest_review,
    parse_review_markers,
    review_gate_passes,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _marker(pr: int = 123, sha: str = SHA_A, reviewer: str = "muse-1",
            verdict: str = "passed") -> str:
    return format_review_marker(pr, sha, reviewer, verdict)


def _body(marker: str, extra: str = "审核通过，逻辑无问题。") -> str:
    return f"{marker}\n\n## 独立代码审核：通过\n\n{extra}\n"


# ------------------------------------------------------------------ 标记格式
def test_format_marker_roundtrip() -> None:
    marker = _marker()
    assert marker == (
        f"<!-- review-result: pr-123 sha-{SHA_A} "
        "reviewer-muse-1 verdict=passed -->"
    )
    parsed = parse_review_markers([marker])
    assert parsed == [
        {"pr": 123, "sha": SHA_A, "reviewer": "muse-1", "verdict": "passed"}
    ]


def test_format_marker_rejects_bad_verdict_and_sha() -> None:
    with pytest.raises(ValueError):
        format_review_marker(123, SHA_A, "muse-1", "ok")
    with pytest.raises(ValueError):
        format_review_marker(123, "abc", "muse-1", "passed")
    with pytest.raises(ValueError):
        format_review_marker(123, SHA_A, "muse 1", "passed")


def test_malformed_marker_is_ignored_not_fatal() -> None:
    bodies = [
        "<!-- review-result: pr-123 sha-xyz verdict=passed -->",
        "<!-- review-result: pr-123 sha-" + SHA_A + " verdict=maybe -->",
        _body(_marker()),
    ]
    assert len(parse_review_markers(bodies)) == 1
    assert latest_review(bodies)["verdict"] == "passed"


# ------------------------------------------------------------------ 门禁判定
def test_passed_on_current_head_passes() -> None:
    passed, reason = review_gate_passes([_body(_marker())], 123, SHA_A)
    assert passed is True
    assert "muse-1" in reason


def test_no_marker_fails() -> None:
    passed, reason = review_gate_passes(["只有正文，没有标记"], 123, SHA_A)
    assert passed is False
    assert "未审" in reason


def test_passed_on_old_head_fails_after_new_commit() -> None:
    """新提交自动使旧审核失效（P2-1 核心规则）。"""
    passed, reason = review_gate_passes([_body(_marker(sha=SHA_A))], 123, SHA_B)
    assert passed is False
    assert "已失效" in reason


def test_later_changes_requested_overrides_earlier_passed() -> None:
    """审核者事后改判：最后一条标记说了算。"""
    bodies = [
        _body(_marker(sha=SHA_A, verdict="passed")),
        _body(_marker(sha=SHA_A, verdict="changes-requested"),
              "发现一处竞态，须改。"),
    ]
    passed, reason = review_gate_passes(bodies, 123, SHA_A)
    assert passed is False
    assert "changes-requested" in reason


def test_later_blocked_overrides_earlier_passed() -> None:
    bodies = [
        _body(_marker(sha=SHA_A, verdict="passed")),
        _body(_marker(sha=SHA_A, verdict="blocked"), "涉及生产库，须用户定。"),
    ]
    passed, _ = review_gate_passes(bodies, 123, SHA_A)
    assert passed is False


def test_repassed_after_fix_on_new_head_passes() -> None:
    """改完推新提交、重新审核通过：放行。"""
    bodies = [
        _body(_marker(sha=SHA_A, verdict="passed")),
        _body(_marker(sha=SHA_B, verdict="changes-requested"), "须改。"),
        _body(_marker(sha=SHA_C, verdict="passed"), "已改好，重新审过。"),
    ]
    passed, _ = review_gate_passes(bodies, 123, SHA_C)
    assert passed is True


def test_marker_for_another_pr_does_not_count() -> None:
    passed, reason = review_gate_passes([_body(_marker(pr=999))], 123, SHA_A)
    assert passed is False
    assert "PR #999" in reason


def test_reviewer_name_variants_parse() -> None:
    for reviewer in ("muse-1", "muse-4", "lead-agent", "muse_2"):
        marker = _marker(reviewer=reviewer)
        assert parse_review_markers([marker])[0]["reviewer"] == reviewer
