"""任务命令环境白名单验收（#78）。

口径：任务子进程只拿到最小白名单，Worker 环境里的 Secret / 生产凭据
（webhook key、PAT、云凭据等）一律不可见；白名单内的运行必需变量
（PATH 等）保留，任务仍能正常执行。
"""

from __future__ import annotations

import os
import sys

import _bootstrap

ef = _bootstrap.load_sibling("env_filter")
from conftest import ex  # noqa: E402

FAKE_SOURCE = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/worker",
    "LC_ALL": "C.UTF-8",
    "PWD": "/somewhere",
    # 以下都是不该进任务环境的敏感/无关变量
    "GITHUB_TOKEN": "ghp_fake",
    "GROK_ROUTINE_WEBHOOK_KEY": "whsec_fake",
    "AWS_SECRET_ACCESS_KEY": "aws_fake",
    "DATABASE_URL": "postgres://prod",
    "SOME_API_SECRET": "s3cr3t",
    "RANDOM_TOOL_FLAG": "1",
}


def test_secrets_and_unknown_vars_are_stripped():
    env = ef.build_task_env(source=dict(FAKE_SOURCE))
    for key in (
        "GITHUB_TOKEN",
        "GROK_ROUTINE_WEBHOOK_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
        "SOME_API_SECRET",
        "RANDOM_TOOL_FLAG",
        "PWD",  # shell 簿记变量也不带
    ):
        assert key not in env, key


def test_whitelist_vars_are_kept():
    env = ef.build_task_env(source=dict(FAKE_SOURCE))
    assert env["PATH"] == "/usr/bin:/bin"
    if os.name != "nt":
        assert env["HOME"] == "/home/worker"
        assert env["LC_ALL"] == "C.UTF-8"  # LC_* 前缀放行


def test_extra_allowed_is_explicit_and_case_insensitive():
    env = ef.build_task_env(source=dict(FAKE_SOURCE), extra_allowed=("random_tool_flag",))
    assert env["RANDOM_TOOL_FLAG"] == "1"
    assert "GITHUB_TOKEN" not in env  # 没显式追加的仍然进不来


def test_source_is_not_mutated_and_missing_vars_not_invented():
    src = {"PATH": "/usr/bin"}
    env = ef.build_task_env(source=src)
    assert src == {"PATH": "/usr/bin"}
    assert "HOME" not in env  # 源里没有就不会凭空造


def test_executor_run_child_env_has_no_secret(tmp_path, monkeypatch):
    """端到端：经 Executor.run 真起子进程，子进程看到的环境无 Secret。"""
    monkeypatch.setenv("TASK78_FAKE_SECRET", "must-not-leak")
    monkeypatch.setenv("GROK_ROUTINE_WEBHOOK_KEY", "must-not-leak-either")
    out_file = tmp_path / "child_env.txt"
    script = (
        "import os, sys\n"
        f"open({str(out_file)!r}, 'w').write('\\n'.join(sorted(os.environ)))\n"
    )
    executor = ex.Executor(timeout_s=60)
    result = executor.run([sys.executable, "-c", script], tmp_path)
    assert result.exit_code == 0
    child_keys = out_file.read_text().splitlines()
    assert "TASK78_FAKE_SECRET" not in child_keys
    assert "GROK_ROUTINE_WEBHOOK_KEY" not in child_keys
    assert "PATH" in child_keys  # 运行必需变量还在，任务能正常跑
