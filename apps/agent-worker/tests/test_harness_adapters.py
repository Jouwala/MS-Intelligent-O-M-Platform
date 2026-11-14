"""Harness 统一接口测试（TASK-04）：base.py 公共规则 + grok_webhook.py 驱动。

验收口径（Issue #19）：各驱动通过接口测试；同一幂等键二次 start 不重复
执行；超时 4 小时判 timeout。全部离线：假时钟 + 记录型假传输。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

import _bootstrap

_ROOT = _bootstrap.repo_root()
_ADAPTERS = _ROOT / "apps" / "control-plane" / "backend" / "adapters"
base = _bootstrap._load("harness_base", _ADAPTERS / "base.py")
grok = _bootstrap._load("grok_webhook", _ADAPTERS / "grok_webhook.py")


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class DummyAdapter(base.HarnessAdapter):
    """最小驱动：只验证基类公共规则（幂等 / 超时 / 状态机）。"""

    name = "dummy"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.launch_count = 0
        self.fed: list[dict] = []
        self.complete_with: str | None = None

    def _launch(self, spec):
        self.launch_count += 1
        return "dummy-ref"

    def _halt(self, record):
        pass

    def _poll(self, record):
        if self.complete_with:
            record.output = {"via": "poll"}
            self._finish(record, self.complete_with)

    def _feed(self, record, payload):
        self.fed.append(payload)


class RecordingTransport:
    """记录每次 POST 的假传输；fail=True 时模拟投递失败。"""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str, dict]] = []
        self.fail = fail

    def post(self, url: str, key: str, payload: dict) -> None:
        if self.fail:
            raise grok.WebhookError("模拟投递失败")
        self.calls.append((url, key, payload))


def _spec(key="k-1", **payload):
    return base.RunSpec(kind="qa_requested", payload=payload, idempotency_key=key)


def _grok(clock, transport=None):
    return grok.GrokWebhookAdapter(
        transport=transport or RecordingTransport(),
        webhook_url="https://hook.example/qa",
        webhook_key="test-key",
        clock=clock,
    )


# ---------------------------------------------------------------------------
# 基类公共规则
# ---------------------------------------------------------------------------


def test_interface_shape():
    for cls in (DummyAdapter, grok.GrokWebhookAdapter):
        assert issubclass(cls, base.HarnessAdapter)
        for verb in ("start", "stop", "status", "submit", "result"):
            assert callable(getattr(cls, verb))


def test_start_status_result_flow(clock):
    adapter = DummyAdapter(clock=clock)
    handle = adapter.start(_spec())
    assert handle.state == base.RunState.RUNNING
    assert adapter.status(handle.run_id).state == base.RunState.RUNNING
    with pytest.raises(base.RunNotFinished):
        adapter.result(handle.run_id)
    adapter.complete_with = base.RunState.SUCCEEDED
    assert adapter.status(handle.run_id).state == base.RunState.SUCCEEDED
    result = adapter.result(handle.run_id)
    assert result.state == base.RunState.SUCCEEDED
    assert result.output == {"via": "poll"}


def test_idempotent_start_does_not_reexecute(clock):
    adapter = DummyAdapter(clock=clock)
    first = adapter.start(_spec(key="same"))
    second = adapter.start(_spec(key="same"))
    assert second.run_id == first.run_id
    assert adapter.launch_count == 1
    third = adapter.start(_spec(key="other"))
    assert third.run_id != first.run_id
    assert adapter.launch_count == 2


def test_timeout_after_four_hours(clock):
    adapter = DummyAdapter(clock=clock)
    handle = adapter.start(_spec())
    clock.advance(4 * 3600 - 60)
    assert adapter.status(handle.run_id).state == base.RunState.RUNNING
    clock.advance(61)
    assert adapter.status(handle.run_id).state == base.RunState.TIMEOUT
    assert adapter.result(handle.run_id).state == base.RunState.TIMEOUT
    with pytest.raises(base.RunNotActive):
        adapter.submit(handle.run_id, {"x": 1})


def test_stop_is_terminal_and_idempotent(clock):
    adapter = DummyAdapter(clock=clock)
    handle = adapter.start(_spec())
    stopped = adapter.stop(handle.run_id)
    assert stopped.state == base.RunState.STOPPED
    assert adapter.stop(handle.run_id).state == base.RunState.STOPPED
    assert adapter.result(handle.run_id).state == base.RunState.STOPPED
    with pytest.raises(base.RunNotActive):
        adapter.submit(handle.run_id, {})


def test_submit_feeds_active_run(clock):
    adapter = DummyAdapter(clock=clock)
    handle = adapter.start(_spec())
    adapter.submit(handle.run_id, {"note": "继续"})
    assert adapter.fed == [{"note": "继续"}]


def test_unknown_run_raises(clock):
    adapter = DummyAdapter(clock=clock)
    for call in (
        lambda: adapter.status("nope"),
        lambda: adapter.stop("nope"),
        lambda: adapter.submit("nope", {}),
        lambda: adapter.result("nope"),
    ):
        with pytest.raises(base.UnknownRun):
            call()


# ---------------------------------------------------------------------------
# Grok webhook 驱动
# ---------------------------------------------------------------------------


def test_grok_start_posts_orchestrator_payload(clock):
    transport = RecordingTransport()
    adapter = _grok(clock, transport)
    handle = adapter.start(
        _spec(
            repo="Jouwala/noc-soc-intelligent-platform",
            pr_number=47,
            head_sha="abc123def",
            run_url="https://github.com/x/actions/runs/1",
        )
    )
    assert handle.state == base.RunState.RUNNING
    assert len(transport.calls) == 1
    url, key, payload = transport.calls[0]
    assert url == "https://hook.example/qa"
    assert key == "test-key"
    assert payload == {
        "event": "qa_requested",
        "repo": "Jouwala/noc-soc-intelligent-platform",
        "pr_number": 47,
        "head_sha": "abc123def",
        "run_url": "https://github.com/x/actions/runs/1",
    }


def test_grok_payload_includes_issue_number_only_when_present(clock):
    transport = RecordingTransport()
    adapter = _grok(clock, transport)
    adapter.start(_spec(pr_number=47, issue_number=19))
    assert transport.calls[0][2]["issue_number"] == 19


def test_grok_idempotent_start_posts_once(clock):
    transport = RecordingTransport()
    adapter = _grok(clock, transport)
    first = adapter.start(_spec(key="dup", pr_number=47))
    second = adapter.start(_spec(key="dup", pr_number=47))
    assert second.run_id == first.run_id
    assert len(transport.calls) == 1


def test_grok_failed_start_keeps_idempotency_key_free(clock):
    transport = RecordingTransport(fail=True)
    adapter = _grok(clock, transport)
    with pytest.raises(grok.WebhookError):
        adapter.start(_spec(key="retry", pr_number=47))
    transport.fail = False
    handle = adapter.start(_spec(key="retry", pr_number=47))
    assert handle.state == base.RunState.RUNNING
    assert len(transport.calls) == 1


def test_grok_missing_config_fails_loudly(clock, monkeypatch):
    monkeypatch.delenv(grok.ENV_URL, raising=False)
    monkeypatch.delenv(grok.ENV_KEY, raising=False)
    adapter = grok.GrokWebhookAdapter(
        transport=RecordingTransport(), webhook_url="", webhook_key="", clock=clock
    )
    with pytest.raises(grok.WebhookError):
        adapter.start(_spec(pr_number=47))


def test_grok_submit_outcome_completes_run(clock):
    adapter = _grok(clock)
    handle = adapter.start(_spec(pr_number=47))
    adapter.submit(handle.run_id, {"outcome": "passed", "detail": "冒烟全绿"})
    result = adapter.result(handle.run_id)
    assert result.state == base.RunState.SUCCEEDED
    assert result.output["outcome"] == "passed"

    handle2 = adapter.start(_spec(key="k-2", pr_number=48))
    adapter.submit(handle2.run_id, {"outcome": "failed"})
    assert adapter.result(handle2.run_id).state == base.RunState.FAILED

    handle3 = adapter.start(_spec(key="k-3", pr_number=49))
    with pytest.raises(base.HarnessError):
        adapter.submit(handle3.run_id, {"outcome": "unknown"})
    assert adapter.status(handle3.run_id).state == base.RunState.RUNNING


def test_grok_timeout_without_outcome(clock):
    adapter = _grok(clock)
    handle = adapter.start(_spec(pr_number=47))
    clock.advance(4 * 3600 + 1)
    assert adapter.status(handle.run_id).state == base.RunState.TIMEOUT


def test_grok_run_timeout_configurable(clock):
    adapter = grok.GrokWebhookAdapter(
        transport=RecordingTransport(),
        webhook_url="https://hook.example/qa",
        webhook_key="test-key",
        clock=clock,
        run_timeout=timedelta(minutes=30),
    )
    handle = adapter.start(_spec(pr_number=47))
    clock.advance(31 * 60)
    assert adapter.status(handle.run_id).state == base.RunState.TIMEOUT
