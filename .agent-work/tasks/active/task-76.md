# 任务 #76：生产前置：refs 生命周期与配额监控

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/76
- 分支：`agent/task-76-refs`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

切生产前置（最终验收报告 §五、§六）。HWM/lease/reap/dispatch refs 会随认领/续租累积（HWM 每次续租新增），matching-refs 不分页；目前无生命周期清理与配额监控。

做：
1. 实现 refs 数量/增长监控（定期统计 agent-* refs 数量，超阈值告警）。
2. 给出清理策略（哪些 ref 可删、保留期、手动/自动清理入口）。
3. 文档写入运维手册。

执行：三方 agent（MSNOC1）。验收：监控能报出当前 refs 数量，清理策略有文档。

## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
