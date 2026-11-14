"""主机信息与能力标签（TASK-11）。

旧实现有两个只在 Windows 上成立的假设：

- 能力标签硬编码 `("python", "windows-worker")`：Worker 迁到 Linux 后仍自报
  windows-worker，调度器会把需要 POSIX 能力（进程组、可执行位）的 Linux
  任务派过来，或者反过来把 Linux Worker 派去跑 Windows 任务；
- 主机名只读 `COMPUTERNAME`：Linux 上没有这个变量，注册出来的主机名是空串，
  多实例排障时无法区分机器。

本模块按 `sys.platform` 生成能力标签（`windows-worker` / `linux-worker`），
主机名优先 `HOSTNAME` 环境变量、再退回 `socket.gethostname()`（两者都失败时
返回空串而不是抛异常——注册不能因为取主机名失败而整体失败）。
"""

from __future__ import annotations

import os
import socket
import sys
from typing import Callable, Optional


def platform_kind() -> str:
    """能力标签里的平台段：windows / linux / darwin（其他平台用 sys.platform 原值）。"""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("darwin"):
        return "darwin"
    return sys.platform


def platform_label() -> str:
    """注册到调度器的平台标签，与能力标签一致：`windows-worker` / `linux-worker`。"""
    return f"{platform_kind()}-worker"


def capabilities() -> tuple[str, ...]:
    """Worker 能力标签：python + 平台 Worker 标签（可被配置覆盖前缀部分）。"""
    return ("python", platform_label())


def hostname() -> str:
    """主机名：HOSTNAME 环境变量优先（容器/CI 常见），否则 socket.gethostname()。"""
    for getter in (
        lambda: os.environ.get("HOSTNAME", "").strip(),
        lambda: (socket.gethostname() or "").strip(),
    ):
        try:
            value = getter()
        except OSError:
            value = ""
        if value:
            return value
    return ""


def collect(
    *,
    hostname_fn: Optional[Callable[[], str]] = None,
    platform_fn: Optional[Callable[[], str]] = None,
) -> dict:
    """一次性收集注册三元组（hostname/platform/capabilities），便于测试注入。"""
    host = (hostname_fn or hostname)()
    label = (platform_fn or platform_label)()
    caps = ("python", label) if label else ("python",)
    return {"hostname": host, "platform": label, "capabilities": caps}
