# Agent 注册表（第一阶段，2026-11-13）

> Worker 注册、心跳与健康检查的登记册。Muse 1 维护本表，身份变化先改这里再改代码。
> 心跳定义：节点的最近 GitHub 活动时间（push / 评论 / PR 更新），Muse 1 定时查 API 判定，90 分钟无活动判失联。
> 身份独立化（V6，用户 2026-11-13 决定）：Muse 1/2/3 尽可能用独立 GitHub 身份，Grok 如支持也用独立机器人身份（Grok 已于 2026-11-14 退役，本条对其不再适用）；暂时做不到时先用机器身份标记过渡（见下表“过渡方案”列）。

| Agent | 角色 | 运行环境 | GitHub 身份 | 状态 | 过渡方案 |
| --- | --- | --- | --- | --- | --- |
| Muse 1 | 总指挥 / Orchestrator：拆解、分配、协调、审核、决策 | 独立 VM | 用户 Jouwala 经 Muse 连接器（GitHub 账号 Jouwala） | 已注册 | — |
| Muse 2 | Coding Worker：负责分配给它的开发任务 | 独立 VM | 现与 Muse 3 共用 jouwalamiaoyin@gmail.com 身份 | 独立身份过渡中 | 机器身份标记：认领评论头写 `agent-claim: muse-2`，提交信息写 `muse-2:` 前缀，PR 描述注明负责人 |
| Muse 3 | Coding Worker：负责分配给它的开发任务 | 独立 VM | 现与 Muse 2 共用 jouwalamiaoyin@gmail.com 身份；计划独立身份 miaoyinwajila@gmail.com，待确认 | 独立身份过渡中 | 机器身份标记：认领评论头写 `agent-claim: muse-3`，提交信息写 `muse-3:` 前缀，PR 描述注明负责人 |
| Muse 4 | 只读审查 Worker：ZIP 交付生产构建与打包审查（Phase 0，Issue #99） | 待确认 | 现与 Muse 2/3 共用 jouwalamiaoyin@gmail.com 身份 | 已注册（2026-11-16） | 机器身份标记：认领评论头写 `agent-claim: muse-4`，提交信息写 `muse-4:` 前缀，PR 描述注明负责人 |
| Muse 5 | 只读审查 Worker：ZIP 交付生产部署与发布 QA 审查（Phase 0，Issue #100） | 待确认 | 现与 Muse 2/3 共用 jouwalamiaoyin@gmail.com 身份 | 已注册（2026-11-16） | 机器身份标记：认领评论头写 `agent-claim: muse-5`，提交信息写 `muse-5:` 前缀，PR 描述注明负责人 |
| Grok Bot | QA Worker：冒烟测试、功能验证、测试报告、失败反馈；不写代码（第一阶段） | Routine webhook 触发，D:\02 执行（已停用） | 机器人身份（待确认是否支持独立身份） | **已退役（2026-11-14，PR #46）** | QA 职责已转 D:\02 本机 smoke-watcher（本机脚本，非跨 VM Agent）；本行保留为历史登记，不再接任务 |

> **本表是自动分派白名单的机器可读来源**：`apps/control-plane/backend/dispatch/builder_registry.py`
> 取本表「角色含 `Worker` 且状态未标记已退役」的行，把 Agent 名归一化成标签
> （`Muse 4` → `agent:muse-4`）。新增 / 退役 Worker 只改本表，不用改分派代码；
> 表格格式被改坏时 CI 的 `test_builder_registry.py` 会先红，不会静默变成“谁都不派”。

## 能力标签

| Agent | 能力 |
| --- | --- |
| Muse 1 | 需求分析、任务拆分、文件范围查重、进度协调、结果整合、冲突处理、QA 结果裁决（通过 / 返工 / 升级） |
| Muse 2 | Python 后端、FastAPI、SQLite/PG、任务系统、数据访问层 |
| Muse 3 | 前端（React / 门户）、services 重组、文档 |
| Muse 4 | 生产构建与打包审查、技术栈与依赖盘点、ZIP 打包设计 |
| Muse 5 | 生产部署与发布 QA 审查、双平台测试矩阵、安装 / 升级 / 回滚验收标准 |
| D:\02 本机 smoke-watcher | D:\02 冒烟测试（§8.2–8.4）、离线 pytest、冒烟结果评论、通过打 `smoke-passed` |

> 冒烟职责原属 Grok Bot（2026-11-14 退役，PR #46），现由 D:\02 本机 smoke-watcher 承接——它是本机脚本服务，不是跨 VM Agent，不走 Issue 分派，故不占注册表上表的 Agent 行，只在能力表登记。
> 能力标签是认领匹配的依据，不是硬限制：任务需要的能力超出负责人已有能力时，由 Muse 1 改派，不硬上。

## 最近活动（Muse 1 每次检查后更新）

| Agent | 最近 GitHub 活动 | 状态 |
| --- | --- | --- |
| Muse 1 | 2026-11-14：PR #46（Grok 退役）合入、TASK-06 子任务拆分派发 | 在线 |
| Muse 2 | 2026-11-14：多 Worker 调度线推进（PR #52 中央调度器于当夜合入） | 在线 |
| Muse 3 | 2026-11-14：TASK-04 交付 PR #47 合入，Issue #19 由回写自动化自动关闭（派发链首次端到端跑通） | 在线 |
| Muse 4 | 2026-11-16：注册登记，试验 Issue #99 待认领 | 已注册，待认领 |
| Muse 5 | 2026-11-16：注册登记，试验 Issue #100 待认领 | 已注册，待认领 |
| Grok Bot | 2026-11-14：已退役（PR #46），冒烟职责转 D:\02 本机 smoke-watcher | 已退役 |
| D:\02 本机 smoke-watcher | 2026-11-14：接管冒烟，给 PR #44–#47 打 `smoke-passed` | 运行中（本机服务） |

## 注册 / 注销规则

- 新 Worker 加入：先在本表登记（角色、VM、GitHub 身份、能力），再由 Muse 1 分配试验 Issue，试验通过后正式接任务。
- Worker 退出：Muse 1 在本表标记注销，其未完成任务按租约过期重派（认领后 4 小时无进展即过期）。
- 身份变更：GitHub 身份独立化完成后，更新本表对应行，并删除过渡方案列的标记要求。
