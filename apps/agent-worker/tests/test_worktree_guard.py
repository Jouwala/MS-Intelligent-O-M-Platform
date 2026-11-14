"""Worktree 守卫验收：POSIX 钩子可执行位（TASK-12）+ 孤儿 worktree 回收（TASK-06）。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import wt


def _make_repo(tmp_path: Path, hook_mode: int) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    hooks = repo / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(hook_mode)
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    git("config", "core.hooksPath", ".githooks")
    return repo


def test_hook_exec_check_rejects_644_on_posix(tmp_path):
    hook = tmp_path / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o644)
    ok, problem = wt.hook_executable_check(hook)
    if os.name == "posix":
        assert ok == ""
        assert "可执行位" in problem and "chmod" in problem
    else:
        # Windows 没有可执行位语义：明确跳过并记录原因（TASK-12 的验收点）
        assert "跳过" in ok and problem == ""


@pytest.mark.skipif(os.name != "posix", reason="POSIX 可执行位语义；Windows 明确跳过并记录原因")
def test_validate_hooks_fails_when_pre_commit_is_644(tmp_path):
    repo = _make_repo(tmp_path, hook_mode=0o644)
    allocator = wt.WorktreeAllocator(repo)
    path = allocator.allocate("task-644")
    try:
        report = allocator.validate_hooks(path)
        assert report.ok is False
        assert any("可执行位" in problem for problem in report.problems)
    finally:
        allocator.release("task-644", force=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX 可执行位语义")
def test_validate_hooks_passes_when_pre_commit_is_executable(tmp_path):
    repo = _make_repo(tmp_path, hook_mode=0o755)
    allocator = wt.WorktreeAllocator(repo)
    path = allocator.allocate("task-755")
    try:
        report = allocator.validate_hooks(path)
        assert report.ok, report.problems
        assert any("可执行" in check for check in report.checks)
    finally:
        allocator.release("task-755", force=True)


def test_validate_hooks_skips_exec_bit_on_windows(tmp_path):
    repo = _make_repo(tmp_path, hook_mode=0o644)
    allocator = wt.WorktreeAllocator(repo)
    path = allocator.allocate("task-skip")
    try:
        report = allocator.validate_hooks(path)
        if os.name == "posix":
            assert report.ok is False
        else:
            assert report.ok is True
            assert any("跳过" in check for check in report.checks)
    finally:
        allocator.release("task-skip", force=True)


# -- 孤儿回收 ------------------------------------------------------------------


def test_reclaim_orphans_removes_inactive_only_and_is_idempotent(git_repo):
    allocator = wt.WorktreeAllocator(git_repo)
    keep = allocator.allocate("task-a")
    orphan = allocator.allocate("task-b")

    report = allocator.reclaim_orphans({"task-a"})
    assert report.reclaimed == ["task-b"]
    assert report.failed == []
    assert keep.exists() and not orphan.exists()

    again = allocator.reclaim_orphans({"task-a"})  # 幂等：没有新孤儿
    assert again.reclaimed == [] and again.failed == []
    allocator.release("task-a", force=True)


def test_reclaim_orphans_failure_is_non_fatal(git_repo, monkeypatch):
    allocator = wt.WorktreeAllocator(git_repo)
    allocator.allocate("task-a")
    allocator.allocate("task-b")
    original = allocator.release

    def flaky(slug: str, force: bool = False):
        if slug == "task-b":
            raise RuntimeError("目录被占用")
        return original(slug, force=force)

    monkeypatch.setattr(allocator, "release", flaky)
    report = allocator.reclaim_orphans(set())  # 单个失败不得中断整轮
    assert report.reclaimed == ["task-a"]
    assert report.failed and report.failed[0][0] == "task-b"
    assert "目录被占用" in report.failed[0][1]

    monkeypatch.setattr(allocator, "release", original)
    allocator.release("task-b", force=True)
