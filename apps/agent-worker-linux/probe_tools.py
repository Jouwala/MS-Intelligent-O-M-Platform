"""连通性检查（`--check`）与认领探针（`--claim-probe`）。

从 `linux_worker_core.py` 拆出来的原因：核心编排保持单一职责，单文件控制在
500 行内；这两个工具都是**只读/单次探针**语义，不参与常驻循环：

- `check(worker)`：GET `refs/heads/main`，验证 repo/api_base/token 这一整条
  网络与鉴权链路（不注册、不认领、不改任何状态）；
- `probe_claim(worker, issue, release=False)`：一次 upsert + claim，输出单行 JSON
  所需字段（`hwm_refs`/`lease_refs` 直接从 refs 读，供 Muse 在 VM 上做并发验收）。
  **默认不释放**，让 GitHub 上能看到 HWM/lease ref；`release=True` 仅供工具演示。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

#: `--claim-probe` 单行 JSON 的稳定字段（验收脚本按它解析）。
PROBE_FIELDS = (
    "ok",
    "result",
    "worker_id",
    "instance",
    "attempt",
    "token",
    "hwm_refs",
    "lease_refs",
    "refs_error",
)


def collect_refs(scheduler, S, issue_number: int) -> dict[str, Any]:
    """读该 Issue 的 HWM/lease ref 名（失败写进 refs_error，不抛）。"""
    refs: dict[str, Any] = {"hwm_refs": [], "lease_refs": [], "refs_error": ""}
    try:
        prefix = S.ref_api
        hwm = scheduler.ref_store.list(prefix.hwm_prefix(int(issue_number))) or []
        lease = scheduler.ref_store.list(prefix.lease_prefix(int(issue_number))) or []
        refs["hwm_refs"] = sorted(str(getattr(item, "ref", "")) for item in hwm)
        refs["lease_refs"] = sorted(str(getattr(item, "ref", "")) for item in lease)
    except Exception as exc:  # noqa: BLE001 - refs 读取失败用字段回报
        refs["refs_error"] = f"{type(exc).__name__}: {exc}"
    return refs


def check(worker: Any) -> dict[str, Any]:
    """配置校验后的网络连通性检查（GET refs/heads/main），返回可打印 JSON。"""
    config = worker.config
    payload: dict[str, Any] = {
        "ok": False,
        "identity": config.public_identity(),
        "hostname": worker.hostname,
        "platform": worker.platform,
        "capabilities": list(worker.capabilities),
        "auth_mode": config.auth_mode,
        "refs_ok": False,
        "main_sha": "",
        "error": "",
    }
    try:
        info = worker.scheduler.ref_store.get("refs/heads/main")
    except Exception as exc:  # noqa: BLE001 - 检查结果用字段表达，不抛栈
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload
    if info is None:
        payload["error"] = "refs/heads/main 不存在（repo 或 api_base 是否正确？）"
        return payload
    payload["refs_ok"] = True
    payload["main_sha"] = str(getattr(info, "sha", "") or "")
    payload["ok"] = True
    return payload


def probe_claim(worker: Any, issue_number: int, *, release: bool = False) -> dict[str, Any]:
    """单次 upsert+claim（验收探针）；默认保留租约/refs 供人工检查。"""
    config = worker.config
    S = worker.S
    task_id = f"issue-{int(issue_number)}"
    payload: dict[str, Any] = {
        "ok": False,
        "result": "error",
        "worker_id": config.worker_id,
        "instance": config.instance,
        "ref_instance": config.ref_instance,
        "ref_agent_id": config.ref_agent_id,
        "attempt": "",
        "token": None,
        "hwm_refs": [],
        "lease_refs": [],
        "refs_error": "",
        "issue": int(issue_number),
        "config_path": str(config.config_path) if config.config_path else "",
        "released": False,
        "error": "",
    }
    try:
        issue = worker.transport.get_issue(int(issue_number))
        scope = tuple(
            worker.parse_file_scope(getattr(issue, "body", "") or "") or ()
        )
        worker.scheduler.upsert_task(
            task_id,
            int(issue_number),
            repo=config.repo,
            title=str(getattr(issue, "title", "") or ""),
            scopes=scope,
        )
        attempt = uuid.uuid4().hex[:16]
        payload["attempt"] = attempt
        try:
            lease = worker.scheduler.claim(
                task_id,
                agent_id=config.ref_agent_id,
                instance_id=config.ref_instance,
                attempt_id=attempt,
                lease_ttl=timedelta(seconds=config.lease_ttl_seconds),
            )
        except S.LeaseHeld as exc:
            payload.update(result="LeaseHeld", error=str(exc))
            return payload
        except S.AlreadyTerminal as exc:
            payload.update(result="AlreadyTerminal", error=str(exc))
            return payload
        payload.update(
            ok=True,
            result="claimed",
            attempt=lease.attempt_id,
            token=lease.fencing_token,
        )
        if release:
            worker.scheduler.release(
                task_id,
                attempt_id=lease.attempt_id,
                fencing_token=lease.fencing_token,
            )
            payload["released"] = True
            payload["result"] = "released"
        return payload
    except Exception as exc:  # noqa: BLE001 - 探针要把失败写进 JSON 而不是抛栈
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload
    finally:
        payload.update(collect_refs(worker.scheduler, S, int(issue_number)))
