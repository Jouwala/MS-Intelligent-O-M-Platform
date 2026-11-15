# 多 Worker 调度系统统一修复 —— 最终交付与验收报告

> 最终验收对象：`main`，本文件合入前的 SHA 为 `a3ce8af`（PR #66 合并后；本文件合入后会再前进一个提交）
> 设计与决策：`multi-worker-scheduler-design.md`（ADR-2026-11-14）、
> `multi-worker-github-native-authority.md`（ADR-7）
> 回归矩阵：`multi-worker-regression.md`（T01–T30）
> 独立验收：`muse2-final-verification.md`、`muse3-final-verification.md`、
> `muse3-final-verification-addendum.md`

## 一、交付的 PR（全部已冒烟 + 自动合入 main）

| PR | 内容 | 合入时间 |
| --- | --- | --- |
| #52 | 第一轮统一修复（分页/心跳/进程树/清理/取消/Linux 专项 + 中央调度器） | 2026-11-14 |
| #53 | 多 VM 生产拓扑部署门槛（owner 要求保留到新层验收） | 2026-11-14 |
| #54 | Linux 心跳时序 flake 修复 | 2026-11-14 |
| #60 | PR-1 GitHub 原生认领层（HWM CAS / RestRefStore / fake GitHub server） | 23:49 |
| #61 | PR-2 fencing（代次校验 / 续租推进 token / 旧执行者拒绝） | 23:52 |
| #62 | PR-3 过期租约回收（ref-CAS 回收权 + cron Reaper） | 00:05 |
| #63 | PR-4 范围锁（创建后校验 + 冲突回退 + 孤儿清理） | 00:08 |
| #64 | PR-5 完成幂等 + Worker 切换到 GitHub 权威层（激活点） | 00:20 |
| #65 | PR-6 删除 SQLite 权威实现（权威唯一） | 00:32 |
| #66 | B1/R2 确定性回归 + 真实 GitHub 证据入库 + 文档校准 | 01:03 |

## 二、最终架构（权威唯一）

- **GitHub 是唯一权威**：认领 = `POST /git/refs` 创建 HWM ref（服务端 CAS，201=唯一赢家、422=输家重读）；
  租约 = lease ref（ref 名带 `epoch_ms`，存活判定只看它）；fencing = 单调 token + 每次写前
  `max_token == presented_token` 校验；范围锁 = 创建后校验+冲突回退；完成幂等 = 唯一
  `ref-completion-id` 评论标记 + 标签收敛；回收 = Actions cron Reaper，先 `agent-reap/issue/<token>`
  CAS 抢回收权。
- **SQLite 调度器已删除**（PR-6）：代码/测试/文档中无 `SchedulerService/SQLiteStore` 引用，
  无 `AGENT_SCHEDULER_DB/BACKEND` 运行时选择，不存在第二裁决者。
- **Worker**：持续原子心跳、watchdog 退出确认、进程组 SIGTERM→SIGKILL、统一 try/finally 清理、
  取消、Linux 主机信息/可执行位/命令解析保留；完成后直接写 GitHub，无本地 outbox。

## 三、验证证据

| 项 | 结果 |
| --- | --- |
| 本机三套 pytest（最终 main） | **332 passed, 6 skipped**（6 skip 全平台专用） |
| CI（PR #66 head，quality 7/7） | Windows **332/6**、Linux **102/4**；run 43660614802 |
| 真实 GitHub 多 checkout（官方 driver） | **28/28 CHECK PASS**（`evidence/real-github-multivm-verify.txt`） |
| Muse 2 独立复跑真实 API | 28/28 PASS，独立探针 S1–S6 PASS |
| Muse 3 独立真实 API 探针 | **52/52 CHECK PASS**（`evidence/muse3-real-github-evidence.txt`） |
| 修复前基线复现 | 9/9 组探针失败（`evidence/baseline-defects.txt`） |
| B1/R2 回归 | 新增 2 条确定性测试；去掉 token 比较变异下恰好 2 红、原 23 绿 |

