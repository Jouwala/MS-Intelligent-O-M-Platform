# QA / 修复闭环实现映射（TASK-06-D，2026-11-15）

本文把「自动测试 → Review → 失败修复 → 重新验收」这条闭环落成**实现映射**：每一环由谁执行、在什么触发下做什么、幂等与去重口径是什么、依据在哪个文件的哪一行。协议条款本身（消息格式、状态机、返工规则）写在 `docs/workspace/agent-protocol.md` 第二/三/四节，本文不重复，只指实现。

分工：`github-automation.md` 是全部自动化工作流的全景与清单（一行一个工作流）；本文只写 QA/修复这一条链的逐环细节。实现依据是 TASK-06-A（#48，QA 回写与 SHA 绑定门禁）与 TASK-06-B（#49，auto-fix 自动修复闭环）的已合入代码，行号以 2026-11-15 的 main 为准；契约由 `apps/agent-worker/tests/test_qa_writeback_gate.py` 与 `apps/agent-worker/tests/test_auto_fix_loop.py` 两份静态契约测试守着，改实现先看这两份测试会不会红。

## 一、闭环链路

```
开发 Agent 提交 PR（state:in-review）
  → [CI] quality.yml 全绿
  → [冒烟] smoke-watcher 检出 PR head：发 QA_STARTED + 打 state:qa-ready → 跑 ops\smoke\smoke.ps1
      ├─ 通过：PR 报告评论带 smoke-result 绑定标记 → 打 smoke-passed；Issue 发 QA_PASSED + state:qa-passed
      │     → [合并] auto-merge 复核绑定 SHA == 当前 head + 该 head 的 CI 全绿 → 合并
      │     → [回写] pr-merged-writeback 发 PR_MERGED + state:merged + 关闭 Issue
      └─ 失败：Issue 发 QA_FAILED（结构化） + state:qa-failed
            → [修复] auto-fix 建修复 Issue（回派原开发 Agent） + 在原 Issue 发 FIX_REQUESTED
            → 修复 Agent 修完在原 Issue 发 FIX_COMPLETED，修复 PR 重新走 CI → 冒烟（本链递归）
            → 同一根任务第 4 次失败：auto-fix 打 needs-replan 停手 → Muse 1 重新规划（人工介入点）
```

## 二、逐环映射

