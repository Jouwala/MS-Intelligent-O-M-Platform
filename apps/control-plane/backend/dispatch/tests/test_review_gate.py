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


# ------------------------------------------------------- 复审发现 1：verdict 边界
def test_verdict_with_suffix_is_not_parsed_as_passed() -> None:
    """`verdict=passedXYZ` / `passed.` / `passed-foo` 绝不能当成 passed 放行。

    verdict 后只允许空白、`--`（标记结尾）或串尾。
    """
    base = f"<!-- review-result: pr-123 sha-{SHA_A} reviewer-muse-1 verdict="
    for bad in ("passedXYZ -->", "passed. -->", "passed-foo -->",
                "passedx -->", "changes-requestedx -->"):
        assert parse_review_markers([base + bad]) == []
        passed, _ = review_gate_passes([base + bad], 123, SHA_A)
        assert passed is False
    # 合法收尾仍能解析：带空格的标记结尾、无空格的标记结尾
    for good in ("passed -->", "passed-->", "blocked -->"):
        assert len(parse_review_markers([base + good])) == 1


def test_format_marker_rejects_bad_pr_number() -> None:
    with pytest.raises(ValueError):
        format_review_marker(0, SHA_A, "muse-1", "passed")
    with pytest.raises(ValueError):
        format_review_marker(-3, SHA_A, "muse-1", "passed")


# ------------------------------------------- 复审发现 3/4：标记位置与同一评论多条
def test_marker_mid_body_is_accepted() -> None:
    """标记不必在首行（与 smoke-result 的“取最后一条”口径一致）。"""
    body = "先说两句人话。\n\n" + _marker() + "\n\n结论如上。"
    assert len(parse_review_markers([body])) == 1


def test_last_marker_in_same_comment_wins() -> None:
    """同一评论先贴 passed、后贴 blocked：blocked 说了算（安全方向）。"""
    body = (
        _marker(sha=SHA_A, verdict="passed")
        + "\n\n改判：\n\n"
        + _marker(sha=SHA_A, verdict="blocked")
    )
    markers = parse_review_markers([body])
    assert len(markers) == 1
    assert markers[0]["verdict"] == "blocked"
    passed, _ = review_gate_passes([body], 123, SHA_A)
    assert passed is False


# ------------------------------------------------------- 复审发现 6：分页拉取
def test_fetch_comment_bodies_paginates(monkeypatch) -> None:
    """两页评论（第 1 页满 100 条）都要拉到，顺序即时间顺序。"""
    import io
    import json as _json
    import urllib.request as _request

    from dispatch import review_gate

    page1 = [{"body": f"第 1 页第 {i} 条"} for i in range(100)]
    page2 = [{"body": "第 2 页唯一一条"}]

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return _json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    seen_urls: list[str] = []

    def _fake_urlopen(request, timeout=30):
        seen_urls.append(request.full_url)
        if "page=1" in request.full_url:
            return _FakeResponse(page1)
        return _FakeResponse(page2)

    monkeypatch.setattr(_request, "urlopen", _fake_urlopen)
    bodies = review_gate.fetch_comment_bodies("Jouwala/repo", 123, "tok")
    assert len(bodies) == 101
    assert bodies[0] == "第 1 页第 0 条"
    assert bodies[-1] == "第 2 页唯一一条"
    assert any("page=2" in url for url in seen_urls)
