#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 OLIB 拓扑 PDF 第 1 页（物理图）的链路数字化成「站点对」边。

**手工维护工具**，不在任何定时任务里跑：上游换 PDF 版时人工跑一次，产物是
`frontend/public/olib/olib-fiber-links.json`（页面 ?page=olibTopo_v2 的「PDF 链路」图层）。

为什么要这一步
--------------
PDF 第 2 页的逻辑图只梳理出 18 条骨干关系（手写 `CORE_LINKS`），而第 1 页物理图
上画满的是**节点到节点**的链路：40G/10G 光缆带、IPRAN/DWDM 线、P26A–D 项目线，
以及大量黑色细线（图例未注明类型的基础链路），实测八百多段。本脚本把这些矢量
线段抽出来，变成「站点下标对」，页面就能按真实 GPS 画出来。

坐标怎么来的（难点）
--------------------
第 1 页是**示意图不是比例地图**：蒙罗维亚被放大约 10 倍、全国被拉扁，站点标签与
节点圆点之间还隔着引线（实测标签中心到圆点中位数 28pt、p90 73pt）。所以：

1. 控制点 = `topologyData.ts` 的 667 个站点坐标里**已定位**的 541 个，目标值 =
   `olib-site-geo.json` 的真实经纬度；
2. 用**薄板样条（TPS）**拟合 PDF 坐标 → 经纬度（仿射不行：残差中位数 43km，
   只取主图也有 14km）；
3. 线段端点经 TPS 落到经纬度后，吸附到 4km 内最近的已定位站点 —— 于是一条
   PDF 线段就变成「哪两个站点之间有这条链路」。吸不上的段丢弃，并计数报告。

口径与精度
----------
这层表达的是**PDF 上画了哪些站到站链路**，不是光缆走向、容量或时延；站点身份
靠 TPS + 最近站吸附推断，留一验证误差中位数约 8km、p90 约 20km，密集区
（蒙罗维亚）可能吸到邻站。图例未注明的黑色细线单列 `base` 组。

用法
----
    python backend/scripts/gen_olib_fiber_links.py "D:\\muse\\Orange network topology_20260623.pdf" --preview
    python backend/scripts/gen_olib_fiber_links.py "<pdf>" --write     # 写产物 + 打印摘要

依赖：PyMuPDF、numpy、matplotlib（仅 --preview）。**不要**加进 requirements.txt，
它们不是运行时依赖。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from pathlib import Path

import numpy as np
import pymupdf

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
TOPO_DATA = FRONTEND / "src" / "pages" / "networkTopology" / "topologyData.ts"
GEO_JSON = FRONTEND / "public" / "olib" / "olib-site-geo.json"
OUT_JSON = FRONTEND / "public" / "olib" / "olib-fiber-links.json"

# 图例区：左下角，颜色/线型说明，不能当链路抽
LEGEND = {"x1": 480.0, "y0": 3650.0}

# 粗光缆 = 「填充色 + 描边」的带宽条；项目/服务线 = 细描边；黑色细线 = 基础链路
BAND_FILL = {
    (0.949, 0.51, 0.149): ("f40", "40G"),
    (0.851, 0.408, 0.051): ("f40", "40G"),
    (1.0, 1.0, 0.0): ("f10", "10G"),
}
LINE_STROKE = {
    (0.294, 0.675, 0.776): ("ipran", "IPRAN"),
    (0.0, 0.439, 0.753): ("ipran", "DWDM"),
    (0.961, 0.616, 0.337): ("p26", "P26A"),
    (0.0, 0.69, 0.941): ("p26", "P26B"),
    (1.0, 0.0, 0.0): ("p26", "P26C"),
    (0.753, 0.0, 0.0): ("p26", "P26C"),
    (0.867, 0.075, 0.788): ("p26", "P26D"),
    (0.918, 0.439, 0.051): ("plan", "在建"),
}
BASE_STROKE = (0.0, 0.0, 0.0)

SNAP_M = 4000.0         # 端点吸附半径（TPS 局部误差 + 标签偏移量级）
DOT_TOL_PT = 14.0       # 黑色细线两端离节点圆点多近才算「链路」而不是「标签引线」
M_PER_DEG_LAT = 111320.0


def load_sites() -> list[dict]:
    src = TOPO_DATA.read_text(encoding="utf-8")
    rows = re.findall(
        r'^\s*\["((?:[^"\\]|\\.)*)",\s*([\d.]+),\s*([\d.]+),\s*(true|false),\s*(true|false),\s*"([^"]*)"\],?\s*$',
        src, re.M)
    return [dict(name=n.replace('\\"', '"'), x=float(x), y=float(y)) for n, x, y, *_ in rows]


