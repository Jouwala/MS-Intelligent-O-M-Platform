# Agent 注册表（第一阶段，2026-11-13）

> Worker 注册、心跳与健康检查的登记册。Muse 1 维护本表，身份变化先改这里再改代码。
> 心跳定义（2026-11-18，#141 改为真实心跳）：Worker 进程每轮写一条心跳 beat ref
> `refs/heads/agent-heartbeat/<agent>/<instance>/<epoch_ms>/<state>`（epoch 毫秒，
> state 自报 online / idle / working / waiting / blocked / failed）；`list_workers`
> 由心跳新鲜度 + 有效租约推导七态 ONLINE / IDLE / WORKING / WAITING / BLOCKED /
> OFFLINE / FAILED。过期阈值是心跳 TTL 90 秒（`api.DEFAULT_HEARTBEAT_TTL`），
> 租约有效时不判 offline。旧口径“最近 GitHub 活动时间、90 分钟无活动判失联”
> 是错的——那不是进程心跳，已废止。
> 身份独立化（V6，用户 2026-11-13 决定）：Muse 1/2/3 尽可能用独立 GitHub 身份，Grok 如支持也用独立机器人身份（Grok 已于 2026-11-14 退役，本条对其不再适用）；暂时做不到时先用机器身份标记过渡（见下表“过渡方案”列）。

| Agent | 角色 | 运行环境 | GitHub 身份 | 状态 | 过渡方案 |
| --- | --- | --- | --- | --- | --- |
| Muse 1 | 总指挥 / Orchestrator：拆解、分配、协调、审核、决策 | 独立 VM | 用户 Jouwala 经 Muse 连接器（GitHub 账号 Jouwala） | 已注册 | — |
| Muse 2 | Coding Worker：负责分配给它的开发任务 | 独立 VM | 现与 Muse 3 共用 jouwalamiaoyin@gmail.com 身份 | 独立身份过渡中 | 机器身份标记：认领评论头写 `agent-claim: muse-2`，提交信息写 `muse-2:` 前缀，PR 描述注明负责人 |
| Muse 3 | Coding Worker：负责分配给它的开发任务 | 独立 VM | 现与 Muse 2 共用 jouwalamiaoyin@gmail.com 身份；计划独立身份 miaoyinwajila@gmail.com，待确认 | 独立身份过渡中 | 机器身份标记：认领评论头写 `agent-claim: muse-3`，提交信息写 `muse-3:` 前缀，PR 描述注明负责人 |
| Muse 4 | 生产构建与打包 Worker：ZIP 交付构建与打包（Phase 1–2）；Phase 0 报告 PR #106 待合入 | 待确认 | 现与 Muse 2/3 共用 jouwalamiaoyin@gmail.com 身份 | 已注册（2026-11-16） | 机器身份标记：认领评论头写 `agent-claim: muse-4`，提交信息写 `muse-4:` 前缀，PR 描述注明负责人 |
| Muse 5 | 生产部署与发布 QA Worker：ZIP 交付部署与发布 QA（Phase 3–4）；Phase 0 报告已合入 PR #104 | 待确认 | 现与 Muse 2/3 共用 jouwalamiaoyin@gmail.com 身份 | 已注册（2026-11-16） | 机器身份标记：认领评论头写 `agent-claim: muse-5`，提交信息写 `muse-5:` 前缀，PR 描述注明负责人 |
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

## 最近活动（已删除，2026-11-18，#133）

本节原是一张手填的“最近 GitHub 活动”表（约定“Muse 1 每次检查后更新”），
但没有人维护——最后一条真实记录停在 2026-11-16（Muse 4/5 注册登记），
之后 Muse 2/3/4/5 的认领、PR、合入一概没有更新，表里写的“在线”全是
过期信息。手填的状态表一旦过期就是误导，不如没有：**本节整表删除**，
不再恢复。实时状态以 GitHub 本身为准（Issue / PR / 评论时间线），
统一状态视图与心跳机制是 Phase 3（P3-1/P3-2）的事，届时由机器生成，
不再手填。

## 注册 / 注销规则

- 新 Worker 加入：先在本表登记（角色、VM、GitHub 身份、能力），再由 Muse 1 分配试验 Issue，试验通过后正式接任务。
- Worker 退出：Muse 1 在本表标记注销，其未完成任务按租约过期重派（认领后 4 小时无进展即过期）。
- 身份变更：GitHub 身份独立化完成后，更新本表对应行，并删除过渡方案列的标记要求。
