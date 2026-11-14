"""T-REF-STORE：`RestRefStore` 对**真实本地 HTTP 服务**的行为（不是 mock）。

断言全部落在 fake server 的真实 refs 状态与请求计数上：创建唯一性（422）、
POST 不重试、传输异常后 GET 消歧、GET 有限重试、matching-refs 分页与段边界、
删除幂等、锚点分支 SHA。fake server 用标准库 http.server，客户端走真实 urllib，
因此这里覆盖的是真正的网络/状态码/分页路径。
"""

from __future__ import annotations

import pytest

from scheduler.github_refs import GitHubRefError
from scheduler.ref_api import RefInfo

ANCHOR = "a" * 40


def hwm_ref(issue: int, token: int) -> str:
    return f"refs/heads/agent-hwm/issue-{issue}/{token:08d}"


def test_create_true_then_422_false_and_counts(fake_github, ref_store):
    ref = hwm_ref(1, 1)
    assert ref_store.create(ref, ANCHOR) is True
    assert fake_github.refs_snapshot()[ref] == ANCHOR
    assert ref_store.create(ref, ANCHOR) is False
    # 每次都发了一次 POST（没有盲目重试）
    assert fake_github.create_attempts_of(ref) == 2


def test_create_422_without_ref_is_a_real_error(fake_github, ref_store):
    """422 但 ref 不存在 = 锚点 SHA 无效等真实错误，不能静默当「已存在」。"""
    ref = hwm_ref(1, 2)
    with pytest.raises(GitHubRefError) as info:
        ref_store.create(ref, "f" * 40)
    assert info.value.status == 422
    assert ref not in fake_github.refs_snapshot()


def test_post_timeout_after_apply_is_loser_false_and_not_retried(fake_github, ref_store):
    """服务端已应用、客户端断连：先 GET 消歧 → 按失败者 False（安全侧）。"""
    ref = hwm_ref(1, 3)
    fake_github.fail_next(
        "/git/refs", status=None, transport_abort=True, apply_first=True, method="POST"
    )
    assert ref_store.create(ref, ANCHOR) is False
    assert ref in fake_github.refs_snapshot()  # 服务端确实已创建
    assert fake_github.create_attempts_of(ref) == 1  # POST 只尝试一次


def test_post_transport_error_before_apply_raises_and_not_retried(fake_github, ref_store):
    ref = hwm_ref(1, 4)
    fake_github.fail_next(
        "/git/refs", status=None, transport_abort=True, apply_first=False, method="POST"
    )
    with pytest.raises(GitHubRefError):
        ref_store.create(ref, ANCHOR)
    assert ref not in fake_github.refs_snapshot()
    assert fake_github.create_attempts_of(ref) == 1


def test_post_500_is_not_retried(fake_github, ref_store):
    ref = hwm_ref(1, 5)
    fake_github.fail_next("/git/refs", status=500, times=1, method="POST")
    with pytest.raises(GitHubRefError) as info:
        ref_store.create(ref, ANCHOR)
    assert info.value.status == 500
    assert fake_github.create_attempts_of(ref) == 1


def test_get_404_returns_none(fake_github, ref_store):
    assert ref_store.get(hwm_ref(9, 1)) is None


def test_get_retries_transient_5xx_then_succeeds(fake_github, ref_store):
    fake_github.fail_next("/git/ref/heads/main", status=503, times=1)
    assert ref_store.branch_sha("main") == ANCHOR


def test_get_retries_429_then_succeeds(fake_github, ref_store):
    fake_github.fail_next("/git/ref/heads/main", status=429, times=1)
    assert ref_store.branch_sha("main") == ANCHOR


def test_get_retry_exhausted_raises(fake_github, ref_store):
    fake_github.fail_next("/git/ref/heads/main", status=500, times=10)
    with pytest.raises(GitHubRefError) as info:
        ref_store.branch_sha("main")
    assert info.value.status == 500


def test_get_returns_ref_info(fake_github, ref_store):
    ref = hwm_ref(2, 1)
    assert ref_store.create(ref, ANCHOR) is True
    info = ref_store.get(ref)
    assert isinstance(info, RefInfo)
    assert info.ref == ref
    assert info.sha == ANCHOR


def test_delete_missing_is_false_and_is_idempotent(fake_github, ref_store):
    assert ref_store.delete(hwm_ref(2, 9)) is False
    ref = hwm_ref(2, 2)
    assert ref_store.create(ref, ANCHOR) is True
    assert ref_store.delete(ref) is True
    assert ref not in fake_github.refs_snapshot()
    assert ref_store.delete(ref) is False


def test_delete_timeout_after_apply_returns_true(fake_github, ref_store):
    ref = hwm_ref(2, 3)
    fake_github.seed_ref(ref)
    # DELETE 可重试：两次都断连 → 重试用尽后 GET 消歧；服务端已删 → 视为成功
    fake_github.fail_next(
        "/git/refs", status=None, transport_abort=True, apply_first=True, method="DELETE", times=2
    )
    assert ref_store.delete(ref) is True
    assert ref not in fake_github.refs_snapshot()


def test_delete_transport_error_before_apply_raises(fake_github, ref_store):
    ref = hwm_ref(2, 4)
    fake_github.seed_ref(ref)
    fake_github.fail_next(
        "/git/refs", status=None, transport_abort=True, apply_first=False, method="DELETE", times=2
    )
    with pytest.raises(GitHubRefError):
        ref_store.delete(ref)
    assert ref in fake_github.refs_snapshot()  # 没有假装删成功


def test_list_follows_link_pagination(fake_github, ref_store):
    for token in range(1, 151):
        fake_github.seed_ref(hwm_ref(2, token))
    refs = ref_store.list("refs/heads/agent-hwm/issue-2/")
    assert len(refs) == 150
    assert len({item.ref for item in refs}) == 150
    # 两页：第 2 页恰好被请求一次（Link 分页真的生效）
    assert fake_github.page_calls_of("git/matching-refs", 2) == 1


def test_list_empty_prefix_returns_empty(fake_github, ref_store):
    assert ref_store.list("refs/heads/agent-hwm/issue-77/") == []


def test_list_respects_segment_boundary_between_issue_1_and_issue_10(fake_github, ref_store):
    """GitHub matching-refs 是字符串前缀：客户端必须按 `/` 段边界再过滤一次。"""
    fake_github.seed_ref(hwm_ref(1, 1))
    fake_github.seed_ref(hwm_ref(10, 1))
    refs = ref_store.list("refs/heads/agent-hwm/issue-1/")
    assert [item.ref for item in refs] == [hwm_ref(1, 1)]


def test_branch_sha_missing_branch_raises(fake_github, ref_store):
    with pytest.raises(GitHubRefError) as info:
        ref_store.branch_sha("release-does-not-exist")
    assert info.value.status == 404