| 环节 | 执行者 | 触发与动作 | 幂等与去重 | 依据（文件:行号） |
| --- | --- | --- | --- | --- |
| CI 检查 | GitHub Actions `quality.yml` | PR push 触发，7 个 job（见 github-automation.md 第八节） | 同 head 重跑覆盖旧结论；auto-merge 只认 head SHA 上的运行 | `.github/workflows/auto-merge.yml:168`（按 head_sha 查 quality 运行）、`:180`（该 commit 全部 check-runs） |
| 检出待冒烟 PR | smoke-watcher（D:\02 本机脚本） | 每 60 秒轮询 open PR，只处理 quality 结论为 success 的 head；先核对 PR 上的 `smoke-passed` 绑定 SHA，绑定旧 head 的先摘标签再重跑 | 本机状态文件按 PR 记录已冒烟/已失败的完整 SHA，同 head 不重复跑；绑定校验不依赖本机状态 | `ops/deploy/smoke-watcher.ps1:306`（Test-QualityGreen）、`:367`（Get-SmokeResultBoundSha）、`:471-491`（过期标签摘除重跑） |
| QA 开始回写 | smoke-watcher | 切到 PR head 跑冒烟**之前**：Issue 状态单值化为 `state:qa-ready`，发 QA_STARTED（PR、HEAD SHA、预计时长） | 评论带 `qa-started: pr-<PR>-sha-<短SHA>` 标记，发前查 Issue 评论去重 | `ops/deploy/smoke-watcher.ps1:519`（调用点，先于 checkout）、`:380`（Send-QaStarted）、`:342`（Set-IssueQaState 单值化）、`:352`（Test-IssueMarker） |
| 跑冒烟 | smoke-watcher | checkout PR head（detached）跑 `ops\smoke\smoke.ps1` §8.1–8.4，finally 切回原分支；工作树有未提交的已跟踪改动时跳过该 PR | 同 head 通过/失败各只跑一次（状态文件 + 评论标记双保险） | `ops/deploy/smoke-watcher.ps1:201`（Enter-PrCheckout）、`:560`（报告首行拼绑定标记） |
| QA 结果回写 | smoke-watcher | 通过：给 PR 打 `smoke-passed`、Issue 发 QA_PASSED + `state:qa-passed`；失败：Issue 发 QA_FAILED（PR、完整 HEAD SHA、`  - ` 缩进失败项、错误信息与报告入口）+ `state:qa-failed`，PR 不打 smoke-passed | 评论带 `qa-result: pr-<PR>-sha-<短SHA>` 标记，同 (PR, SHA) 不重复发；回写失败只记日志不挡主流程 | `ops/deploy/smoke-watcher.ps1:402`（Send-QaResult）、`:565-568`（打 smoke-passed）、`:574`（调用点） |
| 合并门禁 | GitHub Actions `auto-merge.yml` | 周期轮询 open PR：有 `smoke-passed`、无 `do-not-merge` 才继续；取 PR 评论里最后一条 `smoke-result … result=passed` 标记的 SHA 与当前 head 比对，无标记或对不上都跳过；再验该 head 的 quality 与全部 check-runs 全绿才合并，合并请求带 `sha` 锁定 | 门禁只认绑定标记不认标签本身，head 一变旧结果自动过期；判定顺序由契约测试锁定（标签 → 绑定 → 合并） | `.github/workflows/auto-merge.yml:143`（取 head SHA）、`:152-153`（标签与否决）、`:160-162`（绑定提取与比对）、`:189`（带 sha 合并） |
| 合并回写 | GitHub Actions `pr-merged-writeback.yml` | PR 关闭（已合并）事件或 auto-merge 显式 dispatch：给 Issue 打 `state:merged`、摘其余 `state:` 标签、发 PR_MERGED（`from: github-actions`）、关闭 Issue | 以 PR merged 状态为准，重复触发结果一致 | `.github/workflows/pr-merged-writeback.yml:32`（触发）、`:126`（PR_MERGED 消息头） |
| 修复触发 | GitHub Actions `auto-fix.yml` | `state:qa-failed` 被打上（`issues: labeled`）或人工 `workflow_dispatch` 重放；动手前复核双信号：标签在 + 评论里有最新一条 QA_FAILED，从其正文解析 PR 编号（`qa-result: pr-<N>`）、HEAD SHA（首个 40 位十六进制串）、失败项（`  - ` 行） | 已带 `needs-replan` 的 Issue 整体跳过 | `.github/workflows/auto-fix.yml:44-61`（触发与 job 条件）、`:86-109`（复核与解析） |
| 建修复 Issue | auto-fix 工作流 | 按 `agent-task.md` 模板八节建 Issue：标题 `[修复] #<原> QA 失败（第 R 轮）：<原标题>`，正文带 `depends on #<原 Issue>`、失败项逐条转验收项、验收含「在原 Issue 发 FIX_COMPLETED」；回派只认原 Issue 的 `agent:muse-2`/`agent:muse-3` 标签，取不到不派任何人 | 同一根任务 + 同一失败 HEAD SHA 只建一个（标记里带 sha，建前查） | `.github/workflows/auto-fix.yml:111-124`（计数与幂等）、`:149-154`（回派判定）、`:156-208`（正文与创建） |
| 修复请求回写 | auto-fix 工作流 | 在原 Issue 发 FIX_REQUESTED（`from: auto-fix`）：新 Issue 编号、失败报告链接、轮次（第 R 次 / 上限 3）、负责人或「请 Muse 1 介入指派」 | 随建 Issue 同一步发生，幂等跳过时不重发 | `.github/workflows/auto-fix.yml:210-227` |
| 轮次计数 | auto-fix 工作流 | 计数源 = 全仓 Issue 正文里的标记 `<!-- auto-fix: for-issue-<直属> root-issue-<根> round-<R> sha-<HEAD> -->`，按 `root-issue` 数：链式返工（修复 Issue 再失败）继续算在根任务头上，整链共用 3 次上限 | 计数只读 GitHub 单点，不依赖任何机器的本地文件 | `.github/workflows/auto-fix.yml:15-24`（设计说明）、`:111-119`（实现） |
| 达上限停手 | auto-fix 工作流 | 已有 3 轮修复 Issue 后再失败（第 4 次）：只打 `needs-replan` + 发说明评论（根任务、报告链接、PR、SHA），不建 Issue、退出 | `needs-replan` 是终止标记，带它的 Issue 后续触发整体跳过 | `.github/workflows/auto-fix.yml:126-146` |
| 修复完成回写 | 修复 Agent（人工动作） | 在**原 Issue** 发 FIX_COMPLETED：修复内容、新提交、回归自检结果；修复 PR 重新走 CI → 冒烟全链 | 无自动去重，靠评论本身留痕 | 要求写在修复 Issue 验收清单里：`.github/workflows/auto-fix.yml:187`；协议第 10 类：`docs/workspace/agent-protocol.md` 第二节 |

