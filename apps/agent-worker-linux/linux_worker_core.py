"""Linux 沙箱 Worker 核心：发现 → ref-CAS 认领 → 执行 → 完成/失败/取消 → 清理。

设计边界
--------
- **权威层不在这里**：认领/租约/代次/fencing/范围锁/幂等完成全部复用
  `scheduler.make_scheduler(...)` 返回的 `GitHubRefScheduler`（GitHub ref-CAS），
  本模块只编排调用顺序，不复制 HWM 协议、不缓存权威状态。
- **执行器不在这里**：复用 `apps/agent-worker/executor.py`（POSIX 进程组、
  SIGTERM→SIGKILL、超时/取消、`env_filter` 白名单 #78）。
- **任务发现只读**：`transport.list_issues(labels=(agent:<worker_id>, state:todo))`；
  认领是否成功只由 `scheduler.claim()` 的 ref-CAS 裁决。

失败语义（与 `scheduler/api.py` 状态机一致）
-------------------------------------------
- 无「执行命令」→ `transition(BLOCKED, error_code="no-command")` → `release()`
  （BLOCKED→QUEUED）→ 标签 `state:blocked`；
- 范围冲突 → `release()`（RUNNING→RETRYABLE）→ 标签 `state:todo`，主循环继续；
- 执行失败/超时/进程树未退出 → `transition(FAILED, error_code=...)`（权威层负责
  删租约、释放范围锁、标签收敛 `state:blocked`）；
- 执行中被取消 → `ack_cancel` → `cancelled`；
- 续租/写操作收到 `StaleFencing` → 旧执行者立即停止、不再写权威状态；
- 任何异常路径的 finally 都释放范围锁（用最新 token）与未终结租约；`StaleFencing`
  静默（说明租约已不归我），其余清理异常只记日志。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import linux_config
import probe_tools
import runtime_loader

DEFAULT_CANCEL_POLL_SECONDS = 5.0


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LinuxWorker:
    """一台 VM 上一个 instance 的常驻 Worker（可注入 log/sleep 便于离线测试）。"""

    def __init__(
        self,
        runtime: Mapping[str, Any],
        config: linux_config.LinuxWorkerConfig,
        *,
        log: Optional[Callable[[str], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
        cancel_poll_seconds: float = DEFAULT_CANCEL_POLL_SECONDS,
        transport_factory: Optional[Callable[..., Any]] = None,
        scheduler_factory: Optional[Callable[..., Any]] = None,
        executor_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self.log = log or (lambda message: print(f"[{_utc_iso()}] {message}", flush=True))
        self.sleep = sleep
        self.cancel_poll_seconds = max(0.1, float(cancel_poll_seconds))
        self.S = runtime["scheduler"]
        host = runtime["hostinfo"].collect()
        self.hostname = host.get("hostname", "")
        self.platform = host.get("platform", "")
        self.capabilities = ("linux-sandbox",)

        make_transport = transport_factory or runtime["RestGitHubTransport"]
        self.transport = make_transport(
            config.owner,
            config.repo_name,
            token=config.token or None,
            api_base=config.api_base,
            instance_id=config.ref_instance,
            hostname=self.hostname,
            platform=self.platform,
        )
        make_scheduler = scheduler_factory or runtime_loader.scheduler_factory(runtime)
        self.scheduler = make_scheduler(
            transport=self.transport,
            repo=config.repo,
            lease_ttl=timedelta(seconds=config.lease_ttl_seconds),
            registry_issue=config.registry_issue,
        )
        make_executor = executor_factory or runtime["executor"].Executor
        self.executor = make_executor(
            timeout_s=config.executor_timeout_seconds, log=self.log
        )
        self.command_parse = runtime["command_parse"]
        self.parse_file_scope = runtime["parse_file_scope"]
        self._cancel_watcher_cls = runtime["cancellation"].CancelWatcher
        self._heartbeat_writer_cls = runtime["heartbeat"].HeartbeatWriter
        self.heartbeat = None

    # -- 身份 / 心跳 ---------------------------------------------------------
    def identity_line(self) -> str:
        identity = self.config.public_identity()
        identity["hostname"] = self.hostname
        identity["platform"] = self.platform
        identity["capabilities"] = list(self.capabilities)
        return "identity " + json.dumps(identity, ensure_ascii=False, sort_keys=True)

    def register(self) -> Any:
        snapshot = self.scheduler.register_worker(
            self.config.worker_id,
            self.config.instance,
            hostname=self.hostname,
            platform=self.platform,
            capabilities=self.capabilities,
        )
        self.log(self.identity_line())
        self.log(
            f"registered worker_id={self.config.worker_id} instance={self.config.instance} "
            f"repo={self.config.repo}"
        )
        return snapshot

    def start_heartbeat(self) -> None:
        if self.heartbeat is not None:
            return
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat = self._heartbeat_writer_cls(
            self.config.heartbeat_path(),
            agent_id=self.config.worker_id,
            instance_id=self.config.instance,
            log=self.log,
        )
        self.heartbeat.start()

    def stop_heartbeat(self) -> None:
        if self.heartbeat is not None:
            self.heartbeat.stop()
            self.heartbeat = None

    # -- 主循环 -------------------------------------------------------------
    def run_forever(self, stop_event=None) -> None:
        self.register()
        self.start_heartbeat()
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    result = self.run_cycle()
                    self.log(f"cycle {json.dumps(result, ensure_ascii=False)}")
                except KeyboardInterrupt:
                    self.log("收到中断，准备退出")
                    break
                except Exception as exc:  # noqa: BLE001 - 常驻循环不因单轮异常退出
                    self.log(f"cycle error: {type(exc).__name__}: {exc}")
                if stop_event is not None and stop_event.is_set():
                    break
                self._sleep_or_stop(stop_event, self.config.poll_seconds)
        finally:
            self.stop_heartbeat()

    def _sleep_or_stop(self, stop_event, seconds: float) -> None:
        """可被 stop_event 提前唤醒的 sleep（无事件时退回注入的 sleep）。"""
        if stop_event is None:
            self.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not stop_event.is_set():
            time.sleep(min(0.2, max(0.01, deadline - time.monotonic())))

    def run_cycle(self) -> dict[str, Any]:
        """一个轮询周期：拉取可认领任务 + 最多处理一个。

        调度器心跳在状态已知时写（无任务 → idle，认领后 → working），不再在
        周期开头写 online——否则 online beat 会抢占 60s 节流窗口，idle/working
        永远写不进去。
        """
        try:
            issues = self.transport.list_issues(
                labels=(self.config.agent_label, "state:todo"), state="open"
            )
        except Exception as exc:  # noqa: BLE001 - 断网/限流：下一轮重试
            self.log(f"discover failed: {type(exc).__name__}: {exc}")
            return {"outcome": "discover-error"}
        saw_lease_held = False
        for issue in sorted(issues, key=lambda item: int(item.number)):
            outcome = self.process_issue(issue)
            if outcome in ("lease-held", "already-terminal"):
                if outcome == "lease-held":
                    saw_lease_held = True
                continue
            return {"outcome": outcome, "issue": int(issue.number),
                    "task_id": f"issue-{int(issue.number)}"}
        try:
            self.scheduler.heartbeat_worker(
                self.config.worker_id, self.config.instance,
                state="waiting" if saw_lease_held else "idle",
            )
        except Exception as exc:  # noqa: BLE001 - 心跳失败不阻塞任务发现
            self.log(f"worker heartbeat failed: {type(exc).__name__}: {exc}")
        return {"outcome": "idle"}

    # -- 单个任务 -----------------------------------------------------------
    def process_issue(self, issue) -> str:
        cfg = self.config
        S = self.S
        task_id = f"issue-{int(issue.number)}"
        scope = tuple(self.parse_file_scope(getattr(issue, "body", "") or "") or ())
        state: dict[str, Any] = {
            "task_id": task_id,
            "issue": int(issue.number),
            "attempt": "",
            "token": 0,
            "lease": False,
            "scope": False,
            "scope_acquired": False,
            "terminal": False,
            "stale": False,
        }
        try:
            self.scheduler.upsert_task(
                task_id,
                int(issue.number),
                repo=cfg.repo,
                title=str(getattr(issue, "title", "") or ""),
                scopes=scope,
            )
            attempt = uuid.uuid4().hex[:16]
            try:
                lease = self.scheduler.claim(
                    task_id,
                    agent_id=cfg.ref_agent_id,
                    instance_id=cfg.ref_instance,
                    attempt_id=attempt,
                    lease_ttl=timedelta(seconds=cfg.lease_ttl_seconds),
                )
            except S.LeaseHeld:
                self.log(f"claim {task_id}: lease held by another worker")
                return "lease-held"
            except S.AlreadyTerminal:
                self.log(f"claim {task_id}: already terminal")
                return "already-terminal"
            state.update(
                attempt=lease.attempt_id,
                token=lease.fencing_token,
                lease=True,
            )
            self.log(
                f"claimed {task_id} attempt={lease.attempt_id} token={lease.fencing_token}"
            )
            try:
                self.scheduler.heartbeat_worker(
                    self.config.worker_id, self.config.instance, state="working"
                )
            except Exception as exc:  # noqa: BLE001 - 心跳失败不影响已认领的任务
                self.log(f"worker heartbeat failed: {type(exc).__name__}: {exc}")
            self._set_state_label(int(issue.number), "state:claimed")
            self.scheduler.transition(
                task_id,
                attempt_id=state["attempt"],
                fencing_token=state["token"],
                state=S.RUNNING,
            )

            if self.scheduler.is_cancel_requested(task_id):
                return "cancelled" if self._ack_cancel(state) else "stale-fencing"

            if scope:
                try:
                    self.scheduler.acquire_scope(
                        task_id,
                        attempt_id=state["attempt"],
                        fencing_token=state["token"],
                        scopes=scope,
                    )
                except S.ScopeConflict as exc:
                    self.log(f"scope conflict on {task_id}: {exc}")
                    self._release_quietly(state)
                    self._set_state_label(int(issue.number), "state:todo")
                    return "scope-conflict"
                state["scope_acquired"] = True

            command = self.command_parse.extract_command(getattr(issue, "body", "") or "")
            if command is None:
                self.log(f"{task_id}: no command section → blocked")
                try:
                    self.scheduler.transition(
                        task_id,
                        attempt_id=state["attempt"],
                        fencing_token=state["token"],
                        state=S.BLOCKED,
                        error_code="no-command",
                        error_message="Issue 正文缺少「执行命令」一节",
                    )
                except S.StaleFencing as exc:
                    state["stale"] = True
                    self.log(f"{task_id}: no-command transition stale: {exc}")
                if not state["stale"]:
                    self._release_quietly(state)
                self._set_state_label(int(issue.number), "state:blocked")
                return "blocked"

            result = self._execute(state, command)
            if state["stale"]:
                self.log(f"{task_id}: lease lost during execution, drop local result")
                return "stale-fencing"
            if self.scheduler.is_cancel_requested(task_id):
                return "cancelled" if self._ack_cancel(state) else "stale-fencing"
            if result.killed_reason == "timeout":
                return "failed" if self._fail(state, "executor-timeout", result.summary) else "stale-fencing"
            if not result.tree_exited:
                return "failed" if self._fail(state, "process-tree-leaked", result.summary) else "stale-fencing"
            if not result.ok:
                return "failed" if self._fail(state, "execution-failed", result.summary) else "stale-fencing"
            return "completed" if self._complete(state, result.summary) else "stale-fencing"
        except S.StaleFencing as exc:
            state["stale"] = True
            self.log(f"{task_id}: stale fencing ({exc})，旧执行者停止")
            return "stale-fencing"
        except Exception as exc:  # noqa: BLE001 - 尽力释放后继续主循环
            self.log(f"{task_id}: internal error {type(exc).__name__}: {exc}")
            self._release_quietly(state)
            self._set_state_label(int(issue.number), "state:todo")
            return "error"
        finally:
            self._cleanup(state)

    # -- 执行 ---------------------------------------------------------------
    def _execute(self, state: dict[str, Any], command) -> Any:
        cfg = self.config
        task_id = state["task_id"]
        workdir = cfg.task_workdir(state["issue"])
        workdir.mkdir(parents=True, exist_ok=True)
        log_dir = cfg.task_log_dir(state["issue"])
        watcher = self._cancel_watcher_cls(
            self.scheduler,
            task_id,
            interval_s=self.cancel_poll_seconds,
            log=self.log,
        )
        last_renew = {"at": time.monotonic()}

        def on_tick() -> None:
            now = time.monotonic()
            if now - last_renew["at"] < cfg.renew_interval_seconds:
                return
            last_renew["at"] = now
            try:
                lease = self.scheduler.renew(
                    task_id,
                    attempt_id=state["attempt"],
                    fencing_token=state["token"],
                    lease_ttl=timedelta(seconds=cfg.lease_ttl_seconds),
                )
            except self.S.StaleFencing as exc:
                state["stale"] = True
                self.log(f"{task_id}: renew rejected（租约已丢）：{exc}")
                watcher.trigger()
                return
            except Exception as exc:  # noqa: BLE001 - 单次续租失败下个周期再试
                self.log(f"{task_id}: renew failed（下个周期重试）：{exc}")
                return
            state["token"] = lease.fencing_token
            self.log(f"{task_id}: renewed token={lease.fencing_token}")

        watcher.start()
        try:
            return self.executor.run(
                command,
                workdir,
                scope=(),
                timeout_s=cfg.executor_timeout_seconds,
                log_dir=log_dir,
                cancel_event=watcher.event,
                on_tick=on_tick,
            )
        finally:
            watcher.stop()

    # -- 权威状态写操作（全部带 attempt + token） ----------------------------
    def _complete(self, state: dict[str, Any], summary: str) -> bool:
        try:
            outcome = self.scheduler.complete(
                state["task_id"],
                attempt_id=state["attempt"],
                fencing_token=state["token"],
                idempotency_key=state["attempt"],
                summary=summary,
            )
        except (self.S.StaleFencing, self.S.InvalidTransition) as exc:
            self.log(f"complete rejected: {exc}")
            return False
        state["terminal"] = True
        state["lease"] = False
        state["scope_acquired"] = False
        self.log(
            f"completed {state['task_id']} created={getattr(outcome, 'created', None)}: {summary}"
        )
        return True

    def _fail(self, state: dict[str, Any], code: str, message: str) -> bool:
        try:
            self.scheduler.transition(
                state["task_id"],
                attempt_id=state["attempt"],
                fencing_token=state["token"],
                state=self.S.FAILED,
                error_code=code,
                error_message=message,
            )
        except (self.S.StaleFencing, self.S.InvalidTransition) as exc:
            self.log(f"fail transition rejected: {exc}")
            return False
        state["terminal"] = True
        state["lease"] = False
        state["scope_acquired"] = False
        self.log(f"failed {state['task_id']} {code}: {message}")
        return True

    def _ack_cancel(self, state: dict[str, Any]) -> bool:
        try:
            self.scheduler.ack_cancel(
                state["task_id"],
                attempt_id=state["attempt"],
                fencing_token=state["token"],
            )
        except (self.S.StaleFencing, self.S.InvalidTransition) as exc:
            self.log(f"ack_cancel rejected: {exc}")
            return False
        state["terminal"] = True
        state["lease"] = False
        state["scope_acquired"] = False
        self.log(f"cancelled {state['task_id']}")
        return True

    def _release_quietly(self, state: dict[str, Any]) -> bool:
        """主动释放租约（未终结时）；StaleFencing 静默（已不归我）。"""
        if not state["lease"]:
            return False
        try:
            self.scheduler.release(
                state["task_id"],
                attempt_id=state["attempt"],
                fencing_token=state["token"],
            )
        except (self.S.StaleFencing, self.S.InvalidTransition) as exc:
            self.log(f"release {state['task_id']} skipped: {exc}")
            state["lease"] = False
            state["scope_acquired"] = False
            return False
        except Exception as exc:  # noqa: BLE001 - 清理异常只记录
            self.log(f"release {state['task_id']} failed: {exc}")
            state["lease"] = False
            state["scope_acquired"] = False
            return False
        state["lease"] = False
        state["scope_acquired"] = False  # release 会一并释放范围锁
        return True

    def _cleanup(self, state: dict[str, Any]) -> None:
        """finally 统一收尾：先范围锁、后租约；不覆盖已有异常。"""
        if state.get("stale"):
            # 租约已不归我：不再用旧 token 写任何权威状态
            state["lease"] = False
            state["scope_acquired"] = False
        if state.get("scope_acquired"):
            try:
                self.scheduler.release_scope(
                    state["task_id"],
                    attempt_id=state["attempt"],
                    fencing_token=state["token"],
                )
            except (self.S.StaleFencing, self.S.InvalidTransition) as exc:
                self.log(f"release_scope skipped: {exc}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"release_scope failed: {exc}")
            state["scope_acquired"] = False
        if state.get("lease") and not state.get("terminal"):
            self._release_quietly(state)

    # -- 标签（展示层；失败只记日志） ---------------------------------------
    def _set_state_label(self, issue_number: int, target_label: str) -> None:
        try:
            issue = self.transport.get_issue(int(issue_number))
            current = tuple(getattr(issue, "labels", ()) or ())
            desired = [label for label in current if not label.startswith("state:")]
            if target_label and target_label not in desired:
                desired.append(target_label)
            if tuple(desired) != current:
                self.transport.set_labels(int(issue_number), desired)
        except Exception as exc:  # noqa: BLE001 - 标签是展示层，权威在 refs/评论
            self.log(f"set labels #{issue_number} → {target_label} failed: {exc}")

    # -- 连通性检查 / 认领探针（实现见 probe_tools.py） ---------------------
    def check(self) -> dict[str, Any]:
        return probe_tools.check(self)

    def probe_claim(self, issue_number: int, *, release: bool = False) -> dict[str, Any]:
        return probe_tools.probe_claim(self, issue_number, release=release)

    def close(self) -> None:
        self.stop_heartbeat()