def tps_fit(pdf_pts: np.ndarray, latlng: np.ndarray, lam_ratio: float = 1e-4):
    """薄板样条：PDF 坐标（pt）→ (lat, lng)。返回 warp(pts)。"""
    centre = pdf_pts.mean(axis=0)
    scale = pdf_pts.std() or 1.0
    p = (pdf_pts - centre) / scale

    def kernel(r):
        r = np.where(r > 0, r, 1e-12)
        return r * r * np.log(r)

    k = kernel(np.linalg.norm(p[:, None, :] - p[None, :, :], axis=2))
    n = len(p)
    a = np.zeros((n + 3, n + 3))
    a[:n, :n] = k + lam_ratio * np.mean(np.diag(k)) * np.eye(n)
    a[:n, n:] = np.hstack([np.ones((n, 1)), p])
    a[n:, :n] = a[:n, n:].T
    rhs = np.zeros((n + 3, 2))
    rhs[:n] = latlng
    sol = np.linalg.solve(a, rhs)

    def warp(pts):
        q = (np.asarray(pts, dtype=float) - centre) / scale
        kk = kernel(np.linalg.norm(q[:, None, :] - p[None, :, :], axis=2))
        return np.hstack([np.ones((len(q), 1)), q]) @ sol[n:] + kk @ sol[:n]

    return warp


def find_dots(page) -> list[tuple[float, float]]:
    dots = []
    for d in page.get_drawings():
        fill = d.get("fill")
        r = d["rect"]
        if fill and tuple(round(v, 3) for v in fill) == BASE_STROKE and 1 < max(r.width, r.height) < 20:
            dots.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
    return dots


def dist_to_dots(pt, dots) -> float:
    return min((math.hypot(pt[0] - x, pt[1] - y) for x, y in dots), default=1e9)


