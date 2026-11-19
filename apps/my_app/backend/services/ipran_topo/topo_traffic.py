# -*- coding: utf-8 -*-
r"""拓扑链路流量利用率（Weathermap 口径）：把 iPRAN KPI 端口速率贴到拓扑边上。

为什么单独成文件
----------------
拓扑取数（UME 8 次调用）与端口性能取数（`ipran_kpi_sync` 写库的 15 分钟表）是
**两条独立的链路**，谁也不知道对方存在。本模块只做「把两边对齐」这一件事：
按 `(网元名, 端口名)` 把 KPI 表里最新一个采样时刻的收发速率换算成利用率，填进
`ipran_topo_service._aggregate()` 组装出来的 `edges`。

数据源（只读）
--------------
`data/ipran_ume/ipran_ume_kpi.sqlite3`（便携/回退）或本机 PG schema `ipran_ume`
（生产），表名取 `ipran_ume_kpi_service.IPRAN_KPI_QUERY_CONFIGS[*].table_name`
——**不在这里另抄一份表清单**，采集侧改表名时这里自动跟着走。

同一批数据在库里实测有**两种表结构**（2026-11-19 查生产 PG 确认），两种都要认：

| 结构 | 判定 | 取数 |
| --- | --- | --- |
| 宽表 | 有 `Receive Traffic Rate(Mbps)` / `Send Traffic Rate(Mbps)` 两列 | 直接取两列 |
| EAV | 有 `Counter` / `Counter Value` 两列 | `Counter` 字段存的就是上面两个指标名，按名字筛 |

只取**每张表最新的一个 `Begin Time`**：`MAX("Begin Time")` 走采集侧建的唯一索引
（首列就是 `Begin Time`），实测 6 张表合计 ~0.5s，不扫 600 万行历史。

口径（与 Weathermap 语义对齐，改之前先读）
----------------------------------------
1. **链路名前缀确定方向**：UME 链路名形如 `A设备-端口_B设备-端口`，设备名自己带
   `_` 和 `-`，所以**不能按分隔符切**；必须拿这条边两端的网元名去卡
   `startswith(名 + "-")` 与 `_名 + "-"`（见 `parse_link_ports`）。
2. **源侧发送 = 源→宿**：`txUtil` 取源设备该端口的 `Send Traffic Rate`，
   `rxUtil` 取同一端口的 `Receive Traffic Rate`。源侧没采到时退到宿侧并**对调**
   （宿侧收到的正是源侧发出的），不是把方向猜成一样。
3. **利用率 = Mbps ÷ (端口速率 × 1000) × 100**，保留两位小数。端口速率来自链路名
   （`layers.speed_gb`，`10GE/25GE/100GE`），解析不出（如 `ETH:2`、`[0-0-2-0]`）
   就**不给数字**，界面显示 N/A —— 不拿 `gb=None` 去凑一个 0。
4. **折叠边取组内最大**：一条边折叠了 n 条物理链路时，`rxUtil`/`txUtil` 取组内
   **最大**利用率 —— 与「带宽取最小端口速率」同一取向：让最先拥塞的那条浮现出来。
   每条物理链路的明细在 `utilPorts` 里。
5. `-SubPort:N` 行一律跳过：那是端口的子通道聚合行，和端口行同时存在，一起算会重复。

已知限制（界面必须如实体现，不要静默）
------------------------------------
- KPI 采集只覆盖 `ipran_kpi_config.json` 里配置的那几个网元（实测 13 个），
  拓扑有 93 个层内网元 —— 所以大部分边天然没有流量数据，返回 `traffic_partial` 说明。
- `Measure Object` 与拓扑端口名是**字符串精确匹配**（大小写不敏感）。型号或板卡
  命名有差异时会 miss，`traffic_partial` 的数字会跟着变，不猜近似名。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.database import get_raw_connection
from backend.dialect_helpers import table_columns
from backend.services.ipran.ipran_ume_kpi_service import IPRAN_KPI_QUERY_CONFIGS
from backend.services.ipran_topo import layers

logger = logging.getLogger("ipran_topo")

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "ipran_ume" / "ipran_ume_kpi.sqlite3"

# 两个计数器的名字在宽表列名与 EAV 的 Counter 取值里**完全一致**，所以只留一份常量
RX_COUNTER = "Receive Traffic Rate(Mbps)"
TX_COUNTER = "Send Traffic Rate(Mbps)"

# 子通道聚合行：与端口行同时存在，一起用会重复计数
_SUBPORT_RE = re.compile(r"-SubPort:\d+$", re.I)

# 每条边最多带多少条物理链路的利用率明细（与 ipran_topo_service 的 ports[:12] 对齐）
MAX_UTIL_PORTS = 12


# ----------------------------------------------------------------------------
# 纯函数（不碰数据库，便于单测）
# ----------------------------------------------------------------------------
def parse_link_ports(name: str, left: str, right: str):
    r"""`A设备-端口_B设备-端口` → ((A设备, 端口), (B设备, 端口))；对不上返回 None。

    不能按 `_` 或 `-` 切分：设备名本身就是 `M6K_2S16_Logan_Town_UPE100` 这种，
    板卡名里也有 `-`（`P90E-4XGET-SFP+[0-0-0-1]-10GE:2`）。所以拿这条边已知的
    两个网元名去卡首尾：`名-` 开头、`_名-` 结尾，中间留给端口。大小写不敏感，
    返回的网元名以链路名里的写法为准（KPI 侧同样是 UME 的 displayName）。
    """
    if not name or not left or not right:
        return None
    low = name.lower()
    for a, b in ((left, right), (right, left)):
        if not low.startswith(a.lower() + "-"):
            continue
        start = len(a) + 1
        tail = "_" + b.lower() + "-"
        idx = low.find(tail, start)
        if idx < 0:
            continue
        port_b_start = idx + 1 + len(b) + 1
        port_a = name[start:idx]
        port_b = name[port_b_start:]
        # 两端端口都为空说明链路名不符合约定（例如两端同名的自环），不当成有效解析
        if not port_a or not port_b:
            return None
        return (name[:len(a)], port_a), (name[idx + 1:idx + 1 + len(b)], port_b)
    return None


def util_pct(mbps, gb):
    """Mbps / (GB × 1000) → 百分比；任一缺失或带宽非法返回 None（界面显示 N/A）。"""
    if mbps is None or gb is None:
        return None
    try:
        rate = float(mbps)
        speed = float(gb)
    except (TypeError, ValueError):
        return None
    if speed <= 0:
        return None
    return round(rate * 100.0 / (speed * 1000.0), 2)


def annotate(edges: list, node_names: dict, snapshot: dict, spec: layers.LayerSpec) -> dict:
    """把 `snapshot` 里的端口速率贴到 `edges` 上（就地修改），返回统计。

    snapshot: {(网元名小写, 端口名小写): (rx_mbps, tx_mbps)}
    """
    stats = {
        "totalEdges": len(edges), "matchedEdges": 0,
        "totalPorts": 0, "matchedPorts": 0, "noBandwidthPorts": 0,
    }
    for edge in edges:
        s_name = node_names.get(edge.get("s"), "")
        t_name = node_names.get(edge.get("t"), "")
        best_rx = None
        best_tx = None
        per_port = []
        for link_name in edge.get("ports") or []:
            pair = parse_link_ports(link_name, s_name, t_name)
            if pair is None:
                continue
            stats["totalPorts"] += 1
            (me_a, port_a), (me_b, port_b) = pair
            # 源侧 = 与 edge.s 同名的那一端；链路名的顺序由 UME 决定，不一定与 s/t 同向
            if me_a.lower() == s_name.lower():
                (src_me, src_port), (dst_me, dst_port) = (me_a, port_a), (me_b, port_b)
            else:
                (src_me, src_port), (dst_me, dst_port) = (me_b, port_b), (me_a, port_a)

            row = snapshot.get((src_me.lower(), src_port.lower()))
            gb = layers.speed_gb(src_port, spec)
            side = "src"
            if row is None:
                # 源侧没采到：宿侧「收到」= 源侧「发出」，两个方向对调，不当成同一方向
                row = snapshot.get((dst_me.lower(), dst_port.lower()))
                if row is not None:
                    row = (row[1], row[0])
                    gb = layers.speed_gb(dst_port, spec)
                    side = "dst"
            if row is None:
                continue

            stats["matchedPorts"] += 1
            if gb is None:
                # 速率解析不出来（ETH/无 GE 字样）：有流量也算不出百分比，如实计入
                stats["noBandwidthPorts"] += 1
                continue
            rx = util_pct(row[0], gb)
            tx = util_pct(row[1], gb)
            if rx is None and tx is None:
                continue
            per_port.append({
                "port": src_port if side == "src" else dst_port,
                "me": src_me if side == "src" else dst_me,
                "side": side, "gb": gb, "rx": rx, "tx": tx,
            })
            if rx is not None and (best_rx is None or rx > best_rx):
                best_rx = rx
            if tx is not None and (best_tx is None or tx > best_tx):
                best_tx = tx

        if best_rx is not None or best_tx is not None:
            stats["matchedEdges"] += 1
            edge["rxUtil"] = best_rx
            edge["txUtil"] = best_tx
            edge["utilPorts"] = per_port[:MAX_UTIL_PORTS]
    return stats


# ----------------------------------------------------------------------------
# 读库
# ----------------------------------------------------------------------------
def kpi_tables() -> list:
    """采集侧配置里的 15 分钟表名（去重、保序）。表名真相源在 KPI 服务，不在此处。"""
    out = []
    seen = set()
    for cfg in IPRAN_KPI_QUERY_CONFIGS.values():
        table = str((cfg or {}).get("table_name") or "").strip()
        if table and table not in seen:
            seen.add(table)
            out.append(table)
    return out


def read_snapshot(tables: list | None = None) -> dict:
    """读每张表最新采样时刻的收发速率。

    返回 {"rows": {...}, "sampledAt": str|None, "tables": [...], "rowCount": int}。
    表不存在 / 结构不认识 / 空表都只是跳过，不让一层的流量数据把拓扑取数带崩。
    """
    tables = list(tables or kpi_tables())
    rows: dict = {}
    used = []
    total = 0
    conn = get_raw_connection(str(DB_PATH), logical_name="ipran_ume", timeout=20)
    try:
        for table in tables:
            try:
                cols = set(table_columns(conn, table))
            except Exception as exc:  # 表不存在（新机器 / 采集还没跑过）
                logger.debug("[TRAFFIC] 表 %s 不可用: %s", table, exc)
                continue
            eav = {"Counter", "Counter Value"} <= cols
            wide = {RX_COUNTER, TX_COUNTER} <= cols
            if not (eav or wide):
                logger.debug("[TRAFFIC] 表 %s 没有收发流量列，跳过（列: %s）", table, sorted(cols))
                continue
            cur = conn.cursor()
            try:
                cur.execute(f'SELECT MAX("Begin Time") FROM "{table}"')
                got = cur.fetchone()
            finally:
                cur.close()
            ts = got[0] if got else None
            if ts is None:
                continue
            if eav:
                sql = (f'SELECT "ME","Measure Object","Counter","Counter Value" FROM "{table}" '
                       f'WHERE "Begin Time" = ?')
            else:
                sql = (f'SELECT "ME","Measure Object","{RX_COUNTER}","{TX_COUNTER}" FROM "{table}" '
                       f'WHERE "Begin Time" = ?')
            cur = conn.cursor()
            try:
                cur.execute(sql, (ts,))
                raw = cur.fetchall()
            finally:
                cur.close()
            used.append(table)
            total += len(raw)
            for r in raw:
                me = str(r[0] or "").strip()
                mo = str(r[1] or "").strip()
                if not me or not mo or _SUBPORT_RE.search(mo):
                    continue
                if eav:
                    counter = str(r[2] or "")
                    if counter == RX_COUNTER:
                        rx, tx = r[3], None
                    elif counter == TX_COUNTER:
                        rx, tx = None, r[3]
                    else:
                        continue
                else:
                    rx, tx = r[2], r[3]
                key = (me.lower(), mo.lower())
                stamp = str(ts)
                prev = rows.get(key)
                if prev is None:
                    rows[key] = [rx, tx, stamp]
                elif stamp > prev[2]:
                    # 同一端口在两张表里都出现时取时间戳较新的那条（文本比较，
                    # 同表内时间格式一致；跨表格式不同时以字符串较大者为准）
                    prev[0], prev[1], prev[2] = rx, tx, stamp
                elif stamp == prev[2]:
                    # 同一采样时刻的 Receive / Send 是两行，必须合并而不是互相覆盖
                    if rx is not None:
                        prev[0] = rx
                    if tx is not None:
                        prev[1] = tx
    finally:
        conn.close()

    latest = max((v[2] for v in rows.values()), default=None)
    return {
        "rows": {k: (v[0], v[1]) for k, v in rows.items()},
        "sampledAt": latest,
        "tables": used,
        "rowCount": total,
    }


# ----------------------------------------------------------------------------
# 对外入口（被 ipran_topo_service._aggregate 调用）
# ----------------------------------------------------------------------------
def annotate_edges(edges: list, nodes: list, spec: layers.LayerSpec) -> dict:
    """给 `edges` 就地补 rxUtil / txUtil / utilPorts，返回要放进 `meta.traffic` 的片段。

    任何异常都只转成 warning —— 拓扑本身能出图，不能因为 KPI 库读不到就整层失败。
    """
    base = {"tables": [], "sampledAt": None, "rows": 0, "totalEdges": len(edges),
            "matchedEdges": 0, "totalPorts": 0, "matchedPorts": 0,
            "noBandwidthPorts": 0, "warnings": []}
    try:
        snap = read_snapshot()
    except Exception as exc:
        logger.warning("[TRAFFIC] 读取端口 KPI 失败: %s", exc)
        base["warnings"].append(f"traffic_no_data: 读取 {DB_PATH.name} 失败：{exc}")
        return base

    base.update({"tables": snap["tables"], "sampledAt": snap["sampledAt"], "rows": snap["rowCount"]})
    if not snap["rows"]:
        base["warnings"].append("traffic_no_data: 没有可用的端口流量行（采集任务未跑或库为空）")
        return base

    node_names = {n.get("id"): n.get("name") or "" for n in nodes}
    stats = annotate(edges, node_names, snap["rows"], spec)
    base.update(stats)

    total = stats["totalEdges"]
    matched = stats["matchedEdges"]
    if matched == 0:
        base["warnings"].append(
            f"traffic_unmatched: KPI 采样 {snap['sampledAt']}，但本层 {total} 条链路没有一条匹配到端口流量")
    elif matched < total:
        pct = round(matched * 100 / max(1, total))
        base["warnings"].append(
            f"traffic_partial: 本层 {total} 条链路里 {matched} 条有流量数据（{pct}%），"
            f"其余端口当前未被 KPI 采集覆盖")
    if stats["noBandwidthPorts"]:
        base["warnings"].append(
            f"traffic_no_bandwidth: {stats['noBandwidthPorts']} 个已采到的端口无法从链路名解析速率（如 ETH 口），"
            f"利用率显示 N/A")
    logger.info("[TRAFFIC] layer=%s 边 %d/%d 端口 %d/%d 采样 %s",
                spec.key, matched, total, stats["matchedPorts"], stats["totalPorts"],
                snap["sampledAt"])
    return base
