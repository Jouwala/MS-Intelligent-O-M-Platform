"""Harness Adapter 统一接口（TASK-04，Issue #19）。

语义来源：TASK-02 接口定义 §8.1 —— 每个 Harness（执行后端）一个驱动，
对上层只暴露五个动作：start / stop / status / submit / result。
TASK-02 已以 GitHub 原生形态落地（#17 关闭），其 §8.1 只留下这五个
动词作为接口约定，具体形态在本文件定型。

分工：
- 本文件（基类 HarnessAdapter）实现与驱动无关的公共规则，只实现一遍，
  所有驱动继承，不各自发挥：
  1. 幂等：start 带 idempotency_key；同一键二次 start 直接返回已有
     运行的句柄，不重复执行（验收标准，Issue #19）。
  2. 超时：运行超过 4 小时（可配）未到终态，判 timeout（终态）。
  3. 状态机：pending → running → succeeded / failed / stopped /
     timeout；终态不可再 submit，stop 对终态幂等。
- 具体驱动（如 grok_webhook.py）只实现带下划线的钩子：
  _launch / _halt / _poll / _feed / _collect，负责与真实后端打交道。

时间一律走注入的 clock（返回 aware datetime，UTC），与 github_issue.py
同一约定，便于离线仿真超时。运行记录只存内存：控制面进程重启后
status 查不到旧 run_id 会抛 UnknownRun，调用方应把 run_id 视为
进程级句柄——持久化登记不是本接口的职责（任务状态的唯一事实源仍是
GitHub Issue，见 agent-protocol.md）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunState:
    """运行状态（字符串常量，与协议层的机器可读文本保持一致）。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    TIMEOUT = "timeout"


TERMINAL_STATES = frozenset(
    {RunState.SUCCEEDED, RunState.FAILED, RunState.STOPPED, RunState.TIMEOUT}
)

#: 验收标准（Issue #19）：超时 4 小时判 timeout。
DEFAULT_RUN_TIMEOUT = timedelta(hours=4)


@dataclass
class RunSpec:
    """一次运行的启动参数。

    kind：运行类别（如 qa_requested），由驱动解释。
    payload：类别参数（如 repo / pr_number / head_sha），驱动自取。
    idempotency_key：幂等键；同一驱动实例内同键只执行一次。
    """

    kind: str
    payload: dict = field(default_factory=dict)
    idempotency_key: str = ""


@dataclass
class RunHandle:
    """start 的返回值与 status 的快照形态。"""

    run_id: str
    idempotency_key: str
    state: str
    created_at: datetime
    updated_at: datetime
    detail: str = ""


@dataclass
class RunResult:
    """终态运行的结果。output 由驱动在 _collect 里给出（可为 None）。"""

    run_id: str
    state: str
    output: Optional[dict]
    finished_at: datetime
    detail: str = ""


class HarnessError(Exception):
    """Harness 接口层错误基类。"""


class UnknownRun(HarnessError):
    """run_id 不存在（从未启动，或控制面重启后记录已失）。"""


class RunNotActive(HarnessError):
    """对已到终态的运行做 submit 等只许活动运行的操作。"""


class RunNotFinished(HarnessError):
    """运行未到终态时取 result。先 status 轮询，到终态再取。"""


@dataclass
class _RunRecord:
    spec: RunSpec
    run_id: str
    state: str
    created_at: datetime
    updated_at: datetime
    driver_ref: str = ""
    output: Optional[dict] = None
    detail: str = ""
    finished_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# 统一接口基类
# ---------------------------------------------------------------------------


