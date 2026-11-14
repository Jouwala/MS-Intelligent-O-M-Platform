"""GitHub Actions 自动分派 / 检查逻辑（TASK-01 + TASK-07）。

原先 `auto-dispatch.yml`、`agent-orchestrator.yml`、`dispatch-check.yml` 把
几百行 Python 内嵌在 YAML 的 heredoc 里，无法单测、无法复用，而且有三处
真实缺陷：

1. **check-then-act 重复分派**：先读评论判断“没分派过”，再 POST
   TASK_ASSIGNED。两个 workflow 并发运行时都能通过检查，于是同一个任务被
   分派两次（2026-11-14 TASK-07 报告）。
2. **只读第一页**：`per_page=100` 但不跟随 `Link: rel="next"`。评论超过
   100 条时，认领 / 分派记录读不到 → 重复分派；Issue 超过 100 条时，第
   2 页之后的就绪任务永远发现不了。
3. **失败静默**：查询异常经常被 `except` 兜成“没记录”，用不完整数据继续
   做决定。

本包把上述逻辑抽成可注入 GitHub 客户端的纯模块：

- `protocol.py`        —— Issue 正文 / 评论头解析（协议口径，纯函数）
- `github_client.py`   —— REST 客户端：Link 全量分页、去重、403/429/5xx
                          重试、显式抛错（不静默用半截数据）
- `dispatch_registry.py` —— 用 `POST /git/refs` 建 dispatch ref 做原子 CAS，
                          实现分派幂等（TASK-07）
- `auto_dispatch.py`   —— PR 合入后找就绪任务并分派（原 auto-dispatch.yml）
- `qa_dispatch.py`     —— QA 触发去重（原 agent-orchestrator.yml）
- `dispatch_check.py`  —— 必填字段 / 依赖阻塞检查（原 dispatch-check.yml）

workflow 侧只保留 `python apps/control-plane/backend/dispatch/xxx.py` 调用，
参数走环境变量。测试用内存假 API（`tests/fake_github.py`）跑真实并发与
分页 / 故障注入，不依赖 GitHub 网络。
"""

from __future__ import annotations

__all__ = [
    "protocol",
    "github_client",
    "dispatch_registry",
    "auto_dispatch",
    "qa_dispatch",
    "dispatch_check",
]
