"""分派白名单：注册表解析 + 真实文件回归。

`docs/workspace/agent-registry.md` 是白名单的唯一真相源；本测试同时验证
解析规则（Worker / 未退役）与真实文件（Muse 2–5 在、Muse 1 与已退役的
Grok Bot 不在）。注册表改格式时这里会先红，避免分派静默变成“谁都不派”。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatch.builder_registry import (
    BuilderRegistryError,
    label_for,
    load_builders,
    parse_builders,
)

SAMPLE = """\
| Agent | 角色 | 运行环境 | GitHub 身份 | 状态 | 过渡方案 |
| --- | --- | --- | --- | --- | --- |
| Muse 1 | 总指挥 / Orchestrator：拆解、分配、协调 | 独立 VM | x | 已注册 | — |
| Muse 2 | Coding Worker：开发任务 | 独立 VM | x | 独立身份过渡中 | agent-claim: muse-2 |
| Muse 9 | 只读审查 Worker | 待确认 | x | 已注册（2026-11-16） | agent-claim: muse-9 |
| Grok Bot | QA Worker | — | x | **已退役（2026-11-14）** | — |
"""


def test_parse_skips_orchestrator_and_retired() -> None:
    assert parse_builders(SAMPLE) == ("agent:muse-2", "agent:muse-9")


def test_label_for_normalizes_names() -> None:
    assert label_for("Muse 4") == "agent:muse-4"
    assert label_for("  Grok Bot ") == "agent:grok-bot"


def test_live_registry_contains_all_active_workers() -> None:
    builders = load_builders()
    assert "agent:muse-2" in builders
    assert "agent:muse-3" in builders
    assert "agent:muse-4" in builders
    assert "agent:muse-5" in builders
    assert "agent:muse-1" not in builders  # 总指挥不写业务代码
    assert "agent:grok-bot" not in builders  # 已退役


def test_registry_without_workers_fails_loudly(monkeypatch) -> None:
    """表格里没有任何 Worker（格式漂移）时必须显式失败，不能静默返回空名单。"""
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, encoding=None: (
            "| Agent | 角色 | 运行环境 | GitHub 身份 | 状态 | 过渡方案 |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| Muse 1 | 总指挥 | VM | x | 已注册 | — |\n"
        ),
    )
    with pytest.raises(BuilderRegistryError):
        load_builders(Path("agent-registry.md"))


def test_missing_registry_fails_loudly(monkeypatch) -> None:
    def _raise(self, encoding=None):
        raise FileNotFoundError("agent-registry.md")

    monkeypatch.setattr(Path, "read_text", _raise)
    with pytest.raises(BuilderRegistryError):
        load_builders(Path("agent-registry.md"))
