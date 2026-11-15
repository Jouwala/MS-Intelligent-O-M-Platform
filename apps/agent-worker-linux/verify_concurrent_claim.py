#!/usr/bin/env python3
"""并发认领验收工具（Muse 在 VM 上使用）。

用法：

    # 两台 VM（或同一 VM 两份配置）同时认领同一 Issue：
    python3 verify_concurrent_claim.py --issue 73 \\
        --config-a ~/agent-worker-linux/config.json \\
        --config-b ~/agent-worker-linux/config.json   # 若同机验证，instance 必须不同

    # 验收后清理赢家（GitHub 上可见的 HWM/lease ref 由权威层自动处理）：
    python3 verify_concurrent_claim.py --cleanup-winner /tmp/claim-winner.json

判定标准（Issue #73 验收第 1 条）：两个进程**恰好一个** `claimed`、另一个
`LeaseHeld`；输赢双方 JSON 里的 `hwm_refs` / `lease_refs` 都能在 GitHub 上看到；
任何其它组合（双赢/双输/error）都以非零码报失败。默认**不清理**赢家租约，
方便人工去 GitHub 检查 `agent-hwm/issue-N/...` 与 `agent-lease/issue-N/...`。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import linux_config  # noqa: E402
import linux_worker_core  # noqa: E402
import runtime_loader  # noqa: E402

HERE = Path(__file__).resolve().parent
WORKER = HERE / "worker_linux.py"
REQUIRED_FIELDS = (
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


def _configure_utf8_streams() -> None:
    """管道/重定向时强制 UTF-8（Windows 默认 cp1252 会打崩中文输出）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _load_json_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"结果文件不是 JSON 对象：{path}")
    return payload


def _describe(name: str, payload: dict[str, Any]) -> None:
    print(
        f"[{name}] worker_id={payload.get('worker_id')} "
        f"instance={payload.get('instance')} result={payload.get('result')} "
        f"token={payload.get('token')}"
    )
    for item in payload.get("hwm_refs") or []:
        print(f"    HWM   {item}")
    for item in payload.get("lease_refs") or []:
        print(f"    LEASE {item}")
    if payload.get("refs_error"):
        print(f"    refs_error={payload['refs_error']}")


