# 任务 #51：TASK-06-D：闭环协议条款与实现映射（QA/FIX 消息 + 状态机 + 人工介入点）

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/51
- 分支：`agent/task-51-qa-fix-loop-protocol`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

## 任务说明

TASK-06 子任务 D：把「自动测试 → Review → 失败修复 → 重新验收」这条闭环**写成协议条款**，并落成**实现映射**文档。

本任务对应总纲 Issue #21 的缺口 4 的后半段，也是整个 TASK-06 的收口。

### 为什么要单独一个任务

闭环由四块拼成，每块都在别的任务里（#48 QA 回写与门禁、#49 修复闭环、#50 口径对齐），但**它们合起来构成一条完整的状态机**，而协议目前只描述了其中旧的一半：

- `agent-protocol.md` 第二节的 12 类消息里，`QA_STARTED` / `QA_FAILED` / `QA_PASSED` / `FIX_REQUESTED` / `FIX_COMPLETED` 五类**在现实里要么没实现（QA 三类）、要么实现细节还没定（五类的轮次与归属）**；
- 第三节状态机图里 `state:in-review → state:qa-ready → state:qa-passed / state:qa-failed` 这条支路是断的；
- 第四节「失败与返工流程」写的四步，目前只有第 1 步的**前半段**（贴失败报告）存在，后半段（建修复 Issue、派回原开发 Agent、3 次上限）由 #49 落地。

也就是说：**等 #48、#49 合并后，代码与协议之间仍然差着一份「谁在什么条件下发什么消息、打什么标签」的准确描述。** 本任务把这份描述补上，让下一个任务（比如「自动 Review」或「多 Agent 汇总」）不必重新逆推实现。

### 本任务要做的

1. **协议条款**（`docs/workspace/agent-protocol.md`）：
   - 第二节 12 类消息表：**逐类**写清「谁发、在什么触发条件下发、发出时带哪些机器可读字段、同时打/摘哪些标签」。五类 QA/FIX 消息要写准（例：`FIX_REQUESTED` 必带修复轮次；`QA_FAILED` 必带 head SHA 与日志链接）。
   - 第三节状态机图：**按 #48 / #49 的最终实现重画**，把 `state:qa-ready`、`state:qa-passed`、`state:qa-failed`、`needs-replan` 四个标签的进入/退出条件都标在图上，并标注**哪
## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