def extract_segments(page, dots) -> tuple[list[dict], dict]:
    """抽链路折线。返回 ([{group, type, dash, pts}], 统计)。"""
    out = []
    stats = {"colored": 0, "black": 0, "blackLeader": 0, "tiny": 0}
    for d in page.get_drawings():
        r = d["rect"]
        if r.x1 < LEGEND["x1"] and r.y0 > LEGEND["y0"]:
            continue
        if max(r.width, r.height) < 20:      # 图标、箭头、站点圆点、图例色块
            stats["tiny"] += 1
            continue
        fill = tuple(round(v, 3) for v in d["fill"]) if d.get("fill") else None
        stroke = tuple(round(v, 3) for v in d["color"]) if d.get("color") else None
        dashed = bool(d.get("dashes") and d["dashes"] != "[] 0")

        if fill in BAND_FILL:
            group, raw = BAND_FILL[fill]
        elif stroke in LINE_STROKE and not fill:
            group, raw = LINE_STROKE[stroke]
        elif stroke == BASE_STROKE and not fill:
            group, raw = "base", "基础链路"
        else:
            continue

        lines = [it for it in d["items"] if it[0] == "l"]
        pts: list[tuple[float, float]] = []
        if group in ("f40", "f10") and len(lines) == 2:
            # 带宽条：两条长边平行反向，按最近端点配对后取中线
            (a1, a2), (b1, b2) = lines[0][1:3], lines[1][1:3]
            straight = math.hypot(a1.x - b1.x, a1.y - b1.y) + math.hypot(a2.x - b2.x, a2.y - b2.y)
            crossed = math.hypot(a1.x - b2.x, a1.y - b2.y) + math.hypot(a2.x - b1.x, a2.y - b1.y)
            if crossed < straight:
                b1, b2 = b2, b1
            pts = [((a1.x + b1.x) / 2, (a1.y + b1.y) / 2), ((a2.x + b2.x) / 2, (a2.y + b2.y) / 2)]
        else:
            for it in d["items"]:
                if it[0] == "l":
                    pts += [(it[1].x, it[1].y), (it[2].x, it[2].y)]
                elif it[0] == "c":
                    pts += [(it[1].x, it[1].y), (it[4].x, it[4].y)]
        if len(pts) < 2:
            continue
        if group == "base":
            # 黑色细线里混着「标签→圆点」的引线：两端都要贴着圆点才算链路
            if dist_to_dots(pts[0], dots) > DOT_TOL_PT or dist_to_dots(pts[-1], dots) > DOT_TOL_PT:
                stats["blackLeader"] += 1
                continue
            stats["black"] += 1
        else:
            stats["colored"] += 1
        if group == "f10" and dashed:
            raw = "10G 规划"
        if group == "plan":
            dashed = True
        out.append({"group": group, "type": raw, "dash": dashed, "pts": pts})
    return out, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="OLIB PDF 第 1 页链路数字化（站点对）")
    ap.add_argument("pdf", help="Orange network topology PDF 路径")
    ap.add_argument("--page", type=int, default=1, help="页码（1 起，默认第 1 页）")
    ap.add_argument("--write", action="store_true", help="写 frontend/public/olib/olib-fiber-links.json")
    ap.add_argument("--preview", action="store_true", help="另存一张 PNG 预览（调试用）")
    args = ap.parse_args()

    page = pymupdf.open(args.pdf)[args.page - 1]
    width, height = page.rect.width, page.rect.height
    sites = load_sites()
    records = json.loads(GEO_JSON.read_text(encoding="utf-8"))["records"]

    located = [i for i, r in enumerate(records)
               if isinstance(r.get("lat"), (int, float)) and r.get("method") in ("name-exact", "alias")]
    pdf_pts, latlng, seen = [], [], set()
    for i in located:
        s = sites[i]
        key = (round(s["x"] / 2), round(s["y"] / 2))
        if key in seen:
            continue
        seen.add(key)
        pdf_pts.append((s["x"] / 1000 * width, s["y"] / 1000 * height))
        latlng.append((records[i]["lat"], records[i]["lng"]))
    warp = tps_fit(np.array(pdf_pts), np.array(latlng))

    anchors = np.array(latlng)
    cos_lat = math.cos(math.radians(6.5))

    def nearest_site(lat: float, lng: float):
        dd = np.hypot((anchors[:, 0] - lat) * M_PER_DEG_LAT,
                      (anchors[:, 1] - lng) * M_PER_DEG_LAT * cos_lat)
        j = int(dd.argmin())
        return located[j], float(dd[j])

    dots = find_dots(page)
    segs, stats = extract_segments(page, dots)

    edges: dict[tuple[int, int, str], dict] = {}
    dropped = {"unsnapped": 0, "degenerate": 0}
    snap_hits = 0
    snap_n = 0

    for seg in segs:
        w = warp(seg["pts"])
        ids = []
        for lat, lng in w:
            idx, dist = nearest_site(float(lat), float(lng))
            snap_n += 1
            if dist <= SNAP_M:
                snap_hits += 1
                ids.append(idx)
            else:
                ids.append(None)
        # 折线逐点吸附后按相邻站点对切成若干条边
        chain = [i for i in ids if i is not None]
        if len(chain) < 2:
            dropped["unsnapped"] += 1
            continue
        for a, b in zip(chain, chain[1:]):
            if a == b:
                continue
            key = (min(a, b), max(a, b), seg["group"])
            cur = edges.get(key)
            if cur is None:
                edges[key] = {"a": min(a, b), "b": max(a, b), "c": seg["group"],
                              "t": seg["type"], "d": 1 if seg["dash"] else 0}
            elif seg["dash"]:
                cur["d"] = 1

    by_group: dict[str, int] = {}
    for (_, _, g) in edges:
        by_group[g] = by_group.get(g, 0) + 1
    linked_sites = {i for (a, b, _) in edges for i in (a, b)}
    site_names = {str(i): sites[i]["name"] for i in sorted(linked_sites)}

    payload = {
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": Path(args.pdf).name,
        "page": args.page,
        "pageSize": [width, height],
        "method": "thin-plate-spline+nearest-site",
        "anchors": len(pdf_pts),
        "snapRadiusM": SNAP_M,
        "summary": {
            "segments": len(segs),
            "edges": len(edges),
            "byGroup": by_group,
            "linkedSites": len(linked_sites),
            "dropped": dropped,
            "extract": stats,
            "snappedEndpoints": snap_hits,
            "endpoints": snap_n,
        },
        "sites": site_names,
        "edges": sorted(edges.values(), key=lambda e: (e["c"], e["a"], e["b"])),
    }

    print(f"PDF 页尺寸 {width}x{height}；控制点 {len(pdf_pts)}；抽出彩色 {stats['colored']} 段 / 黑色链路 {stats['black']} 段"
          f"（另有 {stats['blackLeader']} 条黑色标签引线未计入）")
    print(f"站点对边 {len(edges)} 条，涉及 {len(linked_sites)} 个已定位站点；分组 {by_group}")
    print(f"端点吸附 {snap_hits}/{snap_n}；丢弃 {dropped}")

    if args.preview:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        colors = {"f40": "#ea580c", "f10": "#ca8a04", "ipran": "#0891b2",
                  "p26": "#a21caf", "plan": "#f97316", "base": "#94a3b8"}
        fig, ax = plt.subplots(figsize=(12, 9), dpi=110)
        for e in edges.values():
            a, b = records[e["a"]], records[e["b"]]
            ax.plot([a["lng"], b["lng"]], [a["lat"], b["lat"]], color=colors.get(e["c"], "#666"),
                    lw=0.9, alpha=0.75, ls="--" if e["d"] else "-", zorder=1 if e["c"] == "base" else 2)
        ax.plot(anchors[:, 1], anchors[:, 0], ".", ms=2, color="#334155", zorder=3)
        ax.set_aspect("equal")
        ax.set_title(f"{len(edges)} site-pair edges / {len(linked_sites)} sites")
        # 预览图是临时产物：放 TMP/，不进 public（避免随构建发布）
        out_dir = FRONTEND.parent / "TMP"
        out_dir.mkdir(exist_ok=True)
        png = out_dir / "olib-fiber-links.preview.png"
        fig.savefig(png, bbox_inches="tight")
        print("preview:", png)

    if args.write:
        OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print("written:", OUT_JSON, OUT_JSON.stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
