"""文件范围（scope）规范化与重叠判定（TASK-04）。

策略（ADR-4：路径规范化统一、拒绝越界穿越、大小写按仓库策略）

1. 反斜杠统一转 `/`，去掉首尾空白；
2. 消掉空段与 `.`，逐段折叠 `..`；`..` 越过仓库根即视为路径穿越，直接 `ValueError`；
3. 拒绝绝对路径（`/x`、`C:/x`、`\\\\server\\share`）：scope 是仓库相对路径；
4. 大小写**默认敏感**（与 ADR-4 一致：仓库在 Linux 上是大小写敏感文件系统）；
   Windows 盘符/分隔符差异在比较前已统一（`\\` → `/`、消 `.`/`..`）。
   需要大小写不敏感策略时显式传 `casefold=True`（在 Windows 共享盘/大小写混乱的
   历史仓库里可选开启，作为保守串行化的开关，默认不开）。
5. 重叠 = 完全相同，或一方是另一方的目录前缀（`a/b` 与 `a/b/c.py` 重叠）。
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def normalize_scope(raw: str, *, casefold: bool = False) -> str:
    """把单个 scope 规范化为「仓库相对、正斜杠」形式；非法输入抛 ValueError。

    `casefold=False`（默认）保留大小写，按仓库文件系统语义比较；
    `casefold=True` 统一小写，适合需要保守串行化的场景。
    """
    if not isinstance(raw, str):
        raise ValueError(f"文件范围必须是字符串：{raw!r}")
    text = raw.strip().replace("\\", "/")
    if not text:
        raise ValueError("文件范围不能为空")
    if text.startswith("//"):
        raise ValueError(f"文件范围不能是 UNC 路径：{raw}")
    if text.startswith("/"):
        raise ValueError(f"文件范围必须是仓库相对路径，不能以 / 开头：{raw}")
    if _DRIVE_RE.match(text):
        raise ValueError(f"文件范围必须是仓库相对路径，不能是盘符绝对路径：{raw}")

    parts: list[str] = []
    for segment in text.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise ValueError(f"文件范围出现路径穿越（.. 越过仓库根）：{raw}")
            parts.pop()
            continue
        parts.append(segment)
    if not parts:
        raise ValueError(f"文件范围规范化后为空：{raw}")
    normalized = "/".join(parts)
    return normalized.casefold() if casefold else normalized


def normalize_scopes(scopes: Iterable[str], *, casefold: bool = False) -> tuple[str, ...]:
    """批量规范化并去重（保持输入顺序）。任何一项非法都整体拒绝。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in scopes:
        normalized = normalize_scope(raw, casefold=casefold)
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return tuple(out)


def scopes_overlap(
    left: Sequence[str], right: Sequence[str], *, casefold: bool = False
) -> list[str]:
    """返回两组范围里重叠的条目（去重保序）。

    无法规范化的条目（历史脏数据）按「字面统一分隔符后比较」参与判定，
    而不是被静默忽略——漏判会导致两个 Worker 同时改同一文件。
    """

    def canon(items: Sequence[str]) -> list[str]:
        result: list[str] = []
        for item in items:
            try:
                result.append(normalize_scope(item, casefold=casefold))
            except ValueError:
                literal = str(item).strip().replace("\\", "/")
                result.append(literal.casefold() if casefold else literal)
        return result

    left_c, right_c = canon(left), canon(right)
    overlap: list[str] = []
    for a in left_c:
        for b in right_c:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                if a and a not in overlap:
                    overlap.append(a)
                break
    return overlap
