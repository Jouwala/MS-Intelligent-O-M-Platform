"""命令解析离线自测（TASK-13）。

同时覆盖旧行为（引号参数、引号不成对回退、无命令节）与新增契约：
结构化 JSON argv 优先、Windows 反斜杠路径不损坏、POSIX 平台感知 shlex、
非法 JSON 回退，以及永不 shell=True 的 argv 形态。
"""

from __future__ import annotations

import json
import os

import pytest

from conftest import cp, wk


def _body(command: str) -> str:
    return "## 目标\n写结果文件。\n## 执行命令\n```\n" + command + "\n```\n"


def _json_body(argv_json: str, info: str = "json") -> str:
    return "## 目标\n写结果文件。\n## 执行命令\n```" + info + "\n" + argv_json + "\n```\n"


# -- 旧行为保持 ---------------------------------------------------------------

def test_quoted_arg_kept_whole() -> None:
    assert wk.AgentWorker._command_from_issue(
        _body('python3 -c "print(1 + 2)"')
    ) == ["python3", "-c", "print(1 + 2)"]


def test_plain_command() -> None:
    assert wk.AgentWorker._command_from_issue(
        _body("python3 scripts/check.py --fast")
    ) == ["python3", "scripts/check.py", "--fast"]


def test_unbalanced_quote_falls_back_to_split() -> None:
    assert wk.AgentWorker._command_from_issue(
        _body('python3 -c "print(1)')
    ) == ["python3", "-c", '"print(1)']


def test_no_command_section_returns_none() -> None:
    assert wk.AgentWorker._command_from_issue("## 目标\n写结果文件。\n") is None
    assert wk.AgentWorker._command_from_issue("") is None


# -- 结构化 argv 优先 ---------------------------------------------------------

def test_json_argv_takes_priority_over_shlex():
    argv = [r"C:\tools\x.exe", "a b", "--flag=1"]
    assert wk.AgentWorker._command_from_issue(_json_body(json.dumps(argv))) == argv


def test_json_argv_multiline():
    body = _json_body('[\n  "python3",\n  "scripts/check.py"\n]')
    assert wk.AgentWorker._command_from_issue(body) == ["python3", "scripts/check.py"]


def test_invalid_json_falls_back_to_command_line():
    # json 标注但内容非法 → 按第一行命令兜底，不抛异常
    assert wk.AgentWorker._command_from_issue(_json_body("not-json arg")) == [
        "not-json",
        "arg",
    ]


def test_json_array_of_non_strings_rejected():
    assert cp.parse_json_argv(["[1, 2]"]) is None
    assert cp.parse_json_argv([""]) is None


def test_plain_fence_ignores_json_marker_absent():
    assert wk.AgentWorker._command_from_issue(_body("echo hi")) == ["echo", "hi"]


# -- 平台感知 shlex -----------------------------------------------------------

def test_windows_backslash_path_preserved_with_posix_false():
    argv = cp.split_command(r'C:\tools\x.exe "a b"', posix=False)
    assert argv == [r"C:\tools\x.exe", "a b"]


def test_windows_quoted_program_files_path():
    argv = cp.split_command(r'"C:\Program Files\tool\x.exe" --fast', posix=False)
    assert argv == [r"C:\Program Files\tool\x.exe", "--fast"]


def test_posix_quotes_and_escapes():
    argv = cp.split_command('python3 -c "print(1 + 2)" --name \'a b\'', posix=True)
    assert argv == ["python3", "-c", "print(1 + 2)", "--name", "a b"]


def test_unbalanced_quote_falls_back_everywhere():
    assert cp.split_command('x "unclosed', posix=True) == ["x", '"unclosed']
    assert cp.split_command('x "unclosed', posix=False) == ["x", '"unclosed']


@pytest.mark.skipif(os.name == "posix", reason="Windows 默认 posix=False 的行为仅在 Windows 上有意义")
def test_default_split_keeps_windows_path_on_windows():
    assert cp.split_command(r"C:\tools\x.exe") == [r"C:\tools\x.exe"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX 默认 posix=True 的行为在 Windows 上不适用")
def test_default_split_uses_posix_rules_on_posix():
    assert cp.split_command('python3 -c "print(1)"') == ["python3", "-c", "print(1)"]


def test_argv_is_list_never_shell_string():
    """解析结果必须是 argv 列表：executor 用 Popen(list) 执行，永不 shell=True。"""
    argv = wk.AgentWorker._command_from_issue(_body("echo one; echo two"))
    assert isinstance(argv, list)
    assert argv[0] == "echo"
    assert all(isinstance(part, str) for part in argv)
