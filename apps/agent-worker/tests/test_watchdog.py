"""看门狗验收（TASK-03）：心跳阈值、解析容错、重启不漏双实例、信号停止。"""

from __future__ import annotations

import json
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import hb, wd

CMD = ["python", "worker.py"]


def _write_heartbeat(path: Path, at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"agent": "muse-2", "instance": "i", "pid": 1, "at": at.isoformat(),
             "task": "", "state": "idle"}
        ),
        encoding="utf-8",
    )


def _config(**overrides):
    base = dict(
        stale_after_s=180.0,
        check_interval_s=1.0,
        heartbeat_interval_s=30.0,
        max_restarts=3,
        start_grace_s=0.0,
        term_grace_s=0.05,
        kill_wait_s=0.5,
    )
    base.update(overrides)
    return wd.WatchdogConfig(**base)


class FakeProcess:
    """pid=None → 看门狗走 terminate()/kill() 回退分支（跨平台安全）。"""

    def __init__(self, events: list, *, ignore_term: bool = False) -> None:
        self.pid = None
        self.returncode = None
        self.ignore_term = ignore_term
        self.events = events
        self.waited = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.events.append("terminate")
        if not self.ignore_term:
            self.returncode = -15

    def kill(self):
        self.events.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited += 1
        return self.returncode


def _watchdog(tmp_path, *, factory, clock, sleep, config, log=None):
    hb_path = tmp_path / ".agent-worktrees" / ".heartbeat-muse-2.json"
    return wd.Watchdog(
        CMD,
        hb_path,
        config,
        process_factory=factory,
        clock=clock,
        sleep=sleep,
        log=log.append if log is not None else (lambda _m: None),
    )


def test_defaults_stale_much_larger_than_heartbeat_interval():
    cfg = wd.WatchdogConfig()
    assert cfg.heartbeat_interval_s == 30.0
    assert cfg.stale_after_s >= 3 * cfg.heartbeat_interval_s  # 180s = 6 个心跳周期


def test_no_heartbeat_uses_start_grace(tmp_path, float_clock):
    cfg = _config(start_grace_s=60, stale_after_s=180)
    dog = _watchdog(tmp_path, factory=lambda cmd: FakeProcess([]), clock=float_clock,
                    sleep=lambda s: float_clock.advance(s), config=cfg)
    dog._start()
    float_clock.advance(30)
    assert dog._is_stale() is False  # 宽限期内没有心跳不算死
    float_clock.advance(40)
    assert dog._is_stale() is True


def test_corrupt_heartbeat_uses_mtime_not_death(tmp_path, float_clock):
    cfg = _config(stale_after_s=180, start_grace_s=0)
    dog = _watchdog(tmp_path, factory=lambda cmd: FakeProcess([]), clock=float_clock,
                    sleep=lambda s: float_clock.advance(s), config=cfg)
    dog._start()
    float_clock.advance(300)  # 启动后 300s（旧心跳修法要求证据晚于启动时刻）
    dog.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    now = float_clock()
    dog.heartbeat_file.write_text("半截 JS", encoding="utf-8")
    import os

    os.utime(dog.heartbeat_file, (now, now))
    assert dog._is_stale() is False  # 解析失败但 mtime 新鲜 → 不得当死亡
    os.utime(dog.heartbeat_file, (now - 200, now - 200))  # 晚于启动、年龄 >180
    assert dog._is_stale() is True   # 能解析不出且 mtime 也旧，才算死亡证据


def test_fresh_at_heartbeat_not_stale_over_30min_when_refreshed(tmp_path, float_clock):
    """30 分钟（假时钟）持续刷新心跳：任何一轮检查都不得判卡死。"""
    cfg = _config(stale_after_s=180, start_grace_s=0)
    dog = _watchdog(tmp_path, factory=lambda cmd: FakeProcess([]), clock=float_clock,
                    sleep=lambda s: float_clock.advance(s), config=cfg)
    dog._start()
    for _ in range(60):  # 60 × 30s = 30 分钟
        _write_heartbeat(
            dog.heartbeat_file,
            datetime.fromtimestamp(float_clock(), tz=timezone.utc),
        )
        assert dog._is_stale() is False
        float_clock.advance(30)
    # 停止刷新 200s（>180s 阈值）后才判卡死
    float_clock.advance(200)
    assert dog._is_stale() is True


def test_restart_never_starts_new_before_old_confirmed_exit(tmp_path, float_clock, clock):
    """旧进程忽略 SIGTERM → 宽限后 SIGKILL；工厂第二次被调用时旧进程必须已退出。"""
    events: list[str] = []
    procs: list[FakeProcess] = []
    logs: list[str] = []
    first = FakeProcess(events, ignore_term=True)

    def factory(_cmd):
        proc = first if not procs else FakeProcess(events)
        procs.append(proc)
        assert all(p.returncode is not None for p in procs[:-1]), "旧实例未确认退出就拉起新实例"
        return proc

    dog = _watchdog(
        tmp_path,
        factory=factory,
        clock=float_clock,
        sleep=lambda s: float_clock.advance(s),
        config=_config(check_interval_s=0.01, start_grace_s=0.0, max_restarts=3),
        log=logs,
    )
    assert dog.run(max_cycles=1) == 0
    assert len(procs) == 2                  # 确实重启了一次
    assert "terminate" in events and "kill" in events
    assert first.returncode == -9           # 忽略 SIGTERM 后升级 SIGKILL
    assert procs[1].returncode is not None  # 收尾停掉了新实例
    assert any("SIGKILL" in line for line in logs)


