"""分派白名单：从 Agent 注册表解析「可接任务」的 builder（去硬编码）。

`docs/workspace/agent-registry.md` 是 Worker 注册的唯一真相源（谁在、什么角色、
是否退役）。分派链路过去把 `agent:muse-2/3` 写死在 `protocol.BUILDERS` 里，新增
Muse 4 / Muse 5 后自动分派认不出它们的标签，必须改代码才能扩白名单
（2026-11-16 Issue #108 实测：`agent:muse-4` 的任务永远不满足就绪条件）。

本模块把注册表主表投影成标签白名单：

- 只取「角色含 `Worker`」且**状态不含「已退役」**的行；
- 标签由 Agent 名归一化而来：`Muse 4` → `agent:muse-4`；
- 解析不到任何 builder 时**显式抛错**——注册表格式漂移不能静默变成“谁都不派”。

注册表是人工维护的中文表格，格式一变解析就会失败；`tests/test_builder_registry.py`
用真实文件做回归，任何漂移都会在 CI 里变红。改表时保持「Agent | 角色 | 运行环境 |
GitHub 身份 | 状态 | 过渡方案」六列即可，角色列写 `Worker` 才会进白名单。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 注册表相对仓库根目录的位置（唯一真相源）。
REGISTRY_RELATIVE_PATH = Path("docs/workspace/agent-registry.md")

#: dispatch/ → backend/ → control-plane/ → apps/ → 仓库根。
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_REGISTRY_PATH = REPO_ROOT / REGISTRY_RELATIVE_PATH

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WORKER_MARK = "Worker"
_RETIRED_MARK = "已退役"


class BuilderRegistryError(RuntimeError):
    """注册表不可读 / 解析不到 builder：分派链路必须显式失败，不能猜。"""


def label_for(agent_name: str) -> str:
    """Agent 名 → 标签：`Muse 4` → `agent:muse-4`。"""
    slug = _SLUG_RE.sub("-", str(agent_name or "").strip().lower()).strip("-")
    return f"agent:{slug}"


def parse_builders(text: str) -> tuple[str, ...]:
    """把注册表主表投影成 builder 标签元组（顺序与表格一致）。

    判定规则见模块注释；表头行、分隔行、非 Worker 行（如 Muse 1 总指挥）、
    已退役行（如 Grok Bot）都会被跳过。
    """
    builders: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        name, role, status = cells[0], cells[1], cells[4]
        if not name or name == "Agent" or "---" in name:
            continue
        if _WORKER_MARK not in role or _RETIRED_MARK in status:
            continue
        label = label_for(name)
        if label not in builders:
            builders.append(label)
    return tuple(builders)


def load_builders(path: Path | str | None = None) -> tuple[str, ...]:
    """读取注册表并返回白名单；文件缺失或解析为空都抛 `BuilderRegistryError`。"""
    target = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuilderRegistryError(f"读不到 Agent 注册表 {target}：{exc}") from exc
    builders = parse_builders(text)
    if not builders:
        raise BuilderRegistryError(
            f"Agent 注册表 {target} 解析不到任何 Worker（表格格式变了？"
            "角色列需含 Worker、状态列不能是已退役）"
        )
    return builders


def is_builder(label: str, builders: tuple[str, ...] | None = None) -> bool:
    """该标签是否在白名单内（白名单缺省取真实注册表）。"""
    allowed = builders if builders is not None else load_builders()
    return label in allowed
