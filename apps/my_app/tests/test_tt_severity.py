# -*- coding: utf-8 -*-
"""告警级别展示归一化（2026-11-18 切生产后复核）。

上游 `eflow_tt.alarm_severity` 是数字码（1005001…），而值班群/Excel 的格式是
`Alarm: 9910(Critical)`。回归用例 `test_tt_alarm_render.py` 锁的是用户 2026-09-29
指定的格式；本文件只锁映射本身，不依赖数据库。

引擎侧（`apps/whatsapp-kb-bot/scripts/tt-analyze.py`）有一份等价实现，两处映射
必须一致 —— 这里把「码 → 名」的对应关系固定下来。
"""
from backend.services.tt.tt_severity import ALARM_SEVERITY_LABELS, norm_alarm_severity


def test_known_codes_map_to_labels():
    assert norm_alarm_severity("1005001") == "Critical"
    assert norm_alarm_severity("1005002") == "Major"
    assert norm_alarm_severity("1005003") == "Minor"
    assert norm_alarm_severity("1005004") == "Warning"
    assert norm_alarm_severity("1005005") == "Info"


def test_label_and_unknown_are_kept():
    # 已经是等级名（历史数据/测试夹具）不能被改坏
    assert norm_alarm_severity("Critical") == "Critical"
    assert norm_alarm_severity(" critical ") == "critical"
    # 未知码原样返回，不吞成空 —— 宁可显示原始值也不要显示「—」
    assert norm_alarm_severity("9999999") == "9999999"


def test_empty_is_empty_string():
    assert norm_alarm_severity(None) == ""
    assert norm_alarm_severity("") == ""
    assert norm_alarm_severity("   ") == ""


def test_map_has_no_duplicate_labels_for_same_code():
    assert len(ALARM_SEVERITY_LABELS) == 5
