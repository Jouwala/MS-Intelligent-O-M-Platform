"""GrokWebhookAdapter —— 以 Grok webhook 为执行后端的 Harness 驱动（TASK-04）。

实现 base.py 的统一接口（start / stop / status / submit / result），
复用仓库现有的 Grok webhook 契约——即 .github/workflows/agent-orchestrator.yml
里「POST smoke request to Grok Routine webhook」那一步：

- 地址与密钥走仓库 secret 对应的环境变量 GROK_ROUTINE_WEBHOOK_URL /
  GROK_ROUTINE_WEBHOOK_KEY（也可构造参数注入），绝不写进代码；
- 请求：POST JSON，头带 Authorization: Bearer <key>；
- 载荷：{event, repo, pr_number, head_sha, run_url}，有 issue_number
  时才带（与工作流里 jq 的条件拼法一致）；event 默认 qa_requested。

语义映射（webhook 是单向投递，没有查询与取消通道，按实情建模）：
- start：组装载荷并 POST，2xx 视为已受理，运行进入 running；
  POST 失败抛 WebhookError，基类不登记幂等键，可同键重试。
- status：本地记录 + 基类的 4 小时超时判定（webhook 无轮询端点，
  Grok 侧进展不会主动回流到这里）。
- submit：结果回传通道——外部（轮询到 Grok 结论的一方）把
  {"outcome": "passed" | "failed", ...} 投进来，运行随之到
  succeeded / failed，整份载荷存为 result 的 output。
- stop：webhook 无远程取消，只能本地标记 stopped 并注明远端
  若仍执行、其后 submit 的结果仍会被接收——调用方以本地状态为准。
- result：终态后由基类给出，output 即 submit 回传的载荷。

传输层隔离成 WebhookTransport（与 github_issue.py 的 Transport 同一
思路）：HttpWebhookTransport 走真实 HTTP，测试注入记录型假传输。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional, Protocol

# base.py 与本文件同目录，但路径带连字符不是合法包名，按仓库既有约定
# （apps/agent-worker/_bootstrap.py）用文件路径加载并注册固定模块名。
if "harness_base" in sys.modules:
    _base = sys.modules["harness_base"]
else:
    _spec = importlib.util.spec_from_file_location(
        "harness_base", Path(__file__).resolve().parent / "base.py"
    )
    assert _spec is not None and _spec.loader is not None, "无法加载 base.py"
    _base = importlib.util.module_from_spec(_spec)
    sys.modules["harness_base"] = _base
    _spec.loader.exec_module(_base)

HarnessAdapter = _base.HarnessAdapter
RunSpec = _base.RunSpec
RunState = _base.RunState
HarnessError = _base.HarnessError
DEFAULT_RUN_TIMEOUT = _base.DEFAULT_RUN_TIMEOUT

ENV_URL = "GROK_ROUTINE_WEBHOOK_URL"
ENV_KEY = "GROK_ROUTINE_WEBHOOK_KEY"
DEFAULT_EVENT = "qa_requested"


class WebhookError(HarnessError):
    """webhook 投递失败（未配置、网络错误、非 2xx 响应）。"""


class WebhookTransport(Protocol):
    def post(self, url: str, key: str, payload: dict) -> None:
        """POST 成功（2xx）返回 None，失败抛 WebhookError。"""
        ...


class HttpWebhookTransport:
    """真实 HTTP 传输：urllib POST JSON + Bearer 头（对应工作流里的 curl）。"""

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self._timeout = timeout_seconds

    def post(self, url: str, key: str, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                status = resp.status
        except Exception as exc:  # 网络错误 / 非 2xx（HTTPError 是其子类）
            raise WebhookError(f"Grok webhook POST 失败：{exc}") from exc
        if not 200 <= status < 300:
            raise WebhookError(f"Grok webhook 返回 HTTP {status}")


class GrokWebhookAdapter(HarnessAdapter):
    """Grok webhook 驱动。公共规则（幂等、超时、状态机）在基类。"""

    name = "grok-webhook"

    def __init__(
        self,
        transport: Optional[WebhookTransport] = None,
        webhook_url: str = "",
        webhook_key: str = "",
        clock: Callable = _base.utcnow,
        run_timeout: timedelta = DEFAULT_RUN_TIMEOUT,
    ) -> None:
        super().__init__(clock=clock, run_timeout=run_timeout)
        self._transport = transport or HttpWebhookTransport()
        self._webhook_url = webhook_url or os.environ.get(ENV_URL, "")
        self._webhook_key = webhook_key or os.environ.get(ENV_KEY, "")

    # ---- 钩子实现 ----

    def _launch(self, spec: RunSpec) -> str:
        if not self._webhook_url or not self._webhook_key:
            # 与工作流的 test -n 守卫同一口径：没配齐就明确失败，不静默。
            raise WebhookError(
                f"Grok webhook 未配置：需要 {ENV_URL} / {ENV_KEY}"
                "（构造参数或环境变量）"
            )
        payload = self._build_payload(spec)
        self._transport.post(self._webhook_url, self._webhook_key, payload)
        return f"grok:{payload.get('pr_number', '')}:{(payload.get('head_sha') or '')[:8]}"

    def _build_payload(self, spec: RunSpec) -> dict:
        """与 agent-orchestrator.yml 的 jq 拼法逐字段对齐。"""
        src = spec.payload
        payload: dict = {
            "event": spec.kind or DEFAULT_EVENT,
            "repo": src.get("repo", ""),
            "pr_number": int(src.get("pr_number", 0)),
            "head_sha": src.get("head_sha", ""),
            "run_url": src.get("run_url", ""),
        }
        issue_number = src.get("issue_number")
        if issue_number not in (None, ""):
            payload["issue_number"] = int(issue_number)
        return payload

    def _feed(self, record, payload: dict) -> None:
        outcome = str(payload.get("outcome", "")).lower()
        if outcome not in ("passed", "failed"):
            raise HarnessError(
                "Grok 结果回传的 outcome 只能是 passed / failed，"
                f"收到：{payload.get('outcome')!r}"
            )
        record.output = dict(payload)
        self._finish(
            record,
            RunState.SUCCEEDED if outcome == "passed" else RunState.FAILED,
            detail=str(payload.get("detail", "") or record.detail),
        )

    def _halt(self, record) -> None:
        record.detail = (
            "webhook 无远程取消通道：本地已标记 stopped；"
            "Grok 侧若仍在执行，其结论以本地状态为准"
        )
