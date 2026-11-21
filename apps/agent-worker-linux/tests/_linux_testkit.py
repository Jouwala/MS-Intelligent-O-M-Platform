"""测试工具（不是 conftest）：路径/配置构造/fake GitHub 加载。

单独成模块的原因：仓库其它测试目录也有 `conftest.py`，pytest 在**同一进程**
联合运行多个目录时 `import conftest` 会串模块（Windows CI 的 agent-tests 就是
把多个目录放在一条命令里跑）。本包测试一律 `from _linux_testkit import ...`，
避免和 `apps/agent-worker/tests/conftest.py` 撞名。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = TESTS_DIR.parent
REPO_ROOT = PACKAGE_DIR.parents[1]  # repo/apps/agent-worker-linux → repo
BACKEND_TESTS = REPO_ROOT / "apps" / "control-plane" / "backend" / "tests"

for _path in (str(PACKAGE_DIR), str(BACKEND_TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import linux_config  # noqa: E402
import linux_worker_core  # noqa: E402
import runtime_loader  # noqa: E402


def _load_fake_github():
    name = "fake_github_http"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, BACKEND_TESTS / "fake_github_http.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FakeGitHubServer = _load_fake_github().FakeGitHubServer


def make_body(command=None, scope=()) -> str:
    """按 agent-protocol 的 Issue 正文格式拼「改动文件」/「执行命令」两节。"""
    lines = ["## 目标", "测试任务。"]
    if scope:
        lines.extend(["", "## 改动文件"])
        lines.extend(f"- `{path}`" for path in scope)
    if command is not None:
        lines.extend(["", "## 执行命令", "```json", json.dumps(list(command)), "```"])
    return "\n".join(lines) + "\n"


def config_mapping(server, worker_id="muse-1-linux", instance=None, tmp_dir=None, **overrides):
    """一份指向 fake server 的合法配置字典（测试用，token 显式给出）。"""
    base_dir = Path(tmp_dir) if tmp_dir is not None else Path(".")
    raw = {
        "worker_id": worker_id,
        "instance": instance or worker_id,
        "repo_root": str(REPO_ROOT),
        "repo": server.repo_full,
        "api_base": server.base_url,
        "token": "test-token",
        "state_dir": str(base_dir / f"state-{worker_id}"),
        "workdir": str(base_dir / f"work-{worker_id}"),
        "poll_seconds": 1,
        "lease_ttl_seconds": 600,
        "renew_interval_seconds": 150,
        "executor_timeout_seconds": 60,
    }
    raw.update(overrides)
    return raw


def write_config(path: Path, raw: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_worker_config(server, tmp_dir, worker_id="muse-1-linux", instance=None, **overrides):
    raw = config_mapping(server, worker_id, instance, tmp_dir, **overrides)
    return linux_config.from_mapping(raw)
