# -*- coding: utf-8 -*-
"""拓扑流量贴图的纯函数测试（不连库、不连 UME）。

背景：把 iPRAN KPI 的端口速率贴到 UME 拓扑边上，**唯一的难点是链路名解析**。
UME 链路名形如 `A设备-端口_B设备-端口`，而设备名自带下划线
（`M6K_2S16_Logan_Town_UPE100`）、板卡名自带减号（`P90E-4XGET-SFP+[0-0-0-1]-10GE:2`），
按分隔符切必错。本文件用的三条链路名都是 2026-11-19 从生产 `/api/ipran-topo/graph`
原样抄下来的真名，改解析逻辑时先回头看它们。
"""
from backend.services.ipran_topo import layers, topo_traffic as tt

SPEC = layers.LAYERS["ipran"]

# 真实链路名（2026-11-19 生产抓取，未改动）
LINK_KPI = ("M6K_2s16_LBS_UPE59-RPN-02CG-H-VIRTUAL[0-0-7-0]-100GE:1_"
            "M6K_2s16_Nigeria_House_UPE97-RPN-02CG-H-VIRTUAL[0-0-3-0]-100GE:2")
LINK_ETH = "9000-8EA_SPE01-[0-0-2-0]-ETH:2_ODC_SPE03-[0-0-2-0]-ETH:2"
LINK_PARALLEL = ("89MW02-H1GF48A[0-0-1-0]-GE:30_"
                 "9000-8EA_SPE02-P90E-20GET-SFPRJ[0-0-1-0]-GE:12")


def _edge(s, t, ports, **extra):
    return {"s": s, "t": t, "n": len(ports), "gb": 10, "down": 0, "crit": 0, "major": 0,
            "ports": list(ports), **extra}


def test_parse_link_ports_splits_on_known_ne_names():
    left = "M6K_2s16_LBS_UPE59"
    right = "M6K_2s16_Nigeria_House_UPE97"
    got = tt.parse_link_ports(LINK_KPI, left, right)
    assert got == (
        (left, "RPN-02CG-H-VIRTUAL[0-0-7-0]-100GE:1"),
        (right, "RPN-02CG-H-VIRTUAL[0-0-3-0]-100GE:2"),
    )


def test_parse_link_ports_is_orientation_agnostic():
    """链路名里的两端顺序由 UME 决定，跟边的 s/t 不一定同向。"""
    left = "M6K_2s16_LBS_UPE59"
    right = "M6K_2s16_Nigeria_House_UPE97"
    got = tt.parse_link_ports(LINK_KPI, right, left)
    assert got is not None
    assert got[0][0] == left and got[1][0] == right


def test_parse_link_ports_handles_empty_port_prefix():
    """ETH 口的链路名没有板卡名，端口直接从 `[0-0-2-0]` 开始，同样要能取出来。"""
    got = tt.parse_link_ports(LINK_ETH, "9000-8EA_SPE01", "ODC_SPE03")
    assert got == (("9000-8EA_SPE01", "[0-0-2-0]-ETH:2"), ("ODC_SPE03", "[0-0-2-0]-ETH:2"))


def test_parse_link_ports_rejects_unknown_names():
    assert tt.parse_link_ports(LINK_KPI, "不存在A", "不存在B") is None
    assert tt.parse_link_ports("", "A", "B") is None


def test_util_pct_units_and_missing_values():
    # 195.05 Mbps / 10G = 1.95%
    assert tt.util_pct(195.05, 10) == 1.95
    assert tt.util_pct(0, 10) == 0.0
    assert tt.util_pct(None, 10) is None
    assert tt.util_pct(100, None) is None
    assert tt.util_pct(100, 0) is None
    assert tt.util_pct("bad", 10) is None


def test_annotate_fills_both_directions_on_folded_edge():
    """折叠边：方向取组内最大；每条链路的明细都留在 utilPorts。"""
    edge = _edge("a", "b", [LINK_KPI])
    snapshot = {
        ("m6k_2s16_lbs_upe59", "rpn-02cg-h-virtual[0-0-7-0]-100ge:1"): (32.7, 195.05),
    }
    stats = tt.annotate([edge], {"a": "M6K_2s16_LBS_UPE59", "b": "M6K_2s16_Nigeria_House_UPE97"},
                        snapshot, SPEC)
    assert stats["matchedEdges"] == 1
    assert stats["matchedPorts"] == 1
    # 端口名带 100GE:1，速率按 100G 计：32.7/100000*100=0.03，195.05/100000*100=0.20
    assert edge["rxUtil"] == 0.03
    assert edge["txUtil"] == 0.2
    assert edge["utilPorts"][0]["side"] == "src"


def test_annotate_falls_back_to_dest_side_and_swaps_directions():
    """源侧没采到：用宿侧的 Receive 当源的 Send，方向必须对调而不是照抄。"""
    edge = _edge("a", "b", [LINK_KPI])
    snapshot = {
        ("m6k_2s16_nigeria_house_upe97", "rpn-02cg-h-virtual[0-0-3-0]-100ge:2"): (10.0, 500.0),
    }
    tt.annotate([edge], {"a": "M6K_2s16_LBS_UPE59", "b": "M6K_2s16_Nigeria_House_UPE97"},
                snapshot, SPEC)
    # 宿侧 receive 10 Mbps = 源侧发出的；宿侧 send 500 Mbps = 源侧收到的（100G 端口）
    assert edge["txUtil"] == 0.01
    assert edge["rxUtil"] == 0.5
    assert edge["utilPorts"][0]["side"] == "dst"


def test_annotate_skips_port_without_parseable_speed():
    """ETH 口解析不出速率：有流量也不编百分比，只计入 noBandwidthPorts。"""
    edge = _edge("a", "b", [LINK_ETH])
    snapshot = {("9000-8ea_spe01", "[0-0-2-0]-eth:2"): (100.0, 50.0)}
    stats = tt.annotate([edge], {"a": "9000-8EA_SPE01", "b": "ODC_SPE03"}, snapshot, SPEC)
    assert stats["matchedPorts"] == 1
    assert stats["noBandwidthPorts"] == 1
    assert "rxUtil" not in edge and "txUtil" not in edge


def test_annotate_marks_nothing_when_no_rows():
    """一条都没匹配到：不给 util 字段，界面才会显示 N/A 而不是 0%。"""
    edge = _edge("a", "b", [LINK_PARALLEL])
    stats = tt.annotate([edge], {"a": "89MW02", "b": "9000-8EA_SPE02"}, {}, SPEC)
    assert stats["matchedEdges"] == 0
    assert "rxUtil" not in edge
    assert "utilPorts" not in edge


def test_kpi_tables_come_from_collector_config():
    """表名真相源在采集配置里，本模块不另抄一份。"""
    from backend.services.ipran.ipran_ume_kpi_service import IPRAN_KPI_QUERY_CONFIGS

    expected = []
    for cfg in IPRAN_KPI_QUERY_CONFIGS.values():
        name = cfg.get("table_name")
        if name and name not in expected:
            expected.append(name)
    assert tt.kpi_tables() == expected
    assert len(tt.kpi_tables()) >= 1
