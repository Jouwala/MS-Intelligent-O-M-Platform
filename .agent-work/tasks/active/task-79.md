# 任务 #79：生产前置：真实两台物理 VM 联调

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/79
- 分支：`agent/task-79-vm`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

切生产前置（最终验收报告 §五）。当前只做了两个独立 checkout + 真实 API（28/28），未做真实两台物理 VM 联调。

做：两台物理 VM 各跑一个 Worker，并发认领/执行任务，验证跨机互斥、fencing、租约回收、范围锁端到端。

执行：Muse 2、Muse 3（各一台 VM）+ 三方 agent（MSNOC1 协调）。验收：联调报告入库，跨机互斥无重复执行。

## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
