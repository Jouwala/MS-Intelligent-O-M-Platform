"""双进程认领竞争 helper（测试专用；文件名 `_` 开头，pytest 不收集）。

父进程启动本地 fake GitHub HTTP server，两个真实子进程各自用完整 HTTP 客户端
（RestRefStore + RestGitHubTransport）连同一个 server，验证跨进程 CAS 只有 1 个赢家。

用法::

    python _ref_race_helper.py <base_url> <task_id> <agent> <instance> <attempt> <start_epoch>

子进程等到墙钟 `start_epoch` 后认领，把结果打成一行 JSON 到 stdout。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
BACKEND = TESTS_DIR.parent
for _path in (str(TESTS_DIR), str(BACKEND)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from scheduler.github_refs import RestRefStore  # noqa: E402
from scheduler.ref_claim import RefClaimer  # noqa: E402


def _load_adapter_module():
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


def main() -> int:
    base_url, task_id, agent, instance, attempt, start_raw = sys.argv[1:7]
    start_at = float(start_raw)
    while time.time() < start_at:  # 忙等：让两个进程尽量同一毫秒进入 claim
        time.sleep(0.001)

    gi = _load_adapter_module()
    ref_store = RestRefStore(
        "o/r", token="test-token", api_base=base_url, max_retries=1, sleep=lambda _s: None
    )
    transport = gi.RestGitHubTransport(
        "o",
        "r",
        token="test-token",
        api_base=base_url,
        max_retries=1,
        sleep=lambda _s: None,
    )

    def clock() -> datetime:
        return datetime.now(timezone.utc)

    authority = RefClaimer(ref_store, transport, repo="o/r", clock=clock)
    try:
        lease = authority.claim(
            task_id, agent_id=agent, instance_id=instance, attempt_id=attempt
        )
        result = {"ok": True, "instance": instance, "token": lease.fencing_token}
    except Exception as exc:  # noqa: BLE001 - 父进程按类型名断言
        result = {"ok": False, "instance": instance, "error": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