def test_max_restarts_returns_2(tmp_path, float_clock):
    class Exited(FakeProcess):
        def __init__(self, events):
            super().__init__(events)
            self.returncode = 0

    dog = _watchdog(
        tmp_path,
        factory=lambda cmd: Exited([]),
        clock=float_clock,
        sleep=lambda s: float_clock.advance(s),
        config=_config(check_interval_s=0.01, max_restarts=2),
    )
    assert dog.run() == 2
    assert dog.restarts == 2


def test_stop_request_terminates_child_and_exits_zero(tmp_path, float_clock):
    events: list[str] = []
    proc = FakeProcess(events)
    dog = _watchdog(
        tmp_path,
        factory=lambda cmd: proc,
        clock=float_clock,
        sleep=lambda s: float_clock.advance(s),
        config=_config(check_interval_s=0.01),
    )
    dog.request_stop()
    assert dog.run() == 0
    assert proc.returncode is not None
    assert "terminate" in events


def test_sigterm_handler_requests_stop(tmp_path, float_clock):
    dog = _watchdog(
        tmp_path,
        factory=lambda cmd: FakeProcess([]),
        clock=float_clock,
        sleep=lambda s: float_clock.advance(s),
        config=_config(),
    )
    dog._handle_signal(signal.SIGTERM, None)
    assert dog._stop_requested is True


def test_old_heartbeat_from_previous_instance_does_not_trigger_restart(tmp_path, float_clock):
    """旧进程留下 200s 前的心跳文件：新实例刚拉起时不得据此立即重启。"""
    events: list[str] = []
    procs: list[FakeProcess] = []

    def factory(_cmd):
        proc = FakeProcess(events)
        procs.append(proc)
        return proc

    heartbeat = tmp_path / ".agent-worktrees" / ".heartbeat-muse-2.json"
    _write_heartbeat(
        heartbeat, datetime.fromtimestamp(float_clock() - 200, tz=timezone.utc)
    )
    dog = _watchdog(
        tmp_path,
        factory=factory,
        clock=float_clock,
        sleep=lambda s: float_clock.advance(s),
        config=_config(check_interval_s=10, start_grace_s=60, max_restarts=3),
    )
    assert dog.run(max_cycles=2) == 0
    assert len(procs) == 1  # 没有因为旧心跳文件被立即重启
    assert dog.restarts == 0
    assert dog._is_stale() is False


def test_old_heartbeat_restarts_once_after_start_grace(tmp_path, float_clock):
    """宽限期过且始终没有本次实例的新心跳 → 恰好重启一次。"""
    events: list[str] = []
    procs: list[FakeProcess] = []

    def factory(_cmd):
        proc = FakeProcess(events)
        procs.append(proc)
        return proc

    heartbeat = tmp_path / ".agent-worktrees" / ".heartbeat-muse-2.json"
    _write_heartbeat(
        heartbeat, datetime.fromtimestamp(float_clock() - 200, tz=timezone.utc)
    )
    dog = _watchdog(
        tmp_path,
        factory=factory,
        clock=float_clock,
        sleep=lambda s: float_clock.advance(s),
        config=_config(check_interval_s=10, start_grace_s=20, max_restarts=3),
    )
    assert dog.run(max_cycles=3) == 0
    assert dog.restarts == 1
    assert len(procs) == 2


def test_fresh_heartbeat_from_this_instance_keeps_not_stale(tmp_path, float_clock):
    """本次实例写出的心跳仍按 stale_after_s 判定（没有被旧心跳修法绕过）。"""
    dog = _watchdog(
        tmp_path,
        factory=lambda cmd: FakeProcess([]),
        clock=float_clock,
        sleep=lambda s: float_clock.advance(s),
        config=_config(stale_after_s=180, start_grace_s=0),
    )
    dog._start()
    _write_heartbeat(
        dog.heartbeat_file, datetime.fromtimestamp(float_clock(), tz=timezone.utc)
    )
    assert dog._is_stale() is False
    float_clock.advance(181)
    assert dog._is_stale() is True  # 过期心跳仍然算死亡证据


def test_heartbeat_age_helper_returns_none_for_missing(tmp_path, float_clock):
    cfg = _config()
    dog = _watchdog(tmp_path, factory=lambda cmd: FakeProcess([]), clock=float_clock,
                    sleep=lambda s: float_clock.advance(s), config=cfg)
    assert dog.heartbeat_age() is None
    _write_heartbeat(dog.heartbeat_file, datetime.fromtimestamp(float_clock(), tz=timezone.utc))
    age = dog.heartbeat_age()
    assert age is not None and age <= 0.01
