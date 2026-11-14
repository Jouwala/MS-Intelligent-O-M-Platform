"""Issue「执行命令」解析（TASK-13）。

旧实现在 Windows 上用 `shlex.split(line)`（默认 posix=True）解析命令，
而 posix 模式把反斜杠当转义符：`C:\\tools\\x.exe "a b"` 会被拆坏
（`C:toolsx.exe`），带盘符的 Windows 命令根本跑不起来。

现在的解析顺序：

1. **结构化 argv 优先**：`## 执行命令` 一节里带 `json` 标注的代码块，
   内容是一个字符串数组时直接当 argv 用（无需猜引号规则，最可靠）：
   ```json
   ["C:\\\\tools\\\\x.exe", "a b"]
   ```
2. **平台感知 shlex 兜底**：POSIX 用 `posix=True`（处理引号/转义）；
   Windows 用 `posix=False` 保留反斜杠，再手工剥掉成对的外层引号。
3. **引号不成对**（shlex 抛 ValueError）退回 `str.split()` 保底，不抛异常；
4. 永不使用 `shell=True`：executor 始终以 argv 列表 + Popen 执行，
   即使解析结果异常也不会被 shell 二次解释（注入面最小）。
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Optional

SECTION = "执行命令"


def _strip_wrapping_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def split_command(text: str, *, posix: Optional[bool] = None) -> list[str]:
    """按平台拆命令串；POSIX 默认 posix=True，Windows 默认 posix=False。"""
    posix_mode = (os.name == "posix") if posix is None else posix
    try:
        parts = shlex.split(text, posix=posix_mode)
    except ValueError:
        # 引号不成对：shlex 会抛异常，退回朴素 split 保底（不阻断任务）
        return text.split()
    if not posix_mode:
        # posix=False 会把引号留在 token 里（'"a b"'），剥掉成对的外层引号
        parts = [_strip_wrapping_quotes(p) for p in parts]
    return parts


def parse_json_argv(lines: list[str]) -> Optional[list[str]]:
    """把代码块内容当 JSON 解析成 argv；不是合法字符串数组时返回 None。"""
    raw = "\n".join(lines).strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(data, list) and data and all(isinstance(item, str) for item in data):
        return list(data)
    return None


def extract_command(body: str, *, posix: Optional[bool] = None) -> Optional[list[str]]:
    """从 Issue 正文「执行命令」一节取 argv；没有该节/为空返回 None。"""
    in_section = False
    in_block = False
    info = ""
    block_lines: list[str] = []

    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            if in_block:
                break  # 代码块未闭合就到下一节：按已收集内容收尾
            in_section = SECTION in stripped
            continue
        if not in_section:
            continue
        if stripped.startswith("```"):
            if in_block:
                break
            in_block = True
            info = stripped[3:].strip().lower()
            continue
        if in_block:
            block_lines.append(line)
    if not block_lines:
        return None

    if "json" in info:
        argv = parse_json_argv(block_lines)
        if argv is not None:
            return argv  # json 标注但内容非法时继续走 shlex 兜底

    for line in block_lines:
        if line.strip():
            return split_command(line.strip(), posix=posix)
    return None
