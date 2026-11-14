"""Worktree 分配器 + 钩子校验（TASK-03）。

协议约定（agent-protocol.md 第六节）：一任务一分支 `agent/<任务>`，
一任务一 Worktree `.agent-worktrees/<任务>`。

钩子校验针对 AGENTS.md 第四节第 9 条记录的真实坑：core.hooksPath 是相对
路径、按当前工作树根解析，worktree 里若所在分支还没合入 .githooks，
钩子会静默不执行。validate_hooks() 把这件事变成显式检查结果而不是靠自觉。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class HookReport:
    ok: bool
    checks: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class ReclaimReport:
    """孤儿 worktree 回收结果（TASK-06 的收尾项：不得留下无人认领的 worktree）。"""

    reclaimed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def hook_executable_check(path: Path) -> tuple[str, str]:
    """检查钩子文件的可执行位，返回 (通过时的说明, 失败时的说明)。

    POSIX 上 Git 只执行带可执行位的钩子：权限 644 的 `pre-commit` 会被
    **静默忽略**（不报错、钩子不跑），这是「本地校验看着装了、实际没生效」
    的真实坑（AGENTS.md 第四节第 9 条）。Windows 没有可执行位语义
    （Git for Windows 按 shebang 解释执行），明确跳过并记录原因。
    """
    if os.name != "posix":
        return ("Windows 平台跳过 pre-commit 可执行位检查（Git for Windows 按 shebang 解释执行）", "")
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        return "", f"无法读取 {path} 的权限位：{exc}"
    masked = mode & 0o777
    if mode & 0o111 or os.access(path, os.X_OK):
        return (f".githooks/pre-commit 可执行（mode {oct(masked)}）", "")
    return "", (
        f".githooks/pre-commit 缺少可执行位（mode {oct(masked)}），POSIX 上 Git 会忽略它，"
        "钩子静默不生效；修复：git update-index --chmod=+x .githooks/pre-commit 并重新提交"
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


class WorktreeAllocator:
    def __init__(self, repo_root: Path, base_dir: str = ".agent-worktrees") -> None:
        self.repo_root = Path(repo_root)
        self.base = self.repo_root / base_dir

    # -- 路径 -----------------------------------------------------------------
    def path_for(self, slug: str) -> Path:
        if not _SLUG_RE.match(slug):
            raise ValueError(f"非法任务 slug：{slug!r}（只允许字母数字与 ._-，且不以 . 开头）")
        return self.base / slug

    def branch_for(self, slug: str) -> str:
        return f"agent/{slug}"

    # -- 分配 / 释放 -------------------------------------------------------------
    def allocate(self, slug: str, branch: str | None = None) -> Path:
        """为任务分配 worktree：已存在直接复用；分支不存在则新建。幂等。"""
        path = self.path_for(slug)
        branch = branch or self.branch_for(slug)
        if path.exists() and (path / ".git").exists():
            return path
        self.base.mkdir(parents=True, exist_ok=True)
        _git(self.repo_root, "worktree", "prune")
        exists = _git(self.repo_root, "rev-parse", "--verify", f"refs/heads/{branch}")
        if exists.returncode == 0:
            result = _git(self.repo_root, "worktree", "add", str(path), branch)
        else:
            result = _git(self.repo_root, "worktree", "add", "-b", branch, str(path))
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add 失败：{result.stderr.strip()}")
        return path

    def release(self, slug: str, force: bool = False) -> bool:
        """释放 worktree（默认只移除干净的；force 时连同未提交改动一起丢弃）。"""
        path = self.path_for(slug)
        if not path.exists():
            return False
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        result = _git(self.repo_root, *args)
        if result.returncode != 0:
            raise RuntimeError(f"git worktree remove 失败：{result.stderr.strip()}")
        _git(self.repo_root, "worktree", "prune")
        return True

    # -- 孤儿回收（TASK-06 收尾） ------------------------------------------------
    def reclaim_orphans(self, active_slugs: set[str] | frozenset[str]) -> ReclaimReport:
        """回收 .agent-worktrees/ 下不在 active_slugs 里的孤儿 worktree。

        调用方传「当前活跃任务 slug 集合」（本进程 + 调度器在途任务）；本接口
        - **幂等**：已删除的目录再调用不会报错（返回空）；
        - **失败不抛致命**：git worktree remove 失败（目录被占用、有未跟踪文件
          且 force 仍失败等）只记录到 `failed`，由调用方决定告警；
        - 先 `git worktree prune` 清掉已手工删目录留下的元数据。
        """
        report = ReclaimReport()
        if not self.base.is_dir():
            return report
        _git(self.repo_root, "worktree", "prune")
        active = set(active_slugs)
        for child in sorted(self.base.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue  # 心跳/临时文件不是 worktree
            slug = child.name
            if slug in active:
                continue
            try:
                if self.release(slug, force=True):
                    report.reclaimed.append(slug)
            except Exception as exc:  # noqa: BLE001 - 单个目录失败不得中断整轮回收
                report.failed.append((slug, str(exc)))
        return report

    # -- 钩子校验 ---------------------------------------------------------------
    def validate_hooks(self, worktree: Path) -> HookReport:
        """校验目标 worktree 的提交钩子是否真的生效，返回结构化报告（不抛异常）。"""
        report = HookReport(ok=True)
        hooks_dir = self.repo_root / ".githooks"

        if hooks_dir.is_dir():
            report.checks.append(".githooks/ 目录存在")
        else:
            report.ok = False
            report.problems.append("仓库根 .githooks/ 不存在，钩子无从谈起")

        pre_commit = hooks_dir / "pre-commit"
        if pre_commit.is_file():
            report.checks.append(".githooks/pre-commit 存在")
            check_ok, check_problem = hook_executable_check(pre_commit)
            if check_ok:
                report.checks.append(check_ok)
            else:
                report.ok = False
                report.problems.append(check_problem)
        else:
            report.ok = False
            report.problems.append(".githooks/pre-commit 缺失")

        cfg = _git(worktree, "config", "--get", "core.hooksPath")
        hooks_path = cfg.stdout.strip() if cfg.returncode == 0 else ""
        if hooks_path:
            resolved = (worktree / hooks_path).resolve()
            # 判据是「git 实际会去找钩子的地方确实有 pre-commit」：
            # worktree 的 checkout 里带 .githooks 时，解析到 worktree 自己那份也算生效。
            if resolved.is_dir() and (resolved / "pre-commit").is_file():
                report.checks.append(f"core.hooksPath={hooks_path} 解析到 {resolved}，钩子在位")
                # 实际被执行的可能是 worktree 自己 checkout 出来的那一份，
                # 它的可执行位同样决定钩子是否真的会跑（TASK-12）。
                target = resolved / "pre-commit"
                if target.resolve() != pre_commit.resolve():
                    check_ok, check_problem = hook_executable_check(target)
                    if check_ok:
                        report.checks.append(check_ok)
                    else:
                        report.ok = False
                        report.problems.append(check_problem)
            else:
                report.ok = False
                report.problems.append(
                    f"core.hooksPath={hooks_path} 解析到 {resolved}，那里没有 pre-commit"
                    "（worktree 相对路径坑：钩子会静默不执行）"
                )
        else:
            report.ok = False
            report.problems.append(
                "core.hooksPath 未设置（先跑 node ops/deploy/setup-git-hooks.mjs）"
            )

        # 有 node 与校验脚本时，再跑一遍官方 --check 作为最终裁决
        setup = self.repo_root / "ops" / "deploy" / "setup-git-hooks.mjs"
        if shutil.which("node") and setup.is_file():
            proc = subprocess.run(
                ["node", str(setup), "--check"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode == 0:
                report.checks.append("setup-git-hooks.mjs --check 通过")
            else:
                report.ok = False
                report.problems.append(
                    "setup-git-hooks.mjs --check 未通过："
                    + (proc.stdout + proc.stderr).strip()[:300]
                )
        else:
            report.checks.append("无 node 或校验脚本，跳过 --check（已用上面的静态检查代替）")
        return report
