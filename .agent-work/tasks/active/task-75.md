# 任务 #75：生产前置：启用 Reaper cron + 失败告警

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/75
- 分支：`agent/task-75-reaper-cron`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

切生产前置（最终验收报告 §五）。Reaper 自动 cron（.github/workflows/lease-reaper.yml，每 5 分钟）尚未在生产启用，本轮只手动 dispatch 成功（run 43660614194）。

做：
1. 确认 lease-reaper.yml 的 cron 已启用并在生产跑起来（查最近一次定时运行）。
2. 配置失败告警（Reaper 失败时通知 owner）。
3. 验证一次过期租约自动回收端到端。

执行：三方 agent（MSNOC1）。验收：cron 定时运行记录 + 一次自动回收成功 + 失败告警可达。

## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [x] 按 Issue 验收标准实现
- [x] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
