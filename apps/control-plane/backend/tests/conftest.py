"""控制面测试的公共夹具（apps/control-plane/backend/tests）。

- 把 `apps/control-plane/backend` 放进 sys.path，按标准包导入 `scheduler`；
- 通过 `sched_testkit` 提供假时钟 / 假 GitHub 传输 / 多进程竞争 helper；
- 本目录测试只碰临时 SQLite（tmp_path）与本地 fake GitHub HTTP server，
  **不访问真实 GitHub / 真实网络**。

ADR-7（GitHub 原生权威层）相关夹具：
- `fake_github`：本地 fake GitHub HTTP server（真实 TCP + 服务端裁决 ref 唯一性）；
- `ref_store` / `ref_transport`：指向同一 fake server 的 `RestRefStore` 与
  `RestGitHubTransport`（走真实 HTTP 代码路径，不 mock 逻辑）；
- `ref_clock` / `ref_scheduler`：Ref 后端的假时钟与 `GitHubRefScheduler`。
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


# ---------------------------------------------------------------------------
# ADR-7：GitHub 原生 ref 权威层夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_github():
    """本地 fake GitHub HTTP server（函数级：故障注入/状态不串测试）。"""
    from fake_github_http import FakeGitHubServer

    with FakeGitHubServer() as server:
        yield server


@pytest.fixture()
def ref_clock() -> tk.FakeClock:
    return tk.FakeClock()


@pytest.fixture()
def ref_store(fake_github: "object") -> "object":
    from scheduler.github_refs import RestRefStore

    return RestRefStore(
        fake_github.repo_full,
        token="test-token",
        api_base=fake_github.base_url,
        max_retries=1,
        sleep=lambda _seconds: None,
    )


@pytest.fixture()
def ref_transport(fake_github: "object") -> "object":
    return gi.RestGitHubTransport(
        fake_github.owner,
        fake_github.repo,
        token="test-token",
        api_base=fake_github.base_url,
        max_retries=1,
        sleep=lambda _seconds: None,
    )


@pytest.fixture()
def ref_scheduler(ref_store: "object", ref_transport: "object", ref_clock: tk.FakeClock):
    from scheduler.ref_service import GitHubRefScheduler

    return GitHubRefScheduler(
        ref_store,
        ref_transport,
        repo="o/r",
        clock=ref_clock,
    )

