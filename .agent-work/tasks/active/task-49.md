# 任务 #49：TASK-06-B：QA 失败自动修复闭环（修复 Issue 自动创建 + 轮次上限 3 + needs-replan）

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/49
- 分支：`agent/task-49-auto-fix-loop`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

## 任务说明

TASK-06 子任务 B：把「QA 失败 → 自动修复 → 重新验收」这条返工链路真正做出来。

本任务对应总纲 Issue #21 的缺口 3。

### 现状（已核实）：协议和标签都定义了，但没有任何实现

- `agent-protocol.md` 第四节「失败与返工流程」第 1–4 条写得很完整：QA 失败 → 建修复 Issue 回原开发 Agent → 修复后重新走评审与测试 → 同一任务修复达 3 次打 `needs-replan` 交 Muse 1 重新规划。
- `.github/labels.yml` 已定义 `needs-replan` 标签，描述就是「自动修复已达 3 次上限，由 Muse 1 重新规划，不再自动重派」。
- `agent-protocol.md` 第二节第 9、10 类消息 `FIX_REQUESTED` / `FIX_COMPLETED` 也定义好了。

**但**：没有任何工作流或脚本会创建修复 Issue、没有任何东西会打 `needs-replan`、没有任何东西会数「这是第几轮修复」。原 #21 正文里写的「新建 `.github/workflows/auto-fix.yml`」「`retry_count` 上限 3，超限打 `needs-replan`」**从未落地**（全仓库无此文件）。

所以现在 QA 失败后，闭环是断的：watcher 只在 PR 上贴一份失败报告（且 Issue 上连报告都没有，那属于子任务 #48），**没有任何机制把「失败了」变成「有人要去修」**。

### 本任务要做的

1. **QA 失败 → 自动创建修复 Issue**：新建 `.github/workflows/auto-fix.yml`，在关联 Issue 收到 `state:qa-failed` 后（或由 #48 提供的机器可读 `QA_FAILED` 事件驱动），自动建一个修复 Issue，正文按 `.github/ISSUE_TEMPLATE/agent-task.md` 模板填齐：
   - 任务说明：写明「修复 #<原 Issue> 的 QA 失败项」+ 原失败报告链接；
   - 负责人：**回派给原开发 Agent**（从原 Issue / PR 的 `agent:muse-2` / `agent:muse-3` 标签判定；取不到就不猜、不硬派）；
   - 依赖任务：写 `depends on #<原 Issue>`（原 Issue 未关闭时该依赖确实应阻塞 —�
## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