## 三、人工介入点

以下情况系统不会自己处理，必须有人出手（括号内是出手方）：

1. **`needs-replan` 已打上**（Muse 1）：自动修复达 3 次上限后停手，等 Muse 1 重新规划——拆分任务、换方案或升级给用户。系统不会自动摘这个标签，也不会再建修复 Issue。
2. **取不到原开发 Agent**（Muse 1）：原 Issue 没有 `agent:muse-2` / `agent:muse-3` 标签时，auto-fix 建的修复 Issue 不派给任何人，FIX_REQUESTED 里写明请 Muse 1 介入指派。Muse 1 补上负责人后，修复 Issue 才会被认领。
3. **`do-not-merge` 在场**（打标签的人 / 用户）：auto-merge 一票否决跳过，冒烟与 QA 回写照常；要合必须先由人摘掉该标签，没有任何自动化会摘它。
4. **`risk:high` 任务**（用户）：标签与状态并存，合并前须用户明确批准；auto-merge 不区分 risk 等级，批准动作本身在 GitHub 之外（用户口头/评论确认后由 Muse 1 把关放行或摘除阻挡标签）。
5. **冒烟因环境原因无效**（D:\02 操作者 / Muse 1）：D:\02 被其他任务占用、工作树有他人在途改动、MSNOC1 掉线等导致冒烟中断或被跳过时，该次不算通过也不算失败，不触发 auto-fix；需要人确认环境恢复后等 watcher 下一轮，或人工 `workflow_dispatch` 重放相关环节。
6. **QA 回写失败**（看 watcher 日志的人）：watcher 发 QA_STARTED / QA 结果评论失败时只记本机日志、不挡冒烟主流程，于是可能出现「PR 已打 smoke-passed 但 Issue 没有 QA_PASSED」的半截状态——合并门禁不受影响（只认 PR 侧绑定标记），但 Issue 状态会停在 `state:qa-ready`，需要人发现后手工补标签/评论。
7. **FIX_COMPLETED 缺失**（Muse 1 评审时）：修复者没在原 Issue 发 FIX_COMPLETED 没有任何机器校验拦截，只能在评审/巡逻时人工核对（见第四节待实现）。

## 四、待实现与已知边界

- **FIX_COMPLETED 无机器校验（待实现）**：没有工作流核对修复 Issue 关闭前原 Issue 上是否存在 FIX_COMPLETED 评论，也没有自动代发；目前靠修复 Issue 的验收清单 + Muse 1 人工核对。
- **QA_STARTED / QA_PASSED 无自动消费方**：这两类回写是信息性的（给人和其他 Agent 看），没有下游工作流以它们为触发；唯一被机器消费的 QA 消息是 QA_FAILED（auto-fix 的输入）。
- **auto-fix 轮次计数的规模上限**：计数查全仓 Issue 用 `per_page=100`（`.github/workflows/auto-fix.yml:22-24` 自述、`:110` 实现），当前仓库规模足够；Issue 总数超过 100 后老标记可能漏数，需要改分页，届时轮次与幂等判断都会受影响。
- **修复 Issue 的分支名是模板写死的**：auto-fix 生成的修复 Issue 正文里所属分支固定写 `agent/task-49-fix-<原 Issue 号>`（`.github/workflows/auto-fix.yml:191`），`49` 是 TASK-06-B 的任务号字面量，不随根任务变化；认领修复 Issue 时以正文为准即可，分支由 issue-to-pr 按正文创建。
- **watcher 的关联 Issue 解析只有三路**：PR 正文的 `Closes/Fixes/Resolves #N` → 正文 `task: #N` → 分支名 `agent/task-<N>`（`ops/deploy/smoke-watcher.ps1:315`），三路都取不到时 QA 回写整段跳过（冒烟与 smoke-passed 照常），Issue 状态不会推进——这类 PR 需要人工补正文或手工处理 Issue 标签。

## 五、复核方法

本文每条行号可用下面几条命令复核（仓库根目录执行）：

```powershell
grep -n "function Send-QaStarted|function Send-QaResult|function Set-IssueQaState" ops/deploy/smoke-watcher.ps1
grep -n "SMOKE_SHA=|has_label 'smoke-passed'|/pulls/`$PR/merge" .github/workflows/auto-merge.yml
grep -n "agent-msg: FIX_REQUESTED|root-issue|needs-replan" .github/workflows/auto-fix.yml
python -m pytest apps/agent-worker/tests/test_qa_writeback_gate.py apps/agent-worker/tests/test_auto_fix_loop.py -q
```
