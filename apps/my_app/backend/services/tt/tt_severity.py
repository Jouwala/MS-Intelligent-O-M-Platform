# -*- coding: utf-8 -*-
"""TT 告警级别展示归一化（数字码 → 人能读的等级）。

## 背景（2026-11-18 切生产后复核发现）

`eflow_tt.alarm_severity` 在上游导出里是数字码，生产 PG 实测 41k 行全为
`1005001` / `1005002` / `1005003` / `1005004`；而值班群与 Excel 期望的是
`Alarm: 9910(Critical)`。修复前消息里直接是 `Alarm: 9910(1005001)`，
回归用例 `tests/test_tt_alarm_render.py::test_real_engine_matches_user_specified_format`
因此长期失败（它锁的是用户 2026-09-29 指定的格式）。

只做**展示层**归一化，不动库里的值：
- 规则匹配用的是 `ticket_severity`（P1~P4），与本映射无关；
- 映射沿用 ZTE UME 的告警级别码，未知值原样返回（不吞成空）。

引擎侧 `apps/whatsapp-kb-bot/scripts/tt-analyze.py::norm_alarm_severity` 有一份
等价实现 —— 那是独立进程/共享组件，不能 import 本模块；改映射时两处一起改。
"""
from typing import Any

ALARM_SEVERITY_LABELS = {
    "1005001": "Critical",
    "1005002": "Major",
    "1005003": "Minor",
    "1005004": "Warning",
    "1005005": "Info",
}


def norm_alarm_severity(value: Any) -> str:
    """数字码 → 等级名；已是等级名/未知值原样返回；空值 → ''。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return ALARM_SEVERITY_LABELS.get(text, text)
