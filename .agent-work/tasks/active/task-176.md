# 任务 #176：[修复] 无负责人告警重复建 Issue 且误报 #116（应走告警收件箱 + 稳定 key）

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/176
- 分支：`agent/task-175-ownerless-alert-dedupe`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

## 任务说明
无负责人告警（ownerless-alert）有两个缺陷：

1. **重复建 Issue，不原地更新**：目前已有 4 个内容相同的告警 Issue：#154（2026-11-18 17:48）、#164（18:47）、#165（2026-11-19 00:45）、#167（07:57）。每次 Reaper 运行都新建一个，而不是 PATCH 已有的。
2. **误报 owner-held 任务**：最新 #167 唯一 flag 的是 #116，而 #116 是 lead 特登留住的任务、无 agent 标签是设计（#74 已关闭，所以已从列表消失）。告警正文却要求“Muse 1 确认负责人后补 agent 标签，或关闭”，#116 两条都不适用。

## 根因（待核实，以代码为准）
- marker 用时间戳：``，每次运行都唯一，查找已有 Issue 时对不上，于是新建。#143 的 alert_inbox 用的是稳定 key（``），本告警未走统一路由。
- 未排除明确 owner-held 的 Issue。

## 负责人
agent:muse-2

## 优先级
P1

## 文件修改范围
- 生成无负责人告警的代码（Reaper / ownerless-alert 路径）：改为走 `apps/control-plane/backend/dispatch/alert_inbox.py` 的统一路由，class 如 `ownerless-task`，用稳定 key，PATCH #148 [告警收件箱]；不再单独建 Issue。或至少做到：marker 改为稳定 key，运行前先查找已有告警 Issue，找到就 PATCH。
- 同时排除明确 owner-held 的 Issue（当前是 #116；排除方式由 builder 定：白名单或专用标签，写进代码注释）。
- 不要改 #148、#156 这两个载体 Issue 本身的用途。

## 依赖任务
无

## 验收标准
- 下一次 Reaper 运行后不再新建重复告警 Issue；#148 里同一条告警原地更新。
- #116 不再出现在无负责人告警里。
- 走 PR 到 main，CI 绿。

## 所属分支
agent/task-175-ownerless-alert-dedupe


## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
