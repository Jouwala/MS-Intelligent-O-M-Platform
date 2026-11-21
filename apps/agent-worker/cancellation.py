"""取消信号轮询（TASK-10）。

旧实现没有取消通道：任务被人工取消后，正在执行的子进程会一直跑到超时，
而 Worker 退出后还会照常回传「完成」。

本模块用一个**独立的轮询线程**读调度器状态（默认 5s 一次，可配）：

- 命中 `scheduler.is_cancel_requested(task_id)` → 置 `threading.Event`，
  执行器在轮询循环里看到事件后终止整个进程组（见 executor.py）；
- 轮询线程与心跳线程职责分离：心跳回答「Worker 活着」，取消线程回答
  「这单活该停」，两者生命周期也不同（取消线程随单个任务存亡）；
- 调度器读失败（数据库锁、网络镜像等）只记日志继续轮询，不把「读不到」
  误判成「没有取消」——最终以执行结束后的 `is_cancel_requested` 复核兜底。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

DEFAULT_CANCEL_POLL_INTERVAL_S = 5.0


class CancelWatcher:
    """轮询调度器取消标志的后台线程。"""

    def __init__(
        self,
        scheduler,
        task_id: str,
        *,
        interval_s: float = DEFAULT_CANCEL_POLL_INTERVAL_S,
        log: Callable[[str], None] = print,
    ) -> None:
        self.scheduler = scheduler
        self.task_id = task_id
        self.interval_s = max(0.1, float(interval_s))
        self.log = log
        self.event = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 状态 ---------------------------------------------------------------
    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def trigger(self) -> None:
        self.event.set()

    # -- 轮询 ---------------------------------------------------------------
    def poll_once(self) -> bool:
        """同步查一次；返回是否已请求取消。异常只记日志，不中断轮询。"""
        try:
            requested = bool(self.scheduler.is_cancel_requested(self.task_id))
        except Exception as exc:  # noqa: BLE001 - 调度器读失败不得杀死轮询线程
            self.log(f"[cancel] 读取取消标志失败（下轮重试）：{exc}")
            return False
        if requested:
            self.event.set()
        return requested

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.event.is_set():
                return
            self.poll_once()
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        if self.running or self.event.is_set():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"cancel-{self.task_id}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout_s)

    def __enter__(self) -> "CancelWatcher":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
