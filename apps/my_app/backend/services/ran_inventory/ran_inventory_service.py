# -*- coding: utf-8 -*-
"""RAN 资产台账：聚合、缓存与对外入口。

## 口径（页面「数据口径说明」逐条对应）

1. **只统计在线网元**：网元表 `connectionState = 1` 才进台账；离线网元只作为
   「在线网元」卡的分母，其名下资产不计入任何数字。
2. **关联键**：资产表 `neuuid` ↔ 网元表 `id`（实测 100% 命中），缺 `neuuid` 用
   `neid` ↔ `nbiId` 兜底；两者都命不中的资产行不进台账。
3. **硬件分组**：按 `inventoryunittype` —— pack/RRU/antenna/subrack/shelf/rack/accessory。
4. **机龄**：该网元最早一台设备的 `dateofmanufacture` 至今（365.25 天/年）；
   投产取最早 `networkaccesstime`。均为「一台设备」口径，不是整站上线日期。
5. **软件包**：按 `pkgtype` 计数；补丁 = `patchPackage` + `hotpatchPackage`。
6. `inventorystatus` / `swstatus` / `maintainStatus` 上游未给字典，原样保留、不推断。

取数在 `ran_inventory_client.py`，这里只做纯计算与进程内缓存。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

from backend.services.ran_inventory import ran_inventory_client as client
from backend.services.ran_inventory.ran_inventory_client import (
    INV_KINDS, HOST, PORT,
)

logger = logging.getLogger("ran_inventory_service")

# 台账是慢变量：资产表每天约 04:00 由网管刷新一次，10 分钟缓存足够
CACHE_TTL = max(0, int(os.environ.get("RAN_INV_CACHE_TTL", "600")))

# hw 的 inventoryunittype → 台账分组（key 与前端 i18n 一一对应）
UNIT_GROUPS = (
    ("pack", "板卡"),
    ("RRU", "射频单元"),
    ("antenna", "天馈"),
    ("subrack", "机箱"),
    ("shelf", "机架层"),
    ("rack", "机柜"),
    ("accessory", "配件"),
)

# 型号取哪个字段（页面「型号透视」与型号统计列都用它）：
#   · pack / RRU 取 `packfunction` —— 就是 VBP / VFC / R9242E 这类可采购的板卡与射频型号；
#     `vendorunittypename` 是它下面的细变体（VBPe1p / R9242E M8090(ACA)），61 种太碎，
#     Top10 只覆盖 72.6%，不适合做台账列。
#   · antenna 反过来：`packfunction` 全是 RETC（电调控制器类，1 种，没有信息量），
#     天线型号只能取 `vendorunittypename`（AIMM20M11S0v03 等，14 种，Top8 覆盖 98.6%）。
MODEL_CATEGORIES = ("pack", "RRU", "antenna")


def model_of(unit_type: str, row: dict) -> str:
    """一条硬件记录的型号名。空值统一成「(未标注)」，避免页面上出现空白列。"""
    if unit_type == "antenna":
        return _s(row.get("vendorunittypename")) or "(未标注)"
    return (_s(row.get("packfunction")) or _s(row.get("vendorunittypename"))
            or "(未标注)")


# ---------------------------------------------------------------- 工具
def _s(v):
    return "" if v is None else str(v)


def _dt(v):
    """'2023-03-14 16:17:56' / '2026-07-14T00:11:14+00:00' → 'YYYY-MM-DD'。"""
    s = _s(v)
    return s[:10] if len(s) >= 10 else ""


def _years_from(day: str, today: date) -> float | None:
    if not day:
        return None
    try:
        return round((today - date.fromisoformat(day)).days / 365.25, 1)
    except ValueError:
        return None


# ---------------------------------------------------------------- 聚合
def _aggregate(hw: list, sw: list, opt: list, ne: list,
               today: date | None = None) -> tuple[dict, dict]:
    """把四张原始表聚合成 (台账 payload, 逐网元明细)。只保留在线网元。"""
    today = today or date.today()
    ne_by_uuid = {r.get("id"): r for r in ne if r.get("id")}
    ne_by_nbi = {_s(r.get("nbiId")): r for r in ne}

    online = [r for r in ne if _s(r.get("connectionState")) == "1"]
    online_uuids = {r["id"] for r in online}

    def ne_key(row: dict):
        """资产行归属的网元 uuid：neuuid 优先，neid↔nbiId 兜底，取不到返回 None。"""
        u = row.get("neuuid")
        if u in ne_by_uuid:
            return u
        n = ne_by_nbi.get(_s(row.get("neid")))
        return n["id"] if n else None

    def only_online(rows: list) -> list:
        out = []
        for r in rows:
            k = ne_key(r)
            if k and k in online_uuids:
                out.append(r)
        return out

    hw_on, sw_on, opt_on = only_online(hw), only_online(sw), only_online(opt)

    units_by_ne: dict[str, list] = defaultdict(list)
    for r in hw_on:
        units_by_ne[ne_key(r)].append(r)
    opt_by_ne: dict[str, list] = defaultdict(list)
    for r in opt_on:
        opt_by_ne[ne_key(r)].append(r)
    sw_by_ne: dict[str, list] = defaultdict(list)
    for r in sw_on:
        sw_by_ne[ne_key(r)].append(r)

    # 型号字典：全局按数量降序编号。逐网元只发 [型号下标, 数量] 对，
    # 而不是 {型号名: 数量} —— 882 个网元 × 三类的键名重复，体积差约 3 倍。
    model_counter = {g: Counter() for g in MODEL_CATEGORIES}
    for u in hw_on:
        t = _s(u.get("inventoryunittype"))
        if t in model_counter:
            model_counter[t][model_of(t, u)] += 1
    model_index = {g: {name: i for i, (name, _) in enumerate(c.most_common())}
                   for g, c in model_counter.items()}
    model_defs = {g: [{"name": name, "n": n} for name, n in c.most_common()]
                  for g, c in model_counter.items()}

    ledger = []
    for n in online:
        uid = n["id"]
        units = units_by_ne.get(uid, [])
        cnt = Counter(_s(u.get("inventoryunittype")) for u in units)
        opts = opt_by_ne.get(uid, [])
        pkgs = sw_by_ne.get(uid, [])
        # 按型号计数（只对 pack / RRU / antenna 三类有意义的型号）
        mc: dict[str, list] = {}
        for g in MODEL_CATEGORIES:
            tally = Counter(model_of(g, u) for u in units
                            if _s(u.get("inventoryunittype")) == g)
            mc[g] = [[model_index[g][name], n] for name, n in tally.most_common()]
        launch = min((_dt(u.get("networkaccesstime")) for u in units
                      if _dt(u.get("networkaccesstime"))), default="")
        oldest = min((_dt(u.get("dateofmanufacture")) for u in units
                      if _dt(u.get("dateofmanufacture"))), default="")
        ledger.append({
            "id": uid,
            "site": _s(n.get("name")) or _s(n.get("displayName")),
            "display": _s(n.get("displayName")),
            "nbi": _s(n.get("nbiId")),
            "moc": _s(n.get("moc")),
            "sub": _s(n.get("subType")),
            "ip": _s(n.get("ipAddress")),
            "proto": _s(n.get("protocol")),
            "ver": _s(n.get("version")),
            "maint": _s(n.get("maintainStatus")),
            "hw": len(units),
            "u": {g: cnt.get(g, 0) for g, _ in UNIT_GROUPS},
            "mc": mc,
            "opt": len(opts),
            "sw": len(pkgs),
            "patch": sum(1 for p in pkgs
                         if _s(p.get("pkgtype")) in ("patchPackage", "hotpatchPackage")),
            "launch": launch,
            "oldest": oldest,
            "age": _years_from(oldest, today),
            "hw_bad": sum(1 for u in units if _s(u.get("inventorystatus")) == "2"),
            "opt_bad": sum(1 for o in opts if _s(o.get("inventorystatus")) == "2"),
        })

    unit_totals = Counter()
    for row in ledger:
        for g, _ in UNIT_GROUPS:
            unit_totals[g] += row["u"][g]

    hw_by_type = Counter()
    for r in ledger:
        hw_by_type[r["moc"]] += r["hw"]
    moc_counter = Counter(r["moc"] for r in ledger)
    by_type = [{
        "moc": moc, "ne": n, "hw": hw_by_type[moc],
        "opt": sum(r["opt"] for r in ledger if r["moc"] == moc),
    } for moc, n in moc_counter.most_common()]

    year_hist = Counter()
    for r in hw_on:
        y = _dt(r.get("dateofmanufacture"))[:4]
        if y:
            year_hist[y] += 1

    vendor = Counter(_s(o.get("vendorname")) or "(未标注)" for o in opt_on)
    vendors = [{"name": k, "n": v} for k, v in vendor.most_common(7)]
    if len(vendor) > 7:
        vendors.append({"name": "其他", "n": sum(list(vendor.values())[7:])})

    pack_models = Counter(_s(u.get("packfunction")) for u in hw_on
                          if _s(u.get("inventoryunittype")) == "pack")
    rru_models = Counter(_s(u.get("packfunction")) for u in hw_on
                         if _s(u.get("inventoryunittype")) == "RRU")
    ant_models = Counter(_s(u.get("vendorunittypename")) for u in hw_on
                         if _s(u.get("inventoryunittype")) == "antenna")

    ages = [r["age"] for r in ledger if r["age"] is not None]
    counts = {
        "ne_total": len(ne),
        "ne_online": len(online),
        "ne_offline": len(ne) - len(online),
        "ne_sites": len({r["site"] for r in ledger}),
        "hw_total": len(hw),
        "hw_online": len(hw_on),
        "sw_total": len(sw),
        "sw_online": len(sw_on),
        "opt_total": len(opt),
        "opt_online": len(opt_on),
        "hw_bad": sum(r["hw_bad"] for r in ledger),
        "opt_bad": sum(r["opt_bad"] for r in ledger),
        "patch_total": sum(r["patch"] for r in ledger),
        "patch_ne": sum(1 for r in ledger if r["patch"] > 0),
        "avg_age": round(sum(ages) / len(ages), 1) if ages else None,
        "old_ne": sum(1 for r in ledger if r["age"] is not None and r["age"] >= 8),
    }

    times = [_s(r.get("updatetime")) for r in hw_on if r.get("updatetime")]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot": max(times) if times else "",
        "source": {"host": f"{HOST}:{PORT}", "kinds": list(INV_KINDS)},
        "counts": counts,
        "unit_totals": [{"key": g, "label": label, "n": unit_totals[g]}
                        for g, label in UNIT_GROUPS],
        "model_defs": model_defs,
        "by_type": by_type,
        "years": [{"year": y, "n": year_hist[y]} for y in sorted(year_hist)],
        "opt_vendors": vendors,
        "pack_models": [{"name": k, "n": v} for k, v in pack_models.most_common(10)],
        "rru_models": [{"name": k, "n": v} for k, v in rru_models.most_common(8)],
        "ant_models": [{"name": k, "n": v} for k, v in ant_models.most_common(6)],
        "ne": ledger,
    }

    detail = {"hw": {}, "opt": {}, "sw": {}}
    for r in hw_on:
        detail["hw"].setdefault(ne_key(r), []).append({
            "t": _s(r.get("inventoryunittype")),
            "m": _s(r.get("packfunction")),
            "mt": _s(r.get("vendorunittypename")),
            "sn": _s(r.get("serialnumber")),
            "p": _s(r.get("unitposition")),
            "d": _dt(r.get("dateofmanufacture")),
            "st": _s(r.get("inventorystatus")),
        })
    for r in opt_on:
        detail["opt"].setdefault(ne_key(r), []).append({
            "v": _s(r.get("vendorname")),
            "pn": _s(r.get("vendorpn")),
            "sn": _s(r.get("vendorsn")),
            "p": _s(r.get("unitposition")),
            "d": _dt(r.get("datecode")),
            "st": _s(r.get("inventorystatus")),
        })
    for r in sw_on:
        detail["sw"].setdefault(ne_key(r), []).append({
            "t": _s(r.get("pkgtype")),
            "v": _s(r.get("pkgver")),
            "m": _s(r.get("mainpkgver")),
            "st": _s(r.get("swstatus")),
            "a": _dt(r.get("timeofactivation")),
        })
    return payload, detail


# ---------------------------------------------------------------- 取数入口
def collect() -> tuple[dict, dict]:
    """取一次全量并聚合。异常向上抛，由路由层转成 error payload。"""
    sess = client.Session().acquire()
    try:
        inv = {k: client.fetch_inventory(sess, k) for k in INV_KINDS}
        ne = client.fetch_network_elements(sess)
    except client.AuthExpired:
        logger.warning("[RAN-INV] 取数遇到 401/403，强制重登后重试一次")
        sess = client.Session().acquire(force_refresh=True)
        inv = {k: client.fetch_inventory(sess, k) for k in INV_KINDS}
        ne = client.fetch_network_elements(sess)

    for k in INV_KINDS:
        rows, total = inv[k]
        if total and len(rows) < total:
            raise RuntimeError(
                f"{k} 资产只有 {len(rows)} 行，低于上游声明总数 {total}，"
                "判定为分页残片，拒绝生成台账"
            )
        if len(rows) < 100:
            raise RuntimeError(
                f"{k} 资产只有 {len(rows)} 行，低于安全阈值 100，拒绝生成台账")

    payload, detail = _aggregate(inv["hw"][0], inv["sw"][0], inv["opt"][0], ne)
    payload["auth_source"] = sess.source
    return payload, detail


# ---------------------------------------------------------------- 进程内缓存
_CACHE: dict = {"at": 0.0, "payload": None, "detail": None}
_LOCK = threading.Lock()


def ledger_payload(force: bool = False) -> dict:
    """台账汇总（含在线网元清单）。命中缓存则不打上游。"""
    now = time.time()
    with _LOCK:
        cached = _CACHE["payload"]
        fresh = cached is not None and (now - _CACHE["at"]) < CACHE_TTL and not force
        if fresh:
            out = dict(cached)
            out["from_cache"] = True
            out["cache_age_sec"] = int(now - _CACHE["at"])
            return out
    payload, detail = collect()
    with _LOCK:
        _CACHE.update({"at": time.time(), "payload": payload, "detail": detail})
    out = dict(payload)
    out["from_cache"] = False
    out["cache_age_sec"] = 0
    return out


def ne_detail(ne_id: str) -> dict:
    """单网元的硬件/光模块/软件包明细，从缓存里的明细取，不二次抓上游。"""
    with _LOCK:
        detail = _CACHE["detail"]
        fresh = detail is not None and (time.time() - _CACHE["at"]) < CACHE_TTL
    if not fresh:
        ledger_payload()  # 触发一次取数，填满缓存
        with _LOCK:
            detail = _CACHE["detail"]
    if not detail:
        return {"ne_id": ne_id, "hw": [], "opt": [], "sw": []}
    return {
        "ne_id": ne_id,
        "hw": detail["hw"].get(ne_id, []),
        "opt": detail["opt"].get(ne_id, []),
        "sw": detail["sw"].get(ne_id, []),
    }


def cache_info() -> dict:
    with _LOCK:
        return {
            "cached": _CACHE["payload"] is not None,
            "age_sec": int(time.time() - _CACHE["at"]) if _CACHE["at"] else None,
            "ttl_sec": CACHE_TTL,
        }
