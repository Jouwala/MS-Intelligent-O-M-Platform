"""QA 触发：head SHA 去重并发只登记一次、失败回滚、分页、安全阀。"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from dispatch.github_client import GitHubAPIError, GitHubTransportError
from dispatch.qa_dispatch import WebhookError, trigger_qa

from dispatch.tests.fake_github import HEAD_SHA, FakeGitHub

QA_REF_PREFIX = "refs/heads/dispatch/qa-trigger/"
WEBHOOK_URL = "https://hook.test/smoke"
WEBHOOK_KEY = "test-key"


class WebhookRecorder:
    """记录 webhook 调用；responses 里的元素依次作为返回值或抛出的异常。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []

    def __call__(self, url: str, key: str, payload: dict[str, Any]) -> int:
        assert url == WEBHOOK_URL
        assert key == WEBHOOK_KEY
        self.calls.append(payload)
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return int(response)
        return 200


@pytest.fixture()
def open_pr(fake: FakeGitHub) -> dict[str, Any]:
    return fake.seed_pull(
        31,
        HEAD_SHA,
        merged=False,
        labels=("agent:muse-2",),
        body="Closes #1",
    )


def _trigger(fake: FakeGitHub, recorder: WebhookRecorder, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "event": "workflow_dispatch",
        "pr_number": 31,
        "webhook_url": WEBHOOK_URL,
        "webhook_key": WEBHOOK_KEY,
        "run_url": "https://github.test/actions/runs/1",
        "post_webhook": recorder,
    }
    params.update(overrides)
    return trigger_qa(fake, fake.repo, **params)


def test_trigger_once_then_idempotent(fake: FakeGitHub, open_pr) -> None:
    recorder = WebhookRecorder()
    first = _trigger(fake, recorder)
    assert first["status"] == "triggered"
    assert first["pr_number"] == 31
    assert first["head_sha"] == HEAD_SHA
    assert first["issue_number"] == 1
    assert len(recorder.calls) == 1
    payload = recorder.calls[0]
    assert payload["event"] == "qa_requested"
    assert payload["head_sha"] == HEAD_SHA
    assert payload["pr_number"] == 31
    assert len(fake.refs_with_prefix(QA_REF_PREFIX)) == 1

    second = _trigger(fake, recorder)
    assert second["status"] == "skip"
    assert len(recorder.calls) == 1
    assert len(fake.refs_with_prefix(QA_REF_PREFIX)) == 1


def test_concurrent_same_head_sha_triggers_once(fake: FakeGitHub, open_pr) -> None:
    recorder = WebhookRecorder()
    barrier = threading.Barrier(2)
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            results.append(_trigger(fake, recorder))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(result["status"] for result in results) == ["skip", "triggered"]
    assert len(recorder.calls) == 1
    assert len(fake.refs_with_prefix(QA_REF_PREFIX)) == 1


def test_draft_pr_skipped(fake: FakeGitHub) -> None:
    fake.seed_pull(31, HEAD_SHA, merged=False, draft=True)
    recorder = WebhookRecorder()
    assert _trigger(fake, recorder)["status"] == "skip"
    assert recorder.calls == []


def test_qa_ready_label_skipped(fake: FakeGitHub) -> None:
    fake.seed_pull(31, HEAD_SHA, merged=False, labels=("state:qa-ready",))
    recorder = WebhookRecorder()
    result = _trigger(fake, recorder)
    assert result["status"] == "skip"
    assert "qa-ready" in result["reason"]
    assert recorder.calls == []


def test_smoke_passed_and_registered_skipped(fake: FakeGitHub, open_pr) -> None:
    recorder = WebhookRecorder()
    assert _trigger(fake, recorder)["status"] == "triggered"
    fake.pulls[31]["labels"].append({"name": "smoke-passed"})
    result = _trigger(fake, recorder)
    assert result["status"] == "skip"
    assert "smoke-passed" in result["reason"]


def test_webhook_http_failure_rolls_back_for_retry(fake: FakeGitHub, open_pr) -> None:
    recorder = WebhookRecorder()
    recorder.responses = [500]
    with pytest.raises(WebhookError):
        _trigger(fake, recorder)
    assert fake.refs_with_prefix(QA_REF_PREFIX) == []
    assert _trigger(fake, recorder)["status"] == "triggered"
    assert len(recorder.calls) == 2


def test_webhook_timeout_keeps_claim_to_avoid_duplicate(fake: FakeGitHub, open_pr) -> None:
    recorder = WebhookRecorder()
    recorder.responses = [GitHubTransportError("模拟超时")]
    with pytest.raises(GitHubTransportError):
        _trigger(fake, recorder)
    assert len(fake.refs_with_prefix(QA_REF_PREFIX)) == 1
    # 结果未知时宁可 skip，也不重复触发同一 SHA。
    assert _trigger(fake, recorder)["status"] == "skip"
    assert len(recorder.calls) == 1


def test_missing_webhook_secret_is_explicit(fake: FakeGitHub, open_pr) -> None:
    with pytest.raises(WebhookError):
        _trigger(fake, WebhookRecorder(), webhook_url="")
    with pytest.raises(WebhookError):
        _trigger(fake, WebhookRecorder(), webhook_key="")


def test_workflow_run_finds_pr_on_later_page(fake: FakeGitHub) -> None:
    for number in range(1, 120):
        fake.seed_pull(number, f"{number:040d}", merged=False)
    fake.seed_pull(115, HEAD_SHA, merged=False, body="Closes #1")
    recorder = WebhookRecorder()
    result = _trigger(fake, recorder, event="workflow_run", pr_number=0, head_sha=HEAD_SHA)
    assert result["status"] == "triggered"
    assert result["pr_number"] == 115
    assert len(recorder.calls) == 1


def test_workflow_run_without_matching_pr_skips(fake: FakeGitHub) -> None:
    fake.seed_pull(1, "9" * 40, merged=False)
    recorder = WebhookRecorder()
    result = _trigger(
        fake, recorder, event="workflow_run", pr_number=0, head_sha=HEAD_SHA
    )
    assert result["status"] == "skip"
    assert recorder.calls == []


def test_pull_request_event_path(fake: FakeGitHub, open_pr) -> None:
    recorder = WebhookRecorder()
    result = _trigger(fake, recorder, event="pull_request", pr_number=31)
    assert result["status"] == "triggered"


def test_missing_head_sha_is_explicit(fake: FakeGitHub) -> None:
    fake.seed_pull(31, "", merged=False)
    with pytest.raises(GitHubAPIError):
        _trigger(fake, WebhookRecorder())
