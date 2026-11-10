# -*- coding: utf-8 -*-
"""IP RAN 光纤拓扑 API（拓扑图数据一次返回，前端只画不算）。

端点
----
- `GET /api/ipran-topo/graph?refresh=0`  完整图数据（网元 + 折叠后的边 + 元信息）

`refresh=1` 同时绕过图缓存与站点坐标缓存（页面「刷新」按钮用），
对应 UME 侧 8 次调用，约 1.5~3.0s；不加则命中 120s 进程缓存。
取数口径、折叠规则、坐标来源与告警饱和度的说明见
`backend/services/ipran_topo/ipran_topo_service.py` 模块 docstring。
"""
import logging

from fastapi import APIRouter, Query

from backend.services.ipran_topo import ipran_topo_service as svc

logger = logging.getLogger("ipran_topo_api")
router = APIRouter(prefix="/api/ipran-topo", tags=["ipran-topo"])


@router.get("/graph")
def graph(refresh: int = Query(0, ge=0, le=1, description="1 = 绕过缓存重新取数")):
    try:
        if refresh:
            svc.clear_cache()
            svc.refresh_site_index()
        return svc.build_graph(force=bool(refresh))
    except Exception as exc:
        logger.exception("[ipran-topo/graph] failed: %s", exc)
        return {
            "nodes": [],
            "edges": [],
            "meta": {
                "ne": 0, "ext": 0, "rawLinks": 0, "foldedLinks": 0,
                "tierCounts": {}, "geo": {}, "geoMissSites": [], "geoFuzzyPairs": [],
                "critNe": 0, "cached": False,
                "warnings": [f"load_failed: {exc}"],
            },
        }
