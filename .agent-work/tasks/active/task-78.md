# 任务 #78：Worker 任务命令环境白名单（Secret 不可见）

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/78
- 分支：`agent/task-78-worker-secret`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

已知风险（最终验收报告 §六，非阻断）。任务命令继承 Worker 环境，Secret 与生产凭据对任务命令可见。

做：给 Worker 执行器加最小环境白名单，只传任务必需的变量，Secret/生产凭据默认不进子进程环境。

执行：三方 agent（MSNOC1）。验收：任务命令 env 里无 Secret/生产凭据，任务仍能正常跑。

## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
