# 任务 #48：TASK-06-A：QA 结果回写 Issue + 过期冒烟结果不得触发合并

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/48
- 分支：`agent/task-48-qa-writeback-gate`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

﻿## 任务说明

TASK-06 子任务 A：让**冒烟结果回写到关联 Issue**，并**堵住「过期测试结果触发合并」这个洞**。两件事一起做，因为它们共用同一处「PR ↔ Issue 关联解析」逻辑。

本任务对应总纲 Issue #21 的缺口 1 与缺口 2。

### 缺口 1：QA 结果没回写到 Issue（协议第 6/7/8 类消息从未发出）

现状（已核实）：`ops/deploy/smoke-watcher.ps1` 冒烟结束后只做两件事 —— 往 **PR** 贴一份报告评论（`smoke-watcher.ps1:391`）、通过时只给 **PR** 打 `smoke-passed`（`:393-396`）。**关联 Issue 既收不到测试报告，也不会被打上 `state:qa-ready` / `state:qa-passed` / `state:qa-failed`。**

后果：
- `agent-protocol.md` 第二节定义��� 12 类消息里，第 6（QA_STARTED）、7（QA_FAILED）、8（QA_PASSED）三类**一次都没发过**；状态机图里 `state:in-review → state:qa-ready → state:qa-passed / state:qa-failed` 这条支路在实现里是断的。
- Worker 的轮询入口是 Issue（`agent-polling-setup.md` 第三节），开发 Agent 在 Issue 上看不到测试结论，只能自己点开 PR 看。
- `.github/labels.yml` 已定义 `state:qa-ready` / `state:qa-passed` / `state:qa-failed` 三个标签，但**没有任何代码或工作流会打它们**（已全仓库 grep 确认）。

本任务要做的：
1. 冒烟**开始前**给关联 Issue 打 `state:qa-ready`，并发一条 `QA_STARTED` 机器可读头评论（PR 号、head SHA、预计时长）。
2. 冒烟**失败**时给关联 Issue 发 `QA_FAILED` 结构化评论（PR 号、head SHA、失败项、错误信息、日志链接），并打 `state:qa-failed`。
3. 冒烟**通过**时给关联 Issue 发 `QA_PASSED` 评论（PR 号、head SHA、通过项），并打 `state:qa-passed`。
4. 关联 Issue 解析沿用既有顺序（PR 正文 `Closes/Fixes/Resolves #N` → 消息头 `task: #N` → 分支名（agent/ 前缀加任务编号，不带尖括号示例）
## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