def cleanup_winner(winner_file: Path) -> int:
    """读取 winner JSON 并释放其租约（验收结束后的收尾）。"""
    try:
        payload = _load_json_file(winner_file)
    except (OSError, ValueError) as exc:
        print(f"[cleanup] 读取 winner JSON 失败：{exc}", file=sys.stderr)
        return 2
    config_path = payload.get("config_path") or ""
    attempt = payload.get("attempt") or ""
    token = payload.get("token")
    issue = payload.get("issue")
    if not config_path or not attempt or token is None or issue is None:
        print(
            "[cleanup] winner JSON 缺少 config_path/attempt/token/issue，"
            "无法自动释放；请手工按需清理",
            file=sys.stderr,
        )
        return 2
    try:
        config = linux_config.load_config(config_path)
        runtime = runtime_loader.load_runtime(config.repo_root)
        worker = linux_worker_core.LinuxWorker(runtime, config)
        worker.scheduler.release(
            f"issue-{int(issue)}",
            attempt_id=str(attempt),
            fencing_token=int(token),
        )
    except Exception as exc:  # noqa: BLE001 - 验收工具给出可读失败
        print(f"[cleanup] 释放失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"[cleanup] 已释放 issue-{issue} attempt={attempt} token={token}")
    return 0


def verify(
    issue: int, config_a: Path, config_b: Path, timeout: float, winner_file: Path
) -> int:
    with tempfile.TemporaryDirectory(prefix="claim-verify-") as tmp:
        tmp_dir = Path(tmp)
        result_a = tmp_dir / "a.json"
        result_b = tmp_dir / "b.json"
        # 两个子进程并发启动：先起 A，再立刻起 B（认领由服务端 ref-CAS 裁决）
        print(f"[verify] 并发认领 issue-{issue}（{config_a} vs {config_b}）")
        payloads: dict[str, dict[str, Any]] = {}
        procs = []
        try:
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable, str(WORKER), "--config", str(config_a),
                        "--claim-probe", "--issue", str(issue),
                        "--result-file", str(result_a),
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    encoding="utf-8", errors="replace",
                    cwd=str(HERE),
                )
            )
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable, str(WORKER), "--config", str(config_b),
                        "--claim-probe", "--issue", str(issue),
                        "--result-file", str(result_b),
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    encoding="utf-8", errors="replace",
                    cwd=str(HERE),
                )
            )
            for name, proc, result_file in (("A", procs[0], result_a), ("B", procs[1], result_b)):
                try:
                    proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    print(f"[verify] 进程 {name} 超时（>{timeout}s）", file=sys.stderr)
                    return 2
                if not result_file.is_file():
                    print(
                        f"[verify] 进程 {name} 没有写结果文件（rc={proc.returncode}）："
                        + ((proc.stderr or "").strip()[-800:]),
                        file=sys.stderr,
                    )
                    return 2
                payloads[name] = _load_json_file(result_file)
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()

        for name in ("A", "B"):
            payload = payloads[name]
            missing = [field for field in REQUIRED_FIELDS if field not in payload]
            if missing:
                print(f"[verify] 进程 {name} JSON 缺字段：{missing}", file=sys.stderr)
                return 2
            _describe(name, payload)

        claimed = [payload for payload in payloads.values() if payload.get("result") == "claimed"]
        held = [payload for payload in payloads.values() if payload.get("result") == "LeaseHeld"]
        problems: list[str] = []
        if len(claimed) != 1:
            problems.append(f"期望恰好 1 个 claimed，实际 {len(claimed)}")
        if len(held) != 1:
            problems.append(f"期望恰好 1 个 LeaseHeld，实际 {len(held)}")
        if problems:
            print("[verify] FAIL：" + "；".join(problems), file=sys.stderr)
            print(
                "[verify] 若已有一个赢家，可用 --cleanup-winner <结果文件> 释放后重试",
                file=sys.stderr,
            )
            return 2

        winner = claimed[0]
        if not winner.get("hwm_refs") or not winner.get("lease_refs"):
            print("[verify] FAIL：赢家 JSON 里看不到 HWM/lease ref", file=sys.stderr)
            return 2
        winner_file.parent.mkdir(parents=True, exist_ok=True)
        winner_file.write_text(
            json.dumps(winner, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            f"[verify] PASS：恰好一赢一 LeaseHeld；赢家 "
            f"worker_id={winner.get('worker_id')} instance={winner.get('instance')} "
            f"token={winner.get('token')}"
        )
        print("[verify] GitHub 上应可见：")
        for item in winner.get("hwm_refs") or []:
            print(f"    {item}")
        for item in winner.get("lease_refs") or []:
            print(f"    {item}")
        print(f"[verify] winner JSON：{winner_file}")
        print(
            "[verify] 验收后清理：python3 verify_concurrent_claim.py "
            f"--cleanup-winner {winner_file}"
        )
        return 0


def main(argv: Optional[list[str]] = None) -> int:
    _configure_utf8_streams()
    parser = argparse.ArgumentParser(description="并发认领验收（GitHub ref-CAS）")
    parser.add_argument("--issue", type=int, default=None, help="要并发认领的 Issue 编号")
    parser.add_argument("--config-a", default=None, help="Worker A 的配置文件")
    parser.add_argument("--config-b", default=None, help="Worker B 的配置文件")
    parser.add_argument("--cleanup-winner", default=None, help="读取 winner JSON 并释放租约")
    parser.add_argument(
        "--winner-file",
        default="claim-winner.json",
        help="PASS 时把赢家 JSON 落盘到该路径（默认 ./claim-winner.json，供 --cleanup-winner 使用）",
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="单个探针超时秒数")
    args = parser.parse_args(argv)

    if args.cleanup_winner:
        return cleanup_winner(Path(args.cleanup_winner).expanduser())
    if args.issue is None or not args.config_a or not args.config_b:
        parser.error("需要 --issue 与 --config-a/--config-b（或改用 --cleanup-winner）")
    return verify(
        int(args.issue),
        Path(args.config_a).expanduser(),
        Path(args.config_b).expanduser(),
        float(args.timeout),
        Path(args.winner_file).expanduser(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