class HarnessAdapter:
    """Harness 驱动基类：公共规则在此，驱动只实现钩子。

    钩子约定：
    - _launch(spec) -> str：真正启动后端执行，返回驱动侧引用（可空串）。
      抛异常表示启动失败：本次 start 不登记幂等键，调用方可同键重试。
    - _halt(record)：尽力停止后端执行（后端无停止能力时可为空实现，
      基类仍会把本地状态置 stopped 并在 detail 里注明由驱动说明）。
    - _poll(record)：把后端最新进展同步进 record（改 state / output /
      detail）；无轮询通道的驱动实现为空。基类在 status / result /
      submit / stop 之前调用。
    - _feed(record, payload)：把外部输入投递给运行中的后端；
      无输入通道的驱动应抛 RunNotActive 或 HarnessError 说明。
    - _collect(record) -> Optional[dict]：终态时组装 result 的 output，
      默认直接用 record.output，驱动可覆盖加工。
    """

    #: 驱动名，子类覆盖，用于日志与 detail。
    name = "base"

    def __init__(
        self,
        clock: Callable[[], datetime] = utcnow,
        run_timeout: timedelta = DEFAULT_RUN_TIMEOUT,
    ) -> None:
        self._clock = clock
        self._run_timeout = run_timeout
        self._runs: dict[str, _RunRecord] = {}
        self._by_key: dict[str, str] = {}

    # ---- 五个动作（§8.1）----

    def start(self, spec: RunSpec) -> RunHandle:
        """启动一次运行。同一幂等键二次调用返回已有句柄，不重复执行。"""
        if spec.idempotency_key and spec.idempotency_key in self._by_key:
            return self._snapshot(self._runs[self._by_key[spec.idempotency_key]])
        now = self._clock()
        record = _RunRecord(
            spec=spec,
            run_id=uuid.uuid4().hex,
            state=RunState.PENDING,
            created_at=now,
            updated_at=now,
        )
        # 先登记再启动：_launch 抛错时撤销登记，幂等键不被失败占用。
        self._runs[record.run_id] = record
        if spec.idempotency_key:
            self._by_key[spec.idempotency_key] = record.run_id
        try:
            record.driver_ref = self._launch(spec) or ""
        except Exception:
            self._runs.pop(record.run_id, None)
            if spec.idempotency_key:
                self._by_key.pop(spec.idempotency_key, None)
            raise
        record.state = RunState.RUNNING
        record.updated_at = self._clock()
        return self._snapshot(record)

    def stop(self, run_id: str) -> RunHandle:
        """停止运行。已到终态时幂等返回当前快照，不报错。"""
        record = self._get(run_id)
        self._refresh(record)
        if record.state in TERMINAL_STATES:
            return self._snapshot(record)
        self._halt(record)
        self._finish(record, RunState.STOPPED)
        return self._snapshot(record)

    def status(self, run_id: str) -> RunHandle:
        """查运行状态快照（含超时判定）。"""
        record = self._get(run_id)
        self._refresh(record)
        return self._snapshot(record)

    def submit(self, run_id: str, payload: dict) -> RunHandle:
        """向活动中的运行投递输入。终态运行抛 RunNotActive。"""
        record = self._get(run_id)
        self._refresh(record)
        if record.state in TERMINAL_STATES:
            raise RunNotActive(
                f"运行 {run_id} 已到终态（{record.state}），不能再 submit"
            )
        self._feed(record, payload)
        record.updated_at = self._clock()
        return self._snapshot(record)

    def result(self, run_id: str) -> RunResult:
        """取终态结果。未到终态抛 RunNotFinished。"""
        record = self._get(run_id)
        self._refresh(record)
        if record.state not in TERMINAL_STATES:
            raise RunNotFinished(
                f"运行 {run_id} 尚未结束（当前 {record.state}），先用 status 轮询"
            )
        return RunResult(
            run_id=record.run_id,
            state=record.state,
            output=self._collect(record),
            finished_at=record.finished_at or record.updated_at,
            detail=record.detail,
        )

    # ---- 内部 ----

    def _get(self, run_id: str) -> _RunRecord:
        try:
            return self._runs[run_id]
        except KeyError:
            raise UnknownRun(f"未知 run_id：{run_id}") from None

    def _refresh(self, record: _RunRecord) -> None:
        """同步驱动进展并做超时判定（终态后不再刷新）。"""
        if record.state in TERMINAL_STATES:
            return
        self._poll(record)
        if record.state in TERMINAL_STATES:
            if record.finished_at is None:
                record.finished_at = self._clock()
            record.updated_at = self._clock()
            return
        if self._clock() - record.created_at > self._run_timeout:
            self._finish(
                record,
                RunState.TIMEOUT,
                detail=record.detail
                or f"超过 {self._run_timeout} 未结束，判 timeout",
            )

    def _finish(self, record: _RunRecord, state: str, detail: str = "") -> None:
        record.state = state
        if detail:
            record.detail = detail
        record.finished_at = self._clock()
        record.updated_at = record.finished_at

    def _snapshot(self, record: _RunRecord) -> RunHandle:
        return RunHandle(
            run_id=record.run_id,
            idempotency_key=record.spec.idempotency_key,
            state=record.state,
            created_at=record.created_at,
            updated_at=record.updated_at,
            detail=record.detail,
        )

    # ---- 驱动钩子 ----

    def _launch(self, spec: RunSpec) -> str:
        raise NotImplementedError

    def _halt(self, record: _RunRecord) -> None:
        raise NotImplementedError

    def _poll(self, record: _RunRecord) -> None:
        """默认无轮询通道：状态只由 submit / stop / 超时推进。"""

    def _feed(self, record: _RunRecord, payload: dict) -> None:
        raise NotImplementedError

    def _collect(self, record: _RunRecord) -> Optional[dict]:
        return record.output
