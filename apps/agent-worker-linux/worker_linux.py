#!/usr/bin/env python3
"""Linux 沙箱 Agent Worker 启动器（接入 GitHub ref-CAS 权威层）。

用法（每台 VM 一份配置，`instance` 必须各不相同）：

    python3 worker_linux.py --config ~/agent-worker-linux/config.json          # 常驻
    python3 worker_linux.py --config ... --once                              # 只跑一个周期
    python3 worker_linux.py --config ... --check                             # 配置 + refs 连通性
    python3 worker_linux.py --config ... --claim-probe --issue 73            # 认领探针
    python3 worker_linux.py --config ... --claim-probe --issue 73 --result-file /tmp/r.json

设计：本文件只做「CLI + 日志 + 进程信号」，任务编排在 `linux_worker_core.py`，
权威层/执行器/心跳等全部复用仓库既有实现（见 `runtime_loader.py`）。

输出约定：
- `--check` / `--claim-probe` 的机器可读结果走 **stdout 单行 JSON**；
- 人类日志一律走 **stderr**（可同时追加 `log_file`），token 永不出现在日志里；
- 退出码：0=正常/业务结果已产出，1=检查失败或运行失败，2=配置/加载错误。
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import linux_config  # noqa: E402
import linux_worker_core  # noqa: E402
import runtime_loader  # noqa: E402

DEFAULT_CONFIG = "config.json"


def _configure_utf8_streams() -> None:
    """管道/重定向下强制 UTF-8：Windows 控制台默认 cp1252 会把中文日志/JSON 打崩。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # 老 Python/特殊流
            pass


class _Logger:
    """人类日志 → stderr（可追加到 log_file）；不做任何 token 展开。"""

    def __init__(self, log_file: Optional[Path]) -> None:
        self._file = None
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(log_file, "a", encoding="utf-8")

    def __call__(self, message: str) -> None:
        line = (
            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {message}"
        )
        print(line, file=sys.stderr, flush=True)
        if self._file is not None:
            self._file.write(line + "\n")
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Linux 沙箱 Agent Worker（GitHub ref-CAS 权威层）"
    )
    parser.add_argument(
        "--config",
        default=str(Path(DEFAULT_CONFIG)),
        help="JSON 配置文件路径（默认 ./config.json）",
    )
    parser.add_argument("--once", action="store_true", help="只跑一个轮询周期后退出")
    parser.add_argument("--check", action="store_true", help="校验配置与 refs API 连通性")
    parser.add_argument(
        "--claim-probe", action="store_true", help="只做一次 upsert+claim 并输出单行 JSON"
    )
    parser.add_argument("--issue", type=int, default=None, help="--claim-probe 的 Issue 编号")
    parser.add_argument(
        "--result-file", default=None, help="--claim-probe 时把同一份 JSON 写入该文件"
    )
    parser.add_argument(
        "--release", action="store_true", help="--claim-probe 成功认领后立即释放租约"
    )
    return parser


def _install_stop_handlers() -> threading.Event:
    stop = threading.Event()

    def _handler(signum, _frame) -> None:
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, AttributeError):  # 非主线程/受限环境
            continue
    return stop


def main(argv: Optional[list[str]] = None) -> int:
    _configure_utf8_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.claim_probe and args.issue is None:
        parser.error("--claim-probe 需要同时给出 --issue N")
    if args.issue is not None and not args.claim_probe:
        parser.error("--issue 只能与 --claim-probe 一起使用")

    try:
        config = linux_config.load_config(args.config)
    except linux_config.ConfigError as exc:
        print(f"[config-error] {exc}", file=sys.stderr)
        return 2

    logger = _Logger(config.log_file)
    worker = None
    try:
        try:
            runtime = runtime_loader.load_runtime(config.repo_root)
        except runtime_loader.RuntimeLoadError as exc:
            logger(f"共享运行时加载失败：{exc}")
            return 2
        worker = linux_worker_core.LinuxWorker(runtime, config, log=logger)
        logger(
            f"repo={config.repo} api_base={config.api_base} auth_mode={config.auth_mode}"
        )

        if args.check:
            payload = worker.check()
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            if not payload["ok"]:
                logger(f"--check 未通过：{payload['error']}")
            return 0 if payload["ok"] else 1

        if args.claim_probe:
            payload = worker.probe_claim(int(args.issue), release=args.release)
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            print(line)
            if args.result_file:
                result_path = Path(args.result_file).expanduser()
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(line + "\n", encoding="utf-8")
            logger(
                f"claim-probe issue={args.issue} result={payload['result']} "
                f"token={payload['token']}"
            )
            return 0 if payload["result"] in ("claimed", "released", "LeaseHeld", "AlreadyTerminal") else 1

        if args.once:
            worker.register()
            worker.start_heartbeat()
            try:
                result = worker.run_cycle()
            finally:
                worker.stop_heartbeat()
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result.get("outcome") not in ("error", "discover-error") else 1

        worker.run_forever(stop_event=_install_stop_handlers())
        return 0
    finally:
        if worker is not None:
            worker.close()
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
