# -*- coding: utf-8 -*-
"""CXO 看板缓存路径守卫（2026-11-18 线上事故回归）。

背景：§11-4 把 `cxo_dashboard_cache.py` / `vmax_cxo_daily_cache.py` 移进
`backend/services/cxo/` 子包时，模块里的 `Path(__file__).parent.parent.parent`
只退到 `backend/`，缓存被写到 `backend/data/cxo_dashboard/`；而原版看板的
router（`backend/routers/vmax_cxo_dashboard.py`）仍按 `parents[2]` 读
`my_app/data/cxo_dashboard/`。结果：任务每 10 分钟照常抓数、页面一直读旧文件，
2026-11-18 切到 02 后页面停在 11-16 08:41（任务侧已写到 22:01）。

本测试锁住三件事：
1. 读写两侧共用同一个常量（router 必须 import 服务的 CACHE_FILE，不能自己再算）；
2. 路径必须落在 `my_app/data/`，不能是 `my_app/backend/data/`；
3. 日存 `DAILY_FILE` 同样回到 `my_app/data/`（否则第一次运行会以空 store + 最近
   8 天重建，超过 8 天的归档永久不可回补）。
"""
from backend.routers.vmax_cxo_dashboard import CACHE_FILE as router_cache
from backend.services.cxo.cxo_dashboard_cache import CACHE_FILE as writer_cache
from backend.services.cxo.vmax_cxo_daily_cache import DAILY_FILE


def test_router_and_writer_share_one_cache_path():
    assert router_cache == writer_cache, "原版看板的读路径必须与写路径是同一个文件"


def test_cxo_cache_is_under_app_data_not_backend_data():
    # .../my_app/data/cxo_dashboard/last_known_good.json
    assert writer_cache.name == "last_known_good.json"
    assert writer_cache.parent.name == "cxo_dashboard"
    assert writer_cache.parent.parent.name == "data"
    assert writer_cache.parent.parent.parent.name == "my_app"
    assert "backend" not in writer_cache.parts, "缓存不能写进 my_app/backend/data"


def test_daily_archive_stays_under_app_data():
    # .../my_app/data/vmax_cxo_daily/daily.json
    assert DAILY_FILE.name == "daily.json"
    assert DAILY_FILE.parent.name == "vmax_cxo_daily"
    assert DAILY_FILE.parent.parent.name == "data"
    assert DAILY_FILE.parent.parent.parent.name == "my_app"
    assert "backend" not in DAILY_FILE.parts, "日存必须继续写 data/vmax_cxo_daily 的既有归档"
