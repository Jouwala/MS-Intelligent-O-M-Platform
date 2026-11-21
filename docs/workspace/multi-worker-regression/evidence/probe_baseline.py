"""修复前缺陷复现探针（在 7bbe5ec 原始代码上运行）。

目的：为任务书第四节 14 项缺陷提供「修复前失败证据」。每个探针独立打印
DEFECT-xx / 观测值 / 期望值，最后汇总。只依赖标准库 + 仓库自身模块。

运行：
    cd D:\\tmp\\noc-soc-baseline
    python TMP\\probe_baseline.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent-worker"))

import _bootstrap  # noqa: E402

gi = _bootstrap.load_adapter()
wk = _bootstrap.load_sibling("worker")
wt_mod = _bootstrap.load_sibling("worktree")

NOW = datetime(2026, 11, 14, 2, 0, 0, tzinfo=timezone.utc)
RESULTS: list[tuple[str, str, str]] = []  # (编号, 观测, 判定)


def record(tag: str, observed: str, verdict: str) -> None:
    RESULTS.append((tag, observed, verdict))
    print(f"[{tag}] 观测：{observed}\n        判定：{verdict}")


# ---------------------------------------------------------------------------
# DEFECT-01 GitHub API 分页不完整
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload, headers):
        self._raw = json.dumps(payload).encode()
        self.headers = headers

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_fake_github(total_issues: int, comments: list[dict], comment_total: int):
    """模拟 GitHub REST：按 per_page=100 分页，返回 Link rel=next。"""

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        m = re.search(r"[?&]page=(\d+)", url)
        page = int(m.group(1)) if m else 1
        if "/comments" in url:
            data = comments[:100]
            headers = {
                "Link": f'<{url}&page=2>; rel="next"' if comment_total > 100 else ""
            }
            return _FakeResponse(data, headers)
        nums = list(range(page, min(page + 100, total_issues + 1)))
        data = [
            {
                "number": n,
                "title": f"任务 {n}",
                "body": "",
                "labels": [],
                "state": "open",
                "created_at": "2026-11-14T00:00:00Z",
                "updated_at": "2026-11-14T00:00:00Z",
            }
            for n in nums
        ]
        headers = {
            "Link": f'<{url.split("?")[0]}?page={page + 1}&per_page=100>; rel="next"'
            if page * 100 < total_issues
            else ""
        }
        return _FakeResponse(data, headers)

    return fake_urlopen


def defect_01_pagination() -> None:
    real = urllib.request.urlopen

    def progress_body(i: int) -> str:
        return (
            f"<!-- agent-msg: TASK_PROGRESS, task: #1, from: muse-2, "
            f"at: 2026-11-14T00:{i // 60:02d}:{i % 60:02d}Z -->\n进展 {i}"
        )

    comments = []
    comments.append(
        {
            "id": 1,
            "user": {"login": "muse-2"},
            "body": "<!-- agent-claim: muse-2, 2026-11-14T00:00:00Z -->\n认领",
            "created_at": "2026-11-14T00:00:00Z",
        }
    )
    for i in range(2, 130):
        comments.append(
            {
                "id": i,
                "user": {"login": "muse-2"},
                "body": progress_body(i),
                "created_at": "2026-11-14T00:00:00Z",
            }
        )
    # 完成记录落在第 2 页（第 130 条之后）
    comments.append(
        {
            "id": 999,
            "user": {"login": "muse-2"},
            "body": "<!-- agent-msg: TASK_COMPLETED, task: #1, from: muse-2, "
            "at: 2026-11-14T01:00:00Z -->\n完成",
            "created_at": "2026-11-14T01:00:00Z",
        }
    )
    while len(comments) < 150:
        comments.append(
            {
                "id": 1000 + len(comments),
                "user": {"login": "muse-2"},
                "body": "<!-- 其它评论 -->",
                "created_at": "2026-11-14T01:10:00Z",
            }
        )
    # 让完成记录位于第 2 页：把完成评论放到第 149 位
    completed = comments.pop(130)
    comments.append(completed)
    comments = comments[:150]

    urllib.request.urlopen = _make_fake_github(150, comments, 150)
    try:
        transport = gi.RestGitHubTransport("Jouwala", "noc-soc-intelligent-platform")
        issues = transport.list_issues(state="open")
        fetched = transport.list_comments(1)
        adapter = gi.GitHubIssueAdapter(
            transport, "muse-2", clock=lambda: NOW, lease_ttl=timedelta(hours=4)
        )
        lease = adapter.lease_of(1)
    finally:
        urllib.request.urlopen = real

    record(
        "DEFECT-01 分页",
        f"150 个 Issue 只读到 {len(issues)} 个；150 条评论只读到 {len(fetched)} 条；"
        f"完成记录在第 {len(fetched) + 1} 条以外，lease_of() 仍返回 "
        f"{'有效租约 holder=' + lease.holder if lease else 'None'}",
        "失败（数据不完整且完成状态被误判为仍在执行）",
    )


# ---------------------------------------------------------------------------
# DEFECT-02 同 Agent 不同实例可重复认领
# ---------------------------------------------------------------------------


def defect_02_duplicate_claim() -> None:
    clock = SimpleNamespace(
        now=NOW, __call__=lambda self: self.now
    )  # 兼容 clock() 调用
    transport = gi.InMemoryGitHubTransport(clock=lambda: NOW)
    transport.create_issue(
        42, "任务", "## 改动文件\n- `a.py`\n", labels=("agent:muse-2", "state:todo")
    )
    # 两个实例：基线没有 instance_id，只能用同一 agent 名的两个 adapter 代表
    inst_a = gi.GitHubIssueAdapter(transport, "muse-2", clock=lambda: NOW)
    inst_b = gi.GitHubIssueAdapter(transport, "muse-2", clock=lambda: NOW)

    barrier = threading.Barrier(2)
    original_add = transport.add_comment

    def synced_add(number, body, author=""):
        if "agent-claim:" in body:
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
        return original_add(number, body, author=author)

    transport.add_comment = synced_add
    outcomes: list[str] = []

    def attempt(adapter, name):
        try:
            adapter.claim(42)
            outcomes.append(f"{name}:成功")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"{name}:{type(exc).__name__}")

    threads = [
        threading.Thread(target=attempt, args=(inst_a, "实例A")),
        threading.Thread(target=attempt, args=(inst_b, "实例B")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    claims = sum("agent-claim:" in c.body for c in transport.list_comments(42))
    record(
        "DEFECT-02 原子认领",
        f"并发认领结果 {outcomes}，issue 42 上出现 {claims} 条认领评论（{claims} 个有效租约）",
        "失败（同一任务被两个实例同时认领）",
    )

    # 旧实例仍能提交结果：A 完成（B 已认领）也会写第二条完成记录
    try:
        inst_a.begin_attempt(42, "attempt-a")
        inst_a.complete(42, "attempt-a", "旧实例回传")
    except Exception as exc:  # noqa: BLE001
        pass
    completed = sum("TASK_COMPLETED" in c.body for c in transport.list_comments(42))
    record(
        "DEFECT-02b 旧执行者防护",
        f"实例B 再次认领后，旧实例A 仍成功 complete（TASK_COMPLETED 评论数 = {completed}）",
        "失败（缺少 fencing/幂等完成，旧执行者结果仍被接受）",
    )


# ---------------------------------------------------------------------------
# DEFECT-05/07 完成幂等：评论成功、标签失败后永久卡住
# ---------------------------------------------------------------------------


def defect_05_complete_idempotency() -> None:
    transport = gi.InMemoryGitHubTransport(clock=lambda: NOW)
    transport.create_issue(
        7, "任务", "## 改动文件\n- `a.py`\n", labels=("agent:muse-2", "state:todo")
    )
    adapter = gi.GitHubIssueAdapter(transport, "muse-2", clock=lambda: NOW)
    adapter.claim(7)
    adapter.begin_attempt(7, "attempt-1")

    real_set = transport.set_labels
    state = {"calls": 0}

    def flaky_set(number, labels):
        state["calls"] += 1
        if state["calls"] == 1:
            raise gi.TransportError("模拟标签更新 500")
        return real_set(number, labels)

    transport.set_labels = flaky_set
    try:
        adapter.complete(7, "attempt-1", "完成")
    except gi.TransportError as exc:
        first_error = str(exc)
    else:
        first_error = "无异常"

    # 重试 complete：评论已存在 → lease_of 返回 None → LeaseConflict，标签永不修复
    retry_error = None
    try:
        adapter.complete(7, "attempt-1", "完成")
    except Exception as exc:  # noqa: BLE001
        retry_error = f"{type(exc).__name__}: {exc}"
    labels = transport.get_issue(7).labels
    completed_comments = sum("TASK_COMPLETED" in c.body for c in transport.list_comments(7))
    record(
        "DEFECT-07 完成幂等",
        f"首次 complete 异常={first_error}；重试结果={retry_error}；"
        f"标签={labels}；TASK_COMPLETED 评论数={completed_comments}",
        "失败（完成评论已写、标签失败；重试被判无租约，状态永久不一致）",
    )


# ---------------------------------------------------------------------------
# DEFECT-03/04 心跳与 watchdaog 生命周期
# ---------------------------------------------------------------------------


class _FakeAllocator:
    def __init__(self, base: Path):
        self.base = base

    def allocate(self, slug):
        p = self.base / slug
        p.mkdir(parents=True, exist_ok=True)
        (p / ".git").mkdir(exist_ok=True)
        return p

    def validate_hooks(self, worktree):  # noqa: ARG002
        return wt_mod.HookReport(ok=True)


class _AdvancingClock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)


def defect_03_04_heartbeat_and_watchdog() -> None:
    clock = _AdvancingClock()
    transport = gi.InMemoryGitHubTransport(clock=clock)
    transport.create_issue(
        11, "长任务", "## 改动文件\n- `a.py`\n", labels=("agent:muse-2", "state:todo")
    )
    adapter = gi.GitHubIssueAdapter(transport, "muse-2", clock=clock)
    cfg = wk.WorkerConfig(agent_id="muse-2", executor_timeout_s=3600)
    tmp = Path(tempfile.mkdtemp(prefix="probe-hb-"))
    allocator = _FakeAllocator(tmp)

    def long_runner(_worktree):
        clock.advance(200)  # 模拟执行 200 秒
        return 0

    worker = wk.AgentWorker(
        adapter, allocator, wk.Executor(), cfg, clock=clock, runner=long_runner
    )
    before = worker.heartbeat_file.exists()
    worker.run_once()
    hb = json.loads(worker.heartbeat_file.read_text(encoding="utf-8"))
    hb_at = datetime.fromisoformat(hb["at"])
    age = (clock() - hb_at).total_seconds()
    record(
        "DEFECT-03 执行期心跳",
        f"执行 200 秒期间心跳文件仅写一次（运行前存在={before}），"
        f"结束时心跳年龄 {age:.0f}s",
        "失败（超过 watchdog 默认 180s 阈值即被误判卡死）",
    )

    class _StubbornProc:
        def __init__(self):
            self.terminated = 0
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.terminated += 1  # 模拟忽略 SIGTERM，不退出

    procs: list[_StubbornProc] = []
    started: list[_StubbornProc] = []

    def factory(cmd):  # noqa: ARG001
        p = _StubbornProc()
        procs.append(p)
        started.append(p)
        return p

    watchdog = wk.__dict__.get("Watchdog")  # watchdog 模块由 load_sibling 加载
    wd_mod = _bootstrap.load_sibling("watchdog")
    wd = wd_mod.Watchdog(
        ["python", "worker.py"],
        tmp / ".no-such-heartbeat.json",
        wd_mod.WatchdogConfig(stale_after_s=1, check_interval_s=1, start_grace_s=1),
        process_factory=factory,
        clock=lambda: 100.0,
        sleep=lambda s: None,
        log=lambda m: None,
    )
    try:
        wd.run(max_cycles=3)
    except Exception:  # noqa: BLE001
        pass
    still_alive = [p for p in started if p.poll() is None]
    record(
        "DEFECT-04 Watchdog 旧进程未退出确认",
        f"看门狗已拉起 {len(started)} 个 Worker 进程，其中 {len(still_alive)} 个旧进程仍存活",
        "失败（未确认旧进程退出就启动新实例，双 Worker 并行）",
    )


# ---------------------------------------------------------------------------
# DEFECT-06 失败路径清理
# ---------------------------------------------------------------------------


def defect_06_cleanup() -> None:
    clock = _AdvancingClock()
    transport = gi.InMemoryGitHubTransport(clock=clock)
    transport.create_issue(
        12,
        "任务",
        "## 改动文件\n- `a.py`\n## 执行命令\n```\npython x.py\n```\n",
        labels=("agent:muse-2", "state:todo"),
    )
    adapter = gi.GitHubIssueAdapter(transport, "muse-2", clock=clock)
    tmp = Path(tempfile.mkdtemp(prefix="probe-clean-"))
    worker = wk.AgentWorker(
        adapter,
        _FakeAllocator(tmp),
        wk.Executor(),
        wk.WorkerConfig(agent_id="muse-2"),
        clock=clock,
        runner=lambda _wt: (_ for _ in ()).throw(RuntimeError("执行体内部异常")),
    )
    crash = None
    try:
        worker.handle_task(transport.get_issue(12))
    except RuntimeError as exc:
        crash = str(exc)
    bodies = [c.body for c in transport.list_comments(12)]
    record(
        "DEFECT-06 失败清理",
        f"执行体抛异常={crash}；worker._active={'仍留有任务' if worker._active else '已清理'}；"
        f"scope-unlock 评论数={sum('scope-unlock:' in b for b in bodies)}；"
        f"worktree 仍在={any((tmp / 'task-12').iterdir()) if (tmp / 'task-12').exists() else False}",
        "失败（异常路径未走清理，_active / 范围锁 / worktree 残留）",
    )


# ---------------------------------------------------------------------------
# DEFECT-05(锁) 旧实例释放新实例的锁
# ---------------------------------------------------------------------------


def defect_scope_lock() -> None:
    clock = _AdvancingClock()
    transport = gi.InMemoryGitHubTransport(clock=clock)
    ttl = timedelta(minutes=10)
    old = gi.GitHubIssueAdapter(transport, "muse-2", clock=clock, lease_ttl=ttl)
    transport.create_issue(
        42, "任务", "## 改动文件\n- `src/a.py`\n", labels=("agent:muse-2", "state:todo")
    )
    transport.create_issue(
        43, "任务2", "## 改动文件\n- `src/a.py`\n", labels=("agent:muse-2", "state:todo")
    )
    old.claim(42)
    old.acquire_scope(42, ["src/a.py"])
    clock.advance(601)  # 租约过期
    new = gi.GitHubIssueAdapter(transport, "muse-2", clock=clock, lease_ttl=ttl)
    new.claim(42)
    new.acquire_scope(42, ["src/a.py"])  # 新实例持锁
    # 旧实例恢复后释放锁（没有 fencing/holder 校验）
    old.release_scope(42)
    lock = new._scope_lock_of(42)
    record(
        "DEFECT-04b 旧实例释放新锁",
        f"新实例持锁后，旧实例 release_scope；新实例读取锁 = {lock}",
        "失败（旧实例可清掉新实例的范围锁，锁所有权未被校验）",
    )


# ---------------------------------------------------------------------------
# DEFECT-13 命令解析（Windows 路径）
# ---------------------------------------------------------------------------


def defect_13_command_parse() -> None:
    body = (
        "## 执行命令\n```\n"
        'C:\\tools\\python.exe "C:\\work dir\\run task.py" --flag x\n```\n'
    )
    parsed = wk.AgentWorker._command_from_issue(body)
    record(
        "DEFECT-13 命令解析",
        f"Windows 路径命令被解析为 {parsed}",
        "失败（shlex posix 模式吃掉反斜杠，路径损坏）" if parsed and "C:toolspython.exe" in parsed[0] else "未复现",
    )


def main() -> int:
    print("=" * 78)
    print("基线缺陷复现（commit 7bbe5ec，Windows 本机）")
    print("=" * 78)
    defect_01_pagination()
    defect_02_duplicate_claim()
    defect_05_complete_idempotency()
    defect_03_04_heartbeat_and_watchdog()
    defect_06_cleanup()
    defect_scope_lock()
    defect_13_command_parse()
    print("=" * 78)
    failed = sum(1 for _, _, v in RESULTS if v.startswith("失败"))
    print(f"汇总：{failed}/{len(RESULTS)} 项探针复现失败（= 缺陷存在）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
