"""真实 GitHub 多 checkout 验证的 worker 半（子进程侧；由 driver 拉起）。

用法（由 `real_github_ref_verify.py driver` 自动生成，一般不需要手工调用）::

    python real_github_ref_worker.py --op claim --role a --issue 12 --ttl 600
        [--attempt ...] [--token ...] [--key ...] [--scopes a.py,b/] [--start-at epoch]

在**本 checkout** 的代码上构造 `scheduler.factory.make_scheduler` +
`RestGitHubTransport`，对真实 GitHub API 执行一次操作，最后一行输出
`RESULT: {json}`（driver 解析）。令牌只从 `GITHUB_TOKEN` 环境变量读。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import uuid
from pathlib import Path

DEFAULT_OWNER = "Jouwala"
DEFAULT_NAME = "noc-soc-ref-verify"


def _load_adapter_module() -> object:
    backend = Path(__file__).resolve().parents[2]  # tests/manual -> tests -> backend
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    name = "github_issue"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, backend / "adapters" / "github_issue.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _context(args):
    from datetime import timedelta

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("缺少 GITHUB_TOKEN（只从环境变量读取）")
    backend = Path(__file__).resolve().parents[2]
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from scheduler.factory import make_scheduler  # noqa: PLC0415

    transport = _load_adapter_module().RestGitHubTransport(
        args.owner, args.name, token=token, timeout=60.0, max_retries=2, sleep=time.sleep)
    scheduler = make_scheduler(transport=transport, repo=f"{args.owner}/{args.name}",
                               lease_ttl=timedelta(seconds=float(args.ttl)))
    task_id = f"issue-{args.issue}"
    scheduler.upsert_task(task_id, args.issue, repo=f"{args.owner}/{args.name}", title="verify")
    return scheduler, task_id


def run_worker(args) -> int:
    if args.start_at:
        while time.time() < float(args.start_at):
            time.sleep(0.001)
    scheduler, task_id = _context(args)
    payload: dict[str, object] = {"op": args.op, "role": args.role, "ok": False, "error": ""}
    try:
        if args.op == "claim":
            lease = scheduler.claim(
                task_id, agent_id=f"verify-{args.role}",
                instance_id=uuid.uuid4().hex[:8], attempt_id=uuid.uuid4().hex[:12])
            payload.update(ok=True, token=lease.fencing_token, attempt=lease.attempt_id,
                           instance=lease.instance_id, agent=lease.agent_id)
        elif args.op == "renew":
            lease = scheduler.renew(task_id, attempt_id=args.attempt, fencing_token=int(args.token))
            payload.update(ok=True, token=lease.fencing_token)
        elif args.op == "release":
            task = scheduler.release(task_id, attempt_id=args.attempt,
                                     fencing_token=int(args.token))
            payload.update(ok=True, state=task.state)
        elif args.op == "complete":
            outcome = scheduler.complete(
                task_id, attempt_id=args.attempt, fencing_token=int(args.token),
                idempotency_key=args.key, summary=args.summary or "真实 GitHub 验证完成")
            payload.update(ok=True, created=outcome.created, status=outcome.terminal_status,
                           key=outcome.idempotency_key)
        elif args.op == "complete_flow":
            lease = scheduler.claim(
                task_id, agent_id=f"verify-{args.role}",
                instance_id=uuid.uuid4().hex[:8], attempt_id=uuid.uuid4().hex[:12])
            scheduler.transition(task_id, attempt_id=lease.attempt_id,
                                 fencing_token=lease.fencing_token, state="running")
            first = scheduler.complete(
                task_id, attempt_id=lease.attempt_id, fencing_token=lease.fencing_token,
                idempotency_key=args.key, summary="完成（第一次）")
            second = scheduler.complete(
                task_id, attempt_id=lease.attempt_id, fencing_token=lease.fencing_token,
                idempotency_key=args.key, summary="完成（重复提交）")
            payload.update(ok=True, token=lease.fencing_token, attempt=lease.attempt_id,
                           created=[first.created, second.created])
        elif args.op in ("claim_scope", "acquire_scope"):
            if args.op == "claim_scope":
                lease = scheduler.claim(
                    task_id, agent_id=f"verify-{args.role}",
                    instance_id=uuid.uuid4().hex[:8], attempt_id=uuid.uuid4().hex[:12])
                attempt_id, token = lease.attempt_id, lease.fencing_token
                payload.update(token=token, attempt=attempt_id)
            else:
                attempt_id, token = args.attempt, int(args.token)
            scheduler.acquire_scope(
                task_id, attempt_id=attempt_id, fencing_token=token,
                scopes=[part for part in (args.scopes or "").split(",") if part])
            payload.update(ok=True, scope_ok=True)
        elif args.op == "release_scope":
            released = scheduler.release_scope(
                task_id, attempt_id=args.attempt, fencing_token=int(args.token))
            payload.update(ok=True, released=bool(released))
        else:
            raise ValueError(f"未知 worker op：{args.op}")
    except Exception as exc:  # noqa: BLE001 - 结果按类型名回报，driver 断言
        payload.update(ok=False, error=type(exc).__name__)
    print("RESULT: " + json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="真实 GitHub ref 验证 worker（子进程侧）")
    parser.add_argument("--op", required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--owner", default=os.environ.get("VERIFY_OWNER", DEFAULT_OWNER))
    parser.add_argument("--name", default=os.environ.get("VERIFY_NAME", DEFAULT_NAME))
    for name, default in (("--role", "a"), ("--ttl", "600"), ("--attempt", ""),
                          ("--token", ""), ("--key", "verify-key"), ("--summary", ""),
                          ("--scopes", ""), ("--start-at", "")):
        parser.add_argument(name, default=default)
    return parser


if __name__ == "__main__":
    raise SystemExit(run_worker(build_parser().parse_args()))
