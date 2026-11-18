# -*- coding: utf-8 -*-
"""RAN 资产台账 API。

- `GET /api/ran-inventory/ledger` —— 汇总 + 882 行在线网元台账（进程内缓存 600s）
- `GET /api/ran-inventory/ne/{ne_id}` —— 单网元的硬件 / 光模块 / 软件包明细

取数链路见 `backend/services/ran_inventory/ran_inventory_service.py`。
上游不可用时返回 `{"error": ...}`，由前端渲染告警条，**不伪造空台账** ——
把「取不到」显示成 0 会让用户以为全网没有资产。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from backend.services.ran_inventory import ran_inventory_service as svc

logger = logging.getLogger("ran_inventory")

router = APIRouter(prefix="/api/ran-inventory", tags=["ran-inventory"])


def _error_payload(exc: Exception) -> dict:
    logger.exception("RAN 资产台账接口失败: %s", exc)
    return {
        "error": str(exc),
        "meta": {"warnings": [str(exc)]},
        "counts": {},
        "unit_totals": [],
        "by_type": [],
        "years": [],
        "opt_vendors": [],
        "pack_models": [],
        "rru_models": [],
        "ant_models": [],
        "ne": [],
    }


@router.get("/ledger")
def ledger(refresh: bool = Query(False, description="true = 跳过缓存，重新从 RAN UME 取数")):
    try:
        return svc.ledger_payload(force=refresh)
    except Exception as exc:  # noqa: BLE001
        return _error_payload(exc)


@router.get("/ne/{ne_id}")
def ne_detail(ne_id: str):
    try:
        return svc.ne_detail(ne_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("RAN 资产台账明细接口失败: %s", exc)
        return {"error": str(exc), "ne_id": ne_id, "hw": [], "opt": [], "sw": []}


@router.get("/cache")
def cache():
    return svc.cache_info()
