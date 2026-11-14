# 任务 #19：TASK-04：Harness Adapter

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/19
- 分支：`agent/task-19-harness-adapter`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

## 目标
Harness Adapter：统一接口，各 Harness 一个驱动。
## 阶段
第一阶段（GitHubIssueAdapter）→ 第三/四阶段（GrokWebhookAdapter）。DeepSeek 已删除，不做。
## 负责人
Muse 3。
## 依赖
TASK-02 接口定义（§8.1：`start/stop/status/submit/result`）。
## 改动文件
- `apps/control-plane/backend/adapters/base.py`（统一接口）
- `apps/control-plane/backend/adapters/github_issue.py`
- `apps/control-plane/backend/adapters/grok_webhook.py`（复用现有 webhook）
## 验收标准
- 各驱动通过接口测试。
- 同一幂等键二次 `start` 不重复执行。
- 超时 4 小时判 `timeout`。

## 派发字段（认领时补齐）
- 任务说明：见上「目标」——统一 Harness 接口与各驱动；其中 github_issue.py 已由 TASK-03 的 PR #29 合入，剩余 base.py + grok_webhook.py。
- 优先级：P1（phase:1）
- 文件修改范围：apps/control-plane/backend/adapters/base.py、apps/control-plane/backend/adapters/grok_webhook.py、apps/agent-worker/tests/（新增接口测试）
- 依赖任务：TASK-02（#17，已关闭，依赖已满足）
- 所属分支：agent/task-19-harness-adapter
- 负责人：Muse 3（agent:muse-3）

## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [x] 按 Issue 验收标准实现（base.py + grok_webhook.py + 接口测试，见本分支提交）
- [x] agent-done-check 自检（结果贴到 Issue 评论）
## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
