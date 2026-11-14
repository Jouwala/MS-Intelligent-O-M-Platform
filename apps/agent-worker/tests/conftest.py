"""离线自测的公共夹具：按文件路径加载被测模块 + 可控假时钟 + 调度器替身。"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _bootstrap  # noqa: E402

gi = _bootstrap.load_adapter()
wt = _bootstrap.load_sibling("worktree")
ex = _bootstrap.load_sibling("executor")
wk = _bootstrap.load_sibling("worker")
hb = _bootstrap.load_sibling("heartbeat")
cx = _bootstrap.load_sibling("cancellation")
hi = _bootstrap.load_sibling("hostinfo")
cp = _bootstrap.load_sibling("command_parse")
tl = _bootstrap.load_sibling("task_lifecycle")
wd = _bootstrap.load_sibling("watchdog")

START = datetime(2026, 11, 14, 2, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FloatClock:
    """看门狗用 float 时钟（与 time.time 同型）。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def float_clock() -> FloatClock:
    return FloatClock()


@pytest.fixture()
def transport(clock):
    return gi.InMemoryGitHubTransport(clock=clock)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """一个带 .githooks 的临时 git 仓库（钩子已提交、hooksPath 已设置）。"""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    hooks = repo / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    # POSIX 上 Git 只执行带可执行位的钩子（TASK-12）；Windows 无此语义
    hook.chmod(0o755)
    (repo / "README.md").write_text("test repo\n", encoding="utf-8")
    # 与真实仓库一致：TMP/ 是探针与执行日志的落点，被忽略不算改动
    (repo / ".gitignore").write_text("TMP/\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    git("config", "core.hooksPath", ".githooks")
    return repo


# ---------------------------------------------------------------------------
# 调度器替身：实现 Scheduler Protocol 中 Worker 用到的语义（与 api.py 同形）
# ---------------------------------------------------------------------------


def _norm_scope(entry: str) -> str:
    return entry.replace("\\", "/").strip("/")


def _overlap(a: tuple[str, ...], b: tuple[str, ...]) -> list[str]:
    hits = []
    for left in a:
        for right in b:
            ln, rn = _norm_scope(left), _norm_scope(right)
            if ln == rn or ln.startswith(rn + "/") or rn.startswith(ln + "/"):
                hits.append(ln)
    return hits


class FakeScheduler:
    """内存版 Scheduler：原子性/持久化由 sched-core 的真实 service 负责，
    这里只复刻 Worker 依赖的**语义**（认领、fencing、范围锁、幂等完成、取消）。"""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.S = wk.scheduler_module()
        self.tasks: dict[str, dict] = {}
        self.leases: dict[str, dict] = {}
        self.scope_locks: dict[str, tuple[str, ...]] = {}
        self.cancel_ids: set[str] = set()
        self.workers: dict[tuple[str, str], dict] = {}
        self.completions: list[dict] = []
        self.outbox: list[dict] = []
        self.ack_calls: list[str] = []
        self.release_calls: list[str] = []
        self.scope_release_calls: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    # -- 工具 ---------------------------------------------------------------
    def _record(self, name: str, **kwargs) -> None:
        self.calls.append((name, kwargs))

    def _task(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise self.S.TaskNotFound(task_id)
        return task

    def _require_lease(self, task_id: str, attempt_id: str, fencing_token: int) -> dict:
        lease = self.leases.get(task_id)
        if lease is None:
            raise self.S.StaleFencing(task_id, attempt_id, fencing_token, "", 0, "租约已被回收")
        if lease["attempt_id"] != attempt_id or lease["fencing_token"] != fencing_token:
            raise self.S.StaleFencing(
                task_id,
                attempt_id,
                fencing_token,
                lease["attempt_id"],
                lease["fencing_token"],
                "attempt/token 不符",
            )
        return lease

    def _release(self, task_id: str) -> None:
        self.leases.pop(task_id, None)
        self.scope_locks.pop(task_id, None)

    def _snapshot(self, task_id: str) -> SimpleNamespace:
        return SimpleNamespace(**self._task(task_id))

    # -- Worker 注册与心跳 ---------------------------------------------------
    def register_worker(self, agent_id, instance_id, hostname="", platform="", capabilities=()):
        self._record(
            "register_worker",
            agent_id=agent_id,
            instance_id=instance_id,
            hostname=hostname,
            platform=platform,
            capabilities=tuple(capabilities),
        )
        snap = {
            "agent_id": agent_id,
            "instance_id": instance_id,
            "hostname": hostname,
            "platform": platform,
            "capabilities": tuple(capabilities),
            "state": "active",
        }
        self.workers[(agent_id, instance_id)] = snap
        return SimpleNamespace(**snap)

    def heartbeat_worker(self, agent_id, instance_id) -> bool:
        self._record("heartbeat_worker", agent_id=agent_id, instance_id=instance_id)
        return True

    def list_workers(self, stale_after=None):
        return [SimpleNamespace(**w) for w in self.workers.values()]

    # -- 任务登记 -----------------------------------------------------------
    def upsert_task(self, task_id, issue_number, repo="", title="", scopes=()) -> SimpleNamespace:
        self._record("upsert_task", task_id=task_id, issue_number=issue_number)
        task = self.tasks.get(task_id)
        if task is None:
            task = {
                "task_id": task_id,
                "issue_number": issue_number,
                "repo": repo,
                "title": title,
                "scopes": tuple(scopes),
                "state": self.S.QUEUED,
                "fencing_token": 0,
                "attempt_id": "",
                "agent_id": "",
                "instance_id": "",
                "error_code": "",
                "error_message": "",
                "completions": [],
            }
            self.tasks[task_id] = task
        else:  # 幂等：只刷新元数据，绝不改 state/fencing/attempt
            task.update(issue_number=issue_number, repo=repo, title=title, scopes=tuple(scopes))
        return self._snapshot(task_id)

    def get_task(self, task_id) -> SimpleNamespace:
        return self._snapshot(task_id)

    # -- 认领 / 续租 --------------------------------------------------------
    def claim(self, task_id, *, agent_id, instance_id, attempt_id, lease_ttl=None):
        self._record("claim", task_id=task_id, agent_id=agent_id, instance_id=instance_id)
        task = self._task(task_id)
        if task["state"] in self.S.TERMINAL_STATES:
            raise self.S.AlreadyTerminal(task_id, task["state"])
        lease = self.leases.get(task_id)
        now = self.clock()
        if (
            lease is not None
            and lease["expires_at"] > now
            and not (lease["agent_id"] == agent_id and lease["instance_id"] == instance_id)
        ):
            raise self.S.LeaseHeld(task_id, lease["agent_id"], lease["instance_id"], lease["expires_at"])
        task["fencing_token"] += 1
        task["attempt_id"] = attempt_id
        task["agent_id"] = agent_id
        task["instance_id"] = instance_id
        task["state"] = self.S.CLAIMED
        ttl = lease_ttl or self.S.DEFAULT_LEASE_TTL
        lease = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "agent_id": agent_id,
            "instance_id": instance_id,
            "fencing_token": task["fencing_token"],
            "claimed_at": now,
            "expires_at": now + ttl,
            "heartbeat_at": now,
        }
        self.leases[task_id] = lease
        return SimpleNamespace(**lease)

    def renew(self, task_id, *, attempt_id, fencing_token, lease_ttl=None):
        self._record("renew", task_id=task_id, attempt_id=attempt_id)
        lease = self._require_lease(task_id, attempt_id, fencing_token)
        lease["expires_at"] = self.clock() + (lease_ttl or self.S.DEFAULT_LEASE_TTL)
        lease["heartbeat_at"] = self.clock()
        return SimpleNamespace(**lease)

    # -- 状态迁移 -----------------------------------------------------------
    def transition(self, task_id, *, attempt_id, fencing_token, state, error_code="", error_message=""):
        self._record("transition", task_id=task_id, state=state)
        self._require_lease(task_id, attempt_id, fencing_token)
        task = self._task(task_id)
        self.S.assert_transition(task["state"], state)  # 与真实 service 一样拒绝非法迁移
        task["state"] = state
        task["error_code"] = error_code
        task["error_message"] = error_message
        if state in (self.S.RETRYABLE, self.S.EXPIRED, self.S.CANCELLED, self.S.COMPLETED, self.S.FAILED):
            self._release(task_id)
        return self._snapshot(task_id)

    def release(self, task_id, *, attempt_id, fencing_token, terminal_state=None):
        self._record("release", task_id=task_id)
        self._require_lease(task_id, attempt_id, fencing_token)
        task = self._task(task_id)
        task["state"] = terminal_state or self.S.RETRYABLE
        self._release(task_id)
        self.release_calls.append(task_id)
        return self._snapshot(task_id)

    # -- 范围锁 -------------------------------------------------------------
    def acquire_scope(self, task_id, *, attempt_id, fencing_token, scopes):
        self._record("acquire_scope", task_id=task_id, scopes=tuple(scopes))
        lease = self._require_lease(task_id, attempt_id, fencing_token)
        wanted = tuple(scopes)
        for other_id, held in self.scope_locks.items():
            if other_id == task_id:
                continue
            hits = _overlap(wanted, held)
            if hits:
                holder = self.leases.get(other_id) or {}
                raise self.S.ScopeConflict(
                    task_id, other_id, holder.get("agent_id", ""), hits
                )
        self.scope_locks[task_id] = wanted
        return None

    def release_scope(self, task_id, *, attempt_id, fencing_token) -> bool:
        self._record("release_scope", task_id=task_id)
        self._require_lease(task_id, attempt_id, fencing_token)
        self.scope_release_calls.append(task_id)
        return self.scope_locks.pop(task_id, None) is not None

    def scope_holders(self):
        return {tid: (self.leases.get(tid, {}).get("agent_id", ""), scopes) for tid, scopes in self.scope_locks.items()}

    # -- 完成 ---------------------------------------------------------------
    def complete(self, task_id, *, attempt_id, fencing_token, idempotency_key, summary, terminal_status=None):
        self._record("complete", task_id=task_id, idempotency_key=idempotency_key)
        status = terminal_status or self.S.COMPLETED
        task = self._task(task_id)
        for done in task["completions"]:  # 幂等键重放：返回既有结果，不产生副作用
            if done["idempotency_key"] == idempotency_key:
                return SimpleNamespace(
                    task_id=task_id,
                    terminal_status=done["status"],
                    idempotency_key=idempotency_key,
                    created=False,
                    outbox_ops=0,
                )
        self._require_lease(task_id, attempt_id, fencing_token)
        self.S.assert_transition(task["state"], status)  # 与真实 service 一致
        task["state"] = status
        task["completions"].append(
            {"idempotency_key": idempotency_key, "summary": summary, "status": status}
        )
        self.completions.append(
            {"task_id": task_id, "status": status, "summary": summary, "key": idempotency_key}
        )
        self.outbox.append(
            {"task_id": task_id, "kind": status, "completion_id": idempotency_key, "mirrored": False}
        )
        self._release(task_id)
        return SimpleNamespace(
            task_id=task_id,
            terminal_status=status,
            idempotency_key=idempotency_key,
            created=True,
            outbox_ops=1,
        )

    # -- 取消 ---------------------------------------------------------------
    def request_cancel(self, task_id, *, reason="") -> SimpleNamespace:
        self._record("request_cancel", task_id=task_id, reason=reason)
        task = self._task(task_id)
        if task["state"] in self.S.TERMINAL_STATES:
            return self._snapshot(task_id)
        if self.leases.get(task_id) is not None:
            self.S.assert_transition(task["state"], self.S.CANCEL_REQUESTED)
            task["state"] = self.S.CANCEL_REQUESTED
        else:
            task["state"] = self.S.CANCELLED
        self.cancel_ids.add(task_id)
        return self._snapshot(task_id)

    def is_cancel_requested(self, task_id) -> bool:
        task = self.tasks.get(task_id)
        return task_id in self.cancel_ids or (
            task is not None and task["state"] == self.S.CANCEL_REQUESTED
        )

    def ack_cancel(self, task_id, *, attempt_id, fencing_token) -> SimpleNamespace:
        self._record("ack_cancel", task_id=task_id)
        task = self._task(task_id)
        if task["state"] != self.S.CANCEL_REQUESTED:
            raise self.S.InvalidTransition(task["state"], self.S.CANCELLED)
        self._require_lease(task_id, attempt_id, fencing_token)
        task["state"] = self.S.CANCELLED
        self._release(task_id)
        self.ack_calls.append(task_id)
        return self._snapshot(task_id)

    # -- 回收 / outbox ------------------------------------------------------
    def reap_expired(self, now=None):
        return SimpleNamespace(expired_leases=(), released_locks=(), retryable=(), orphan_attempts=())

    def pending_outbox(self, limit=100):
        return [SimpleNamespace(**op) for op in self.outbox if not op["mirrored"]][:limit]

    def mark_outbox(self, op_id, *, status, error=""):
        return None

    # -- 分派 / 审计（Worker 不用，留空实现便于鸭子类型） -------------------
    def register_dispatch(self, dispatch_key, **kwargs):
        return True

    def dispatch_state(self, dispatch_key):
        return None

    def set_dispatch_status(self, dispatch_key, status, *, comment_posted=None):
        return None

    def list_audit(self, task_id, limit=200):
        return []


class FakeMirror:
    """GitHubMirror 替身：drain 契约 = drain(scheduler, limit=100) -> int。"""

    def __init__(self) -> None:
        self.drains = 0
        self.applied = 0
        self.fail_next = 0

    def drain(self, scheduler, limit=100) -> int:
        self.drains += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("模拟镜像断网")
        pending = [op for op in scheduler.outbox if not op.get("mirrored")]
        for op in pending[:limit]:
            op["mirrored"] = True
        self.applied += len(pending[:limit])
        return len(pending[:limit])


@pytest.fixture()
def scheduler(clock) -> FakeScheduler:
    return FakeScheduler(clock)


@pytest.fixture()
def mirror() -> FakeMirror:
    return FakeMirror()


def make_worker(
    transport,
    git_repo,
    clock,
    scheduler,
    mirror=None,
    *,
    sleep=None,
    runner=None,
    **overrides,
):
    """构造一个全部依赖可注入的 Worker（默认假时钟 sleep 推进、执行器宽限很短）。"""
    agent_id = overrides.pop("agent_id", "muse-2")
    adapter = gi.GitHubIssueAdapter(
        transport, agent_id, clock=clock, lease_ttl=timedelta(hours=4)
    )
    cfg_kwargs = dict(
        agent_id=agent_id,
        poll_interval_s=5,
        recovery_backoff_s=10,
        recovery_budget_s=900,
        scope_wait_timeout_s=120,
        cancel_poll_interval_s=0.02,
        lease_renew_interval_s=10**6,
    )
    cfg_kwargs.update(overrides)
    executor = ex.Executor(term_grace_s=0.2, kill_wait_s=2.0, poll_interval_s=0.02)
    return wk.AgentWorker(
        adapter,
        wt.WorktreeAllocator(git_repo),
        executor,
        wk.WorkerConfig(**cfg_kwargs),
        scheduler,
        mirror=mirror,
        clock=clock,
        sleep=sleep or (lambda seconds: clock.advance(seconds)),
        runner=runner,
    )
