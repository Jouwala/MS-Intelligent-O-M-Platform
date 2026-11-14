"""控制面测试的公共夹具（apps/control-plane/backend/tests）。

- 把 `apps/control-plane/backend` 放进 sys.path，按标准包导入 `scheduler`；
- 通过 `sched_testkit` 提供假时钟 / 假 GitHub 传输 / 多进程竞争 helper；
- 本目录测试只碰临时 SQLite（tmp_path），**不访问真实 GitHub**。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
BACKEND = TESTS_DIR.parent
for _path in (str(TESTS_DIR), str(BACKEND)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from scheduler import SchedulerService  # noqa: E402
import sched_testkit as tk  # noqa: E402


def _load_adapter_module():
    """加载 `adapters/github_issue.py`（目录名带连字符，不能走包导入）。"""
    name = "github_issue"
    if name in sys.modules:
        return sys.modules[name]
    path = BACKEND / "adapters" / "github_issue.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gi = _load_adapter_module()


@pytest.fixture(scope="session")
def adapter_module():
    """`adapters/github_issue.py` 模块（RestGitHubTransport 等）。"""
    return gi


@pytest.fixture()
def clock() -> tk.FakeClock:
    return tk.FakeClock()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "scheduler.db"


@pytest.fixture()
def svc(db_path: Path, clock: tk.FakeClock):
    service = SchedulerService(db_path, clock=clock)
    try:
        yield service
    finally:
        service.close()
