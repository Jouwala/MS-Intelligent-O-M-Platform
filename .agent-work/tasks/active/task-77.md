# 任务 #77：W7：Worker 根进程退出后孤儿子进程回收

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/77
- 分支：`agent/task-77-w7-worker`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

已知风险（最终验收报告 §六，非阻断）。Worker 根进程自行退出后遗留的子进程，看门狗重启路径不会清理（watchdog 只管理自己拉起的 Worker 进程树）。Muse 2 实测。

做：Worker 启动时按 workers 表/心跳文件里的 pid 做孤儿回收，或由独立 Reaper 定时清理。

执行：三方 agent（MSNOC1）。验收：模拟 Worker 根进程异常退出后，孤儿进程被回收，无泄漏。

## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
