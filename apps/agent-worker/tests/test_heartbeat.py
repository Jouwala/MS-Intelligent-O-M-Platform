"""心跳验收（TASK-03 / TASK-14）。

覆盖：长任务执行期持续心跳（假时钟推进 200s / 30 分钟不 stale）、
原子写的并发读、payload 字段、停止线程幂等、解析失败按 mtime 兜底。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

from conftest import hb


def _writer(tmp_path, clock, **kwargs):
    return hb.HeartbeatWriter(
        tmp_path / "hb.json",
        agent_id="muse-2",
        instance_id="inst-1",
        clock=clock,
        **kwargs,
    )


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class GateSleeper:
    """每拍写完后「睡」30 秒（推进假时钟），由测试放行下一拍。"""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.release = threading.Event()
        self.arrived = threading.Event()

    def __call__(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.arrived.set()
        self.release.wait(3.0)
        self.release.clear()
        self.arrived.clear()


def test_payload_contains_agent_instance_pid_at_task_state(tmp_path, clock):
    writer = _writer(tmp_path, clock)
    writer.update(task_id="issue-42", state="running")
    writer.write()
    data = json.loads((tmp_path / "hb.json").read_text(encoding="utf-8"))
    assert data["agent"] == "muse-2"
    assert data["instance"] == "inst-1"
    assert data["pid"] == os.getpid()
    assert data["at"] == "2026-11-14T02:00:00+00:00"
    assert data["task"] == "issue-42"
    assert data["state"] == "running"


def test_background_heartbeat_keeps_30min_task_not_stale(tmp_path, clock):
    """模拟 30 分钟长任务：心跳线程每 30s 刷新，任何时刻年龄都不超过一个周期。"""
    gate = GateSleeper(clock)
    writer = _writer(tmp_path, clock, interval_s=30, sleep=gate)
    writer.start()
    try:
        assert _wait_until(lambda: writer.writes >= 1)
        for _ in range(60):  # 60 拍 × 30s = 1800s = 30 分钟
            assert gate.arrived.wait(3.0)
            gate.release.set()
            assert _wait_until(lambda: writer.writes >= 2)
            age = hb.read_heartbeat_age(tmp_path / "hb.json", clock().timestamp())
            assert age is not None and age <= 30.0
    finally:
        gate.release.set()
        writer.stop()
    assert not writer.running


def test_200s_task_stale_without_refresh_but_fresh_with_it(tmp_path, clock):
    """旧缺陷复现：executor 跑 200s 只写一次心跳 → 180s 阈值下必然误杀。"""
    path = tmp_path / "hb-static.json"
    static = hb.HeartbeatWriter(
        path, agent_id="muse-2", instance_id="i", clock=clock
    )
    static.write()
    clock.advance(200)
    assert hb.read_heartbeat_age(path, clock().timestamp()) > 180  # 会被误杀

    # 后台线程持续刷新后，同样的 200s 执行期心跳始终新鲜
    gate = GateSleeper(clock)
    live = hb.HeartbeatWriter(
        tmp_path / "hb-live.json",
        agent_id="muse-2",
        instance_id="i",
        interval_s=30,
        clock=clock,
        sleep=gate,
    )
    live.start()
    try:
        assert _wait_until(lambda: live.writes >= 1)
        for _ in range(7):  # 7 × 30s = 210s > 180s
            assert gate.arrived.wait(3.0)
            gate.release.set()
            assert _wait_until(lambda: live.writes >= 2)
            age = hb.read_heartbeat_age(tmp_path / "hb-live.json", clock().timestamp())
            assert age is not None and age <= 30.0
    finally:
        gate.release.set()
        live.stop()


def _read_json_with_retry(path, attempts: int = 100) -> dict:
    """真实看门狗也会容忍瞬时打开失败（Windows 共享冲突）；重试不是放宽断言，
    因为最终仍要求读到**完整可解析**的 JSON。"""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, FileNotFoundError) as exc:
            last = exc
            time.sleep(0.002)
    raise AssertionError(f"并发读写持续失败，最后一次：{last!r}")


def test_atomic_write_concurrent_reads_never_see_partial_json(tmp_path):
    """写方 10ms 一拍、读方连续读：原子替换保证永远能解析出完整 JSON。"""
    writer = hb.HeartbeatWriter(
        tmp_path / "hb.json",
        agent_id="muse-2",
        instance_id="inst-1",
        interval_s=0.01,
        clock=lambda: datetime.now(timezone.utc),
    )
    writer.start()
    try:
        assert _wait_until(lambda: (tmp_path / "hb.json").exists())
        for _ in range(300):
            data = _read_json_with_retry(tmp_path / "hb.json")
            assert data["agent"] == "muse-2"  # 绝不读到半截 JSON
            time.sleep(0.002)  # 模拟看门狗「读一下就松开」的真实占空比
    finally:
        writer.stop()
    assert writer.writes >= 2
    assert not list(tmp_path.glob("hb.json.tmp*"))  # 临时文件不残留


def test_stop_is_idempotent_and_stops_writes(tmp_path, clock):
    writer = _writer(tmp_path, clock, interval_s=0.01)
    writer.start()
    assert writer.running
    writer.stop()
    assert not writer.running
    before = writer.writes
    time.sleep(0.05)
    assert writer.writes == before
    writer.stop()  # 再停一次不抛异常


def test_read_age_falls_back_to_mtime_on_corrupt_json(tmp_path, clock):
    path = tmp_path / "hb.json"
    path.write_text("{不是 JSON", encoding="utf-8")
    now = clock().timestamp()
    os.utime(path, (now, now))
    assert hb.read_heartbeat_age(path, now) == 0.0  # 解析失败不得当死亡证据

    os.utime(path, (now - 120, now - 120))
    age = hb.read_heartbeat_age(path, now)
    assert age is not None and abs(age - 120) < 1


def test_read_age_missing_file_is_none(tmp_path):
    assert hb.read_heartbeat_age(tmp_path / "nope.json", time.time()) is None
