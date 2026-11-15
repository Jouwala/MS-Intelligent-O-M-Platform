"""从 repo_root 加载仓库内共享运行时（**不复制** Worker/调度器实现）。

本包（`apps/agent-worker-linux/`）只属于「Linux 沙箱部署壳」，真正的执行器、
命令解析、心跳、取消轮询、调度器实现全部复用仓库：

- `apps/agent-worker/_bootstrap.py`：按文件路径加载兄弟模块（目录名带连字符，
  不是合法 Python 包名）；
- `apps/agent-worker/{executor,command_parse,heartbeat,cancellation,hostinfo}.py`；
- `apps/control-plane/backend/adapters/github_issue.py`：REST 传输 + 范围解析；
- `apps/control-plane/backend/scheduler/`：GitHub ref-CAS 权威层
  （`make_scheduler` → `GitHubRefScheduler`）。

为什么用 importlib 按路径加载，而不是往 sys.path 里塞目录名：`apps/agent-worker`
和 `apps/control-plane` 带连字符，不是合法包名；仓库既有的 `_bootstrap.py` 已经
把这件事做对了，这里直接复用它，避免出现第二套加载规则。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

#: 共享运行时版本探针：repo_root 必须同时具有这两个文件才算「完整仓库 clone」。
REQUIRED_FILES = (
    "apps/agent-worker/_bootstrap.py",
    "apps/control-plane/backend/scheduler/factory.py",
)


class RuntimeLoadError(RuntimeError):
    """repo_root 不可用 / 共享模块加载失败。"""


def _load_module(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeLoadError(f"无法加载模块 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 统一包装成可读错误
        sys.modules.pop(name, None)
        raise RuntimeLoadError(f"加载共享模块失败 {path}：{exc}") from exc
    return module


def validate_repo_root(repo_root: Path) -> Path:
    """确认 repo_root 是一个包含权威层与共享 Worker 的完整仓库 clone。"""
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeLoadError(f"repo_root 不存在或不是目录：{root}")
    missing = [rel for rel in REQUIRED_FILES if not (root / rel).is_file()]
    if missing:
        raise RuntimeLoadError(
            f"repo_root={root} 不是完整仓库 clone，缺少：{', '.join(missing)}"
        )
    return root


def load_bootstrap(repo_root: Path) -> ModuleType:
    """加载 `apps/agent-worker/_bootstrap.py`（其内部按自身位置推导仓库根）。"""
    root = validate_repo_root(repo_root)
    return _load_module("agent_worker_bootstrap", root / "apps/agent-worker/_bootstrap.py")


def load_runtime(repo_root: Path) -> dict[str, Any]:
    """一次性加载本 Worker 需要的全部共享模块，返回名字 → 模块/类/函数。"""
    root = validate_repo_root(repo_root)
    bootstrap = load_bootstrap(root)
    adapter = bootstrap.load_adapter()
    runtime: dict[str, Any] = {
        "repo_root": root,
        "bootstrap": bootstrap,
        "adapter": adapter,
        "scheduler": bootstrap.load_scheduler(),
        "executor": bootstrap.load_sibling("executor"),
        "command_parse": bootstrap.load_sibling("command_parse"),
        "heartbeat": bootstrap.load_sibling("heartbeat"),
        "cancellation": bootstrap.load_sibling("cancellation"),
        "hostinfo": bootstrap.load_sibling("hostinfo"),
        "RestGitHubTransport": adapter.RestGitHubTransport,
        "parse_file_scope": adapter.parse_file_scope,
    }
    return runtime


def scheduler_factory(runtime: dict[str, Any]):
    """取 `make_scheduler`（`scheduler` 包顶层导出；老版本退回 factory 子模块）。"""
    scheduler = runtime.get("scheduler")
    factory = getattr(scheduler, "make_scheduler", None)
    if factory is not None:
        return factory
    fallback = getattr(getattr(scheduler, "factory", None), "make_scheduler", None)
    if fallback is None:  # pragma: no cover - 权威层缺失时不静默降级
        raise RuntimeLoadError(
            "共享调度器缺少 make_scheduler（apps/control-plane/backend/scheduler/factory.py）"
        )
    return fallback