## 四、独立验收结论

- **Muse 2**：T01–T30 **30/30 PASS**，无阻断；建议解除多 VM 部署门槛（附生产前置项）。
- **Muse 3**：最终层发现唯一阻断 B1/R2（HWM 已推进但旧 lease ref 未删窗口无测试覆盖），
  已补回归并通过**独立变异复验 → B1/R2 CLOSED**；建议 owner 批准解除门槛。
- 两方均明确：**解除权在 owner**，且解除前应完成生产前置项。

## 五、多 VM 部署门槛（已解除，owner 2026-11-15 批准）

`multi-worker-scheduler-design.md` §十三 / 本文：
**门槛已于 2026-11-15 由 owner 批准解除；多 VM 部署不再禁止。**

解除依据：
1. 本报告与 B1 复审/证据入库（PR #67 完成）；
2. GitHub 原生 ref-CAS 权威层已实施并合入（PR #60~#65），SQLite 权威已删除；
3. Muse 2 最终验收 T01–T30 30/30 PASS；Muse 3 B1/R2 已闭环；
4. **owner 2026-11-15 明确批准解除**。

生产部署前置（解除后仍须完成，非门槛条件）：
1. 生产部署前完成一次**真实两台物理 VM 联调**（当前只做了两个独立 checkout + 真实 API）；
2. 启用 Reaper cron 并配置失败告警；
3. refs 生命周期/配额监控（HWM 每次续租新增、matching-refs 不分页的真实语义）。
`§十三` 与矩阵 §六的 BLOCKED 项已随本解除同步更新。

## 六、已知风险与后续项（非阻断）

1. **W7**：Worker 根进程自行退出后遗留的孤儿子进程，看门狗重启不清理（Muse 2 实测）。
2. 任务命令继承 Worker 环境（Secret 对任务命令可见），建议最小环境白名单。
3. HWM/lease/reap/dispatch refs 会累积，需要生命周期清理与配额监控。
4. Reaper 自动 cron 尚未在生产启用（本轮只手动 dispatch 成功 run 43660614194）。
5. 最终 merge commit 在 main 上没有 quality push run（auto-merge 用 GITHUB_TOKEN，
   GitHub 抑制 push 事件）；同树 PR head CI + 手动 dispatch 可作为生产前复核。
6. 真实 GitHub Actions 双 workflow 并发仍未实测（需隔离环境触发两条 workflow）。

## 七、回滚

- 整体回滚：按 #66 → #60~#65 的逆序 revert（或 revert 各 merge commit）；Worker 回退到
  PR #52 的 SQLite 权威实现需要 revert 删除 PR-6，属大回滚，建议在部署前用 tag 固化。
- 运行时状态：测试/验证产生的 refs 均在隔离私库；生产仓库如出现异常 `agent-*` refs，
  可删除后从任务描述重建（任务描述仍在 GitHub Issue）。
- 回滚不涉及数据库迁移（无本地库）。

## 八、交付物索引

| 交付物 | 位置 |
| --- | --- |
| 设计决策（两阶段 ADR） | `multi-worker-scheduler-design.md`、`multi-worker-github-native-authority.md` |
| T01–T30 矩阵与证据 | `multi-worker-regression.md`、`evidence/` |
| Muse 2/3 最终验收与 B1 复审 | `muse2-final-verification.md`、`muse3-final-verification.md`、`muse3-final-verification-addendum.md` |
| 部署门槛 | `multi-worker-scheduler-design.md` §十三、`agent-protocol.md` §8.4 |
| 代码 | `apps/control-plane/backend/scheduler/`（ref_* / github_refs / factory）、
  `apps/control-plane/backend/dispatch/lease_reaper.py`、`apps/agent-worker/`、`.github/workflows/` |
