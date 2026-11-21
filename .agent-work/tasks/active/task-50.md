# 任务 #50：TASK-06-C：Grok 退役口径对齐（协议/注册表/分派规则/实现映射/控制台文案）

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/50
- 分支：`agent/task-50-retire-grok-align-docs`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

## 任务说明

TASK-06 子任务 C：把 **Grok 退役后的口径**在协议、注册表、标签、门户控制台四处对齐，让文档与代码/实现一致。

本任务对应总纲 Issue #21 的缺口 4。

### 现状（已逐处核实，共 5 份文档 + 1 处前端文案）

Grok Bot 于 2026-11-14 退役（PR #46，`chore(ci): Grok Bot 退役，冒烟链路改由 D:\02 本机 smoke-watcher 接管`）。冒烟改由 D:\02 本机 `smoke-watcher` 承担。但**口径没有全量对齐**，以下位置仍写着 Grok 或已被作废的机制：

| 位置 | 现在写的 | 与现实不符之处 |
| --- | --- | --- |
| `docs/workspace/agent-protocol.md` | 第二节第 6/7/8 类消息的执行者写「smoke-watcher（D:\02 本机）」✅，但第三节状态机图里仍写 `state:qa-ready ──(Grok 触发)──▶`；第四节第 1 条写「QA 失败：Grok 发 QA_FAILED…」；补充规则写「2026-11-14 起 QA 由 D:\02 本机 smoke-watcher 承担」 | 同一份文档内自相矛盾：状态机图与第四节第 1 条还写 Grok |
| `docs/workspace/agent-registry.md` | 注册表整行还写着「Grok Bot \| QA Worker \| Routine webhook 触发，D:\02 执行 \| 机器人身份（待确认）\| 已注册（webhook 链路已验证）」；能力表里 Grok 一行还在；「最近活动」表还停在 2026-11-13 | 登记的是**已退役**的 Agent，且能力表没有反映「QA 现由 D:\02 本机承担」这个新角色 |
| `docs/workspace/dispatch-rules.md` | 第二节能力表有「Grok Bot \| D:\02 冒烟测试…\| 只接 QA 任务，第一阶段不写代码」；第一节第 2 条「负责人」写「三选一：`agent:muse-2` / `agent:muse-3` / `agent:grok`」 | `agent:grok` 已退役，不该再作为可选负责人 |
| `docs/workspace/github-automation.md` | 全景图第 23–26 行「quality 全绿 → agent-orchestrator.yml 自动 POST webhook 请 Grok 冒烟」；第二节表格里 `agent-orchestrator.yml` 一行仍描述「POST webhook 请 Gr
## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [x] 按 Issue 验收标准实现
- [x] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
