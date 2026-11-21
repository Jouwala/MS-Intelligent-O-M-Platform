"""模块加载引导（TASK-03）。

仓库按 Issue #18 的改动文件清单把 Worker 放在 apps/agent-worker/、
适配器放在 apps/control-plane/backend/adapters/——两个目录名都带连字符，
不是合法 Python 包名，无法用包导入。因此这里用文件路径显式加载，
模块间只通过这里注册的名字互相引用，不写 sys.path  hack。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]


def _load(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"无法加载模块 {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_adapter() -> ModuleType:
    return _load(
        "github_issue",
        _REPO_ROOT / "apps" / "control-plane" / "backend" / "adapters" / "github_issue.py",
    )


def load_sibling(name: str) -> ModuleType:
    return _load(f"agent_worker_{name}", _HERE / f"{name}.py")


_BACKEND = _REPO_ROOT / "apps" / "control-plane" / "backend"


def load_scheduler() -> ModuleType:
    """加载中央调度器包 `apps/control-plane/backend/scheduler/`。

    这里把 `apps/control-plane/backend` 放进 sys.path 后按标准包导入，是因为
    `scheduler` 目录自身是合法包名，只有上级 `control-plane` 带连字符无法走包路径；
    包内 `from .api import ...` 的相对导入必须依赖包的 submodule_search_locations，
    所以不能用上面的单文件加载方式。整个仓库只有这一处 sys.path 入口。
    """
    name = "scheduler"
    if name in sys.modules:
        return sys.modules[name]
    backend = str(_BACKEND)
    if backend not in sys.path:
        sys.path.insert(0, backend)
    pkg_dir = _BACKEND / "scheduler"
    spec = importlib.util.spec_from_file_location(
        name, pkg_dir / "__init__.py", submodule_search_locations=[str(pkg_dir)]
    )
    assert spec is not None and spec.loader is not None, f"无法加载调度器包 {pkg_dir}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def repo_root() -> Path:
    return _REPO_ROOT
