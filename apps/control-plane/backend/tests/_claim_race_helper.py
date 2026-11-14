"""多进程认领竞争 helper（测试专用；文件名以 `_` 开头，pytest 不收集）。

测试父进程用两个真实子进程同时 `claim` 同一个任务，验证 SQLite
`BEGIN IMMEDIATE` + 唯一约束下「恰好一个成功」。

用法::

    python _claim_race_helper.py <db> <task_id> <agent> <instance> <attempt> <start_epoch>

子进程等到 `start_epoch`（墙钟）后发起认领，把结果打成一行 JSON 到 stdout。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from scheduler import SchedulerService  # noqa: E402


def main() -> int:
    db, task_id, agent, instance, attempt, start_raw = sys.argv[1:7]
    start_at = float(start_raw)
    # 忙等而不是 sleep(粗粒度)：让两个进程尽量在同一毫秒进入 claim
    while time.time() < start_at:
        time.sleep(0.001)

    def clock() -> datetime:
        return datetime.fromtimestamp(max(time.time(), start_at), tz=timezone.utc)

    service = SchedulerService(db, clock=clock)
    try:
        lease = service.claim(
            task_id, agent_id=agent, instance_id=instance, attempt_id=attempt
        )
        result = {"ok": True, "instance": instance, "token": lease.fencing_token}
    except Exception as exc:  # noqa: BLE001 - 父进程按类型名断言
        result = {"ok": False, "instance": instance, "error": type(exc).__name__}
    finally:
        service.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
