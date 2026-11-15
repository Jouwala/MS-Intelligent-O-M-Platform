"""pytest 夹具（不是 conftest：见下）。

为什么不用 `tests/conftest.py`：仓库里多个测试目录都有 conftest.py，pytest 在
**同一进程联合运行多个目录**（Windows CI 的 agent-tests 就是一条命令跑几个目录）
时会以模块名 `conftest` 缓存第一个加载的文件；本包目录名带连字符不能做成包，
又无法保证收集顺序，因此干脆不提供 conftest.py——夹具放本模块，测试文件用
`from _linux_fixtures import ...` 显式导入（pytest 会从测试模块命名空间收集
fixture）。这样与其它目录的 conftest 完全不冲突。
"""

from __future__ import annotations

import pytest

import _linux_testkit as tk
import linux_worker_core


@pytest.fixture(scope="session")
def runtime():
    """共享运行时（executor/command_parse/heartbeat/cancellation/scheduler/adapter）。"""
    return tk.runtime_loader.load_runtime(tk.REPO_ROOT)


@pytest.fixture()
def fake_github():
    """本地 fake GitHub HTTP server（真实 TCP，ref 唯一性由服务端裁决）。"""
    server = tk.FakeGitHubServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def make_config(fake_github, tmp_path):
    def build(worker_id="muse-1-linux", instance=None, **overrides):
        return tk.load_worker_config(fake_github, tmp_path, worker_id, instance, **overrides)

    return build


@pytest.fixture()
def make_worker(runtime, make_config):
    def build(worker_id="muse-1-linux", instance=None, *, log=None, **overrides):
        config = make_config(worker_id=worker_id, instance=instance, **overrides)
        return linux_worker_core.LinuxWorker(
            runtime, config, log=log or (lambda _message: None)
        )

    return build
