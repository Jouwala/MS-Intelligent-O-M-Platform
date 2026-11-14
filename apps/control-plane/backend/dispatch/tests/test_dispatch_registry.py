"""git ref CAS 登记：稳定键、唯一性、并发赢家、422 消歧、对账查询。"""

from __future__ import annotations

import threading

import pytest

from dispatch.dispatch_registry import (
    SOURCE_AUTO_DISPATCH,
    SOURCE_QA,
    DispatchRegistry,
    dispatch_ref,
    make_dispatch_id,
    parse_dispatch_id,
)
from dispatch.github_client import GitHubAPIError

from dispatch.tests.fake_github import HEAD_SHA, MAIN_SHA, MERGE_SHA, FakeGitHub


def test_dispatch_id_is_stable_and_scoped() -> None:
    first = make_dispatch_id("acme/demo", "issue", 7, MERGE_SHA, SOURCE_AUTO_DISPATCH)
    again = make_dispatch_id("acme/demo", "issue", 7, MERGE_SHA, SOURCE_AUTO_DISPATCH)
    assert first == again
    assert first.startswith("issue-7-")
    assert make_dispatch_id("acme/demo", "issue", 8, MERGE_SHA, SOURCE_AUTO_DISPATCH) != first
    assert make_dispatch_id("acme/demo", "pr", 7, MERGE_SHA, SOURCE_AUTO_DISPATCH) != first
    assert make_dispatch_id("acme/demo", "issue", 7, HEAD_SHA, SOURCE_AUTO_DISPATCH) != first
    assert make_dispatch_id("acme/demo", "issue", 7, MERGE_SHA, SOURCE_QA) != first
    assert make_dispatch_id("other/repo", "issue", 7, MERGE_SHA, SOURCE_AUTO_DISPATCH) != first


def test_ref_name_and_parse_roundtrip() -> None:
    dispatch_id = make_dispatch_id("acme/demo", "issue", 42, MERGE_SHA, SOURCE_AUTO_DISPATCH)
    ref = dispatch_ref(dispatch_id, SOURCE_AUTO_DISPATCH)
    assert ref == f"refs/heads/dispatch/pr-merged/{dispatch_id}"
    parsed = parse_dispatch_id(dispatch_id)
    assert parsed is not None
    assert parsed["kind"] == "issue"
    assert parsed["number"] == 42
    assert parse_dispatch_id("garbage") is None


def test_register_first_wins_second_is_idempotent(fake: FakeGitHub) -> None:
    registry = DispatchRegistry(fake, fake.repo)
    first = registry.register(
        "issue", 7, MERGE_SHA, SOURCE_AUTO_DISPATCH, anchor_sha=MERGE_SHA
    )
    second = registry.register(
        "issue", 7, MERGE_SHA, SOURCE_AUTO_DISPATCH, anchor_sha=MERGE_SHA
    )
    assert first.created is True
    assert second.created is False
    assert first.dispatch_id == second.dispatch_id
    assert len(fake.refs_with_prefix("refs/heads/dispatch/")) == 1


def test_register_concurrent_only_one_winner(fake: FakeGitHub) -> None:
    registry = DispatchRegistry(fake, fake.repo)

    def worker(results: list[bool]) -> None:
        result = registry.register(
            "issue", 7, MERGE_SHA, SOURCE_AUTO_DISPATCH, anchor_sha=MERGE_SHA
        )
        results.append(result.created)

    results: list[bool] = []
    barrier = threading.Barrier(8)

    def run() -> None:
        barrier.wait()
        worker(results)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_422_without_ref_is_a_real_error(fake: FakeGitHub) -> None:
    """422 也可能是锚点 SHA 无效 —— 不能当成“已登记”静默丢分派。"""
    registry = DispatchRegistry(fake, fake.repo)
    fake.inject(
        method="POST",
        path_contains="/git/refs",
        status=422,
        times=5,
        body={"message": "Object does not exist"},
    )
    with pytest.raises(GitHubAPIError) as excinfo:
        registry.register("issue", 7, MERGE_SHA, SOURCE_AUTO_DISPATCH, anchor_sha=MERGE_SHA)
    assert excinfo.value.status == 422


def test_unknown_anchor_sha_is_explicit(fake: FakeGitHub) -> None:
    """假 API 会拒绝不存在的 sha（真实 GitHub 同样 422 Object does not exist）。"""
    registry = DispatchRegistry(fake, fake.repo)
    with pytest.raises(GitHubAPIError):
        registry.register(
            "issue", 7, MERGE_SHA, SOURCE_AUTO_DISPATCH, anchor_sha="f" * 40
        )


def test_anchor_fallback_to_main(fake: FakeGitHub) -> None:
    registry = DispatchRegistry(fake, fake.repo)
    assert registry.resolve_anchor_sha() == MAIN_SHA
    assert registry.resolve_anchor_sha(HEAD_SHA) == HEAD_SHA


def test_release_deletes_ref(fake: FakeGitHub) -> None:
    registry = DispatchRegistry(fake, fake.repo)
    registration = registry.register("pr", 31, HEAD_SHA, SOURCE_QA, anchor_sha=HEAD_SHA)
    assert registry.exists("pr", 31, HEAD_SHA, SOURCE_QA) is True
    assert registry.release(registration.dispatch_id, SOURCE_QA) is True
    assert registry.exists("pr", 31, HEAD_SHA, SOURCE_QA) is False


def test_find_for_trigger_filters_by_sha_and_source(fake: FakeGitHub) -> None:
    registry = DispatchRegistry(fake, fake.repo)
    registry.register("issue", 7, MERGE_SHA, SOURCE_AUTO_DISPATCH, anchor_sha=MERGE_SHA)
    registry.register("issue", 8, HEAD_SHA, SOURCE_AUTO_DISPATCH, anchor_sha=HEAD_SHA)
    registry.register("pr", 31, MERGE_SHA, SOURCE_QA, anchor_sha=MERGE_SHA)

    found = registry.find_for_trigger(MERGE_SHA, SOURCE_AUTO_DISPATCH)
    assert [item["number"] for item in found] == [7]
    assert found[0]["kind"] == "issue"
    assert registry.find_for_trigger(MERGE_SHA, SOURCE_QA)[0]["number"] == 31
    assert registry.find_for_trigger(MAIN_SHA, SOURCE_AUTO_DISPATCH) == []
