# -*- coding: utf-8 -*-
"""CN KPI 冷缓存治理的回归守卫（2026-11-18，TODO P1）。

旧行为：TTL 到期后请求**同步**跑 18 个组件，冷时超过 nginx 300s → 浏览器 504，
`?page=cn_kpi` 只剩空壳。新行为：立即返回「旧值 + refreshing」或「computing」，
刷新在后台线程做，结果落盘 last-known-good。

这里锁三件事：
1. 没有旧值时立即返回 `computing=True`（不阻塞、不抛错），后台算完后下次请求直接出数；
2. 有旧值时立即返回旧值（`stale=True`），并触发后台刷新；
3. 进程重启后（内存清空）能从磁盘 last-known-good 恢复，不回到「空壳」。
"""
import json
import threading
import time

import pytest

from backend.services.cn import cn_kpi_service as svc


def _payload(widget_count=1):
    return {
        "meta": {
            "db": "remote.db", "host": "http://remote", "count": widget_count,
            "failed": 0, "elapsed_ms": 1.0, "cached": False,
            "stale": False, "refreshing": False, "start": None, "end": None,
        },
        "title": "CCN_Daily_KPI",
        "title_html": "",
        "widgets": [
            {"id": f"w{i}", "title": f"W{i}", "chart": "kpi", "rows": [[i]], "columns": ["v"], "error": ""}
            for i in range(widget_count)
        ],
    }


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "LAST_GOOD_DIR", tmp_path)
    svc._cache.clear()
    svc._last_good.clear()
    svc._inflight.clear()
    svc._last_attempt.clear()
    yield
    svc._cache.clear()
    svc._last_good.clear()
    svc._inflight.clear()
    svc._last_attempt.clear()


def test_cold_returns_computing_then_data(monkeypatch):
    """无旧值：立即返回 computing；后台算完后再请求直接出数。"""
    def fake_compute(key, start_v, end_v):
        data = _payload()
        entry = {"at": time.time(), "data": data}
        with svc._cache_lock:
            svc._cache[key] = entry
            svc._last_good[key] = entry
        return data

    monkeypatch.setattr(svc, "_compute", fake_compute)
    res = svc.get_overview()
    assert res["meta"]["computing"] is True
    assert res["widgets"] == []

    deadline = time.time() + 3
    while time.time() < deadline and not svc._cache:
        time.sleep(0.02)
    res2 = svc.get_overview()
    assert not res2["meta"].get("computing")
    assert len(res2["widgets"]) == 1


def test_stale_returns_immediately_and_refreshes(monkeypatch):
    """有旧值：请求立即返回旧值 + stale/refreshing，不等待后台刷新。"""
    old = {"at": time.time() - 3600, "data": _payload(2)}
    svc._cache["~"] = old
    started = threading.Event()
    release = threading.Event()

    def slow_compute(key, start_v, end_v):
        started.set()
        release.wait(3)
        return old["data"]

    monkeypatch.setattr(svc, "_compute", slow_compute)
    t0 = time.time()
    res = svc.get_overview()
    elapsed = time.time() - t0
    try:
        assert elapsed < 0.5, f"旧值路径不应阻塞（{elapsed:.2f}s）"
        assert res["meta"]["stale"] is True
        assert res["meta"]["refreshing"] is True
        assert res["meta"]["data_at"]
        assert len(res["widgets"]) == 2
        assert started.wait(2), "应触发后台刷新"
    finally:
        release.set()


def test_restart_recovers_from_disk_last_good(monkeypatch):
    """内存清空（进程重启）后能从磁盘文件恢复旧值，而不是回到空壳。"""
    entry = {"at": time.time() - 120, "data": _payload(1)}
    (svc.LAST_GOOD_DIR / "all.json").write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")

    called = []
    monkeypatch.setattr(svc, "_compute", lambda *a, **k: called.append(1))

    res = svc.get_overview()
    assert res["meta"]["stale"] is True
    assert res["meta"]["computing"] in (False, None)
    assert len(res["widgets"]) == 1
    assert called, "应从磁盘恢复后触发后台刷新"


def test_force_does_not_block(monkeypatch):
    """点「刷新」也走后台：立即返回旧值，最多等一次远端计算的其中一小段。"""
    svc._cache["~"] = {"at": time.time() - 10, "data": _payload(1)}
    started = threading.Event()
    release = threading.Event()

    def slow_compute(*a, **k):
        started.set()
        release.wait(3)

    monkeypatch.setattr(svc, "_compute", slow_compute)
    t0 = time.time()
    res = svc.get_overview(force=True)
    try:
        assert time.time() - t0 < 0.5
        assert res["meta"]["stale"] is True
        assert started.wait(2)
    finally:
        release.set()
