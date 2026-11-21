# Muse 3 复审附录：B1/R2 关闭确认（最终 main 复审）

> 验收方：Muse 3（独立复审，不采信修复方声明）
> **复审 SHA：`a3ce8af30fd7ab72cee851bb2847a778e35ea043`**（PR #66 `fix(ref): 补齐 B1/R2 确定性回归 + 真实 GitHub 证据入库 + 最终文档校准`，base main，mergedAt `2026-11-15T01:03:43Z`）
> 被复审 PR：#66（head `ad61e9b06b0481b72bfe9669e41151008a93fd3f`）；上一轮阻断项来自 `4586d8b` 的最终验收（`muse3-final-verification.md`）
> 报告位置：`D:\tmp\final-acceptance\muse3-final-verification-addendum.md`（未修改仓库、未 commit/push）
> 环境：Windows（MSNOC1 / .20），Python 3.12.10，pytest 9.1.1，gh 2.102.0。

## 〇、复审结论

| 项 | 结论 |
| --- | --- |
| **B1/R2（唯一阻断项）** | **已关闭（CLOSED）**：两条新增确定性回归在正确代码下通过（两文件 **25 passed**）；我在临时副本独立注入「去掉 token 比较」变异后 **恰好这 2 条红、其余 23 条绿**（2 failed / 23 passed）——满足关闭判据。 |
| 全量回归 | 最终 main fresh clone：**332 passed, 6 skipped**（较上轮 330 增加 2 条 B1 回归；6 skip 仍全部平台专用）。 |
| CI | PR #66 head quality run `43660614802` **7/7 job 全绿**（Windows **332 passed/6 skipped**；Linux **102 passed/4 skipped**）；merge commit 上 `writeback`/`dispatch-next` success。 |
| N1/N2/N3/N4 | **均已修**（N4 有 2 处轻微残留措辞，非阻断，见 §5）。 |
| **多 VM 部署门槛建议（更新）** | **代码与验收条件已满足，B1 关闭后我建议 owner 批准解除门槛**；解除前请完成设计 §13 的生产前置项：**真实两台物理 VM 联调 + Reaper cron 启用与失败告警 + refs 生命周期/配额监控**。解除权在 owner。 |

## 一、新增两条回归测试存在且通过

最终 main fresh clone：`git clone --depth 1 --branch main https://github.com/Jouwala/noc-soc-intelligent-platform.git D:\tmp\muse3-recheck`，`git status` 干净，HEAD = `a3ce8af`。

| 测试 | 位置 | 是否存在 |
| --- | --- | --- |
| `test_ref_fencing.py::test_old_token_writes_rejected_when_hwm_advanced_but_old_lease_ref_present` | `apps/control-plane/backend/tests/test_ref_fencing.py:260` | 是 |
| `test_ref_completion.py::test_old_token_complete_rejected_when_hwm_advanced_but_old_lease_ref_present` | `apps/control-plane/backend/tests/test_ref_completion.py:231` | 是 |

```powershell
cd D:\tmp\muse3-recheck
python -m pytest apps/control-plane/backend/tests/test_ref_fencing.py `
    apps/control-plane/backend/tests/test_ref_completion.py -q -p no:cacheprovider
# => 25 passed in 13.35s
```

两条用例的覆盖方式经代码审阅确认正确：claim 得到 token=1 后**手工 `create(hwm_ref(1,2))` 推进 HWM 并保留旧 lease ref**（确定性重现 renew/claim 的中间窗口），随后断言 `transition` / `release` / `release(terminal)` / `ack_cancel`（fencing 侧）与 `complete`（完成侧）全部 `StaleFencing`，且完成侧断言没有写出任何 `ref-completion-id` 评论。

## 二、独立 B1 变异（关闭判据）

在**临时副本** `D:\tmp\muse3-recheck-mut`（独立 clone 后仅改一个文件）注入与上轮相同的变异：去掉 `ref_fencing.py` `_require_current` 里的 `presented_token == max_token` 比较（只保留「自己的 lease ref 存在」要求）。

```powershell
# 变异内容（ref_fencing.py:73 区块）：require_current_token(...) -> 只判 matched is None
python -m pytest apps/control-plane/backend/tests/test_ref_fencing.py `
    apps/control-plane/backend/tests/test_ref_completion.py -q -p no:cacheprovider
# => 2 failed, 23 passed in 13.54s
# FAILED test_ref_fencing.py::test_old_token_writes_rejected_when_hwm_advanced_but_old_lease_ref_present
# FAILED test_ref_completion.py::test_old_token_complete_rejected_when_hwm_advanced_but_old_lease_ref_present
```

- **恰好 2 条红 = 新增的两条 B1 回归**；其余 23 条（原有 fencing/completion 用例）仍绿。
- 正确代码（未变异）同一命令：**25 passed**。
- 结论：新增回归能杀死 R2 变异，且不依赖偶然；结合上一轮真实 GitHub 承重探针（正确代码拒绝 / 变异代码接受，6/6 CHECK PASS），**B1/R2 判定关闭**。

## 三、全量回归（最终 main）

```powershell
cd D:\tmp\muse3-recheck
python -m pytest apps/agent-worker/tests apps/control-plane/backend/tests `
    apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider -rs
# => 332 passed, 6 skipped in 75.37s（collect 338）
```

- 6 个 skipped 与上轮逐条一致，仍全部平台专用：`test_command_parse.py:107`（POSIX 默认）、`test_executor_process_tree.py:155/173/192`（POSIX_ONLY）、`test_worktree_guard.py:49/62`（POSIX 可执行位）。
- 新增 2 条 B1 回归未引入 skip/弱化：`--collect-only` = 338 = 332 passed + 6 skipped（与上轮 336 相比 +2，符合预期）。

## 四、CI 复核（PR #66）

- PR #66 quality run **`43660614802`**（pull_request，head `ad61e9b`）：**7/7 job success**，5m35s。
  - `agent-worker + control-plane 调度/分派测试`（windows）：**332 passed, 6 skipped in 71.97s**；
  - `agent-worker Linux 进程/信号测试`（ubuntu）：**102 passed, 4 skipped in 6.11s**（4 skip = 1 条 Windows 专用 + 3 条 WINDOWS_ONLY，合法）；
  - 其余 5 个既有作业 success。
- merge commit `a3ce8af`：`pr-merged-writeback`（43661256346）success、`auto-dispatch`（43661254688）success。
- main 上仍无 quality push run（auto-merge 用默认 `GITHUB_TOKEN` 合并会抑制 push 事件）——该已知行为已在矩阵 §八 明确写入（N3 关闭）；我已在 fresh main 上独立复跑 332 passed 作为最终 main 的回归证据。

## 五、N1–N4 复核

| 项 | 状态 | 证据 |
| --- | --- | --- |
| **N1** 真实 API 证据入库且无 token | **已修** | `docs/workspace/multi-worker-regression/evidence/real-github-multivm-verify.txt`（89 行）、`muse3-real-github-evidence.txt`（77 行）已入库；对两文件做 `gho_/ghp_/github_pat_/Authorization/Bearer` 扫描：**0 命中**；矩阵两处引用路径已从 `.log` 改为 `.txt`。 |
| **N2** Reaper 可运行 | **已修（可运行），cron 自动触发仍需生产验证** | 手动 dispatch run **`43660614194`**：`status=completed, conclusion=success, event=workflow_dispatch, head=main`，job「回收过期租约与孤儿范围锁」7s 成功；workflow runs 总数仍为 1（无自动 cron 运行）。设计 §13 已把「启用 Reaper cron + 失败告警、refs 生命周期/配额监控」列为解除门槛的生产前置项 —— 与我上轮建议一致。 |
| **N3** 矩阵 §八 CI 证据 | **已修** | 矩阵 §八 已回填：Windows **330 passed/6 skipped（本 PR 新增 B1 回归后 332）**、Linux 102/4；给出 run `43658004859`/`43658008019`；并新增「最终 merge commit 在 main 上没有 quality push run（GITHUB_TOKEN 抑制）+ 可用 workflow_dispatch/PAT」的说明。 |
| **N4** 陈旧注释/文档 | **基本已修，2 处轻微残留（非阻断）** | 已修：`scheduler/api.py`（去掉 `store.py/service.py/mirror.py` 与 outbox 收敛的过时描述）、`agent-worker/worker.py`（结果回传描述改为 ref 权威层直写）、`task_lifecycle.drain_mirror`（明确为兼容空操作）；矩阵 §五 `test_pagination.py::test_p01–p20` 简写已改为「`test_pagination.py`（31 例…）」。残留：① `adapters/github_issue.py:249` 文档串仍写「由 outbox 对账路径处理」（PR-5 后应为完成评论幂等/对账）；② `task_lifecycle.py:7` 模块文档串仍列 `mirror.drain`（方法已改为兼容空操作，代码本身有注释说明）；③ worker 测试的 `FakeScheduler`/`FakeMirror` 仍模拟已删除的本地 outbox（仅测试替身，不影响生产路径）。均非阻断，建议后续清理。 |

## 六、多 VM 部署门槛建议（更新）

设计 §13 的解除条件与当前状态：

| 条件 | 状态 |
| --- | --- |
| GitHub 原生 ref-CAS 权威层合入并通过回归（PR #60~#65，SQLite 权威实现删除、权威唯一） | **满足**（`4586d8b` 起；本轮 `a3ce8af` 复核） |
| T02/T04/T05/T11/T12/T15/T23/T24/T30 回归 | **满足**（最终 main 全量 332 passed；真实 API 52/52 已于上轮独立验证） |
| Muse 2 / Muse 3 独立验收 | **满足**（两份报告已入库 `muse2-final-verification.md` / `muse3-final-verification.md`；B1 已在本附录关闭） |
| PR #66（B1 回归 + 证据入库 + 文档校准）CI 全绿并合入 | **满足**（run 43660614802 7/7；merge `a3ce8af`） |
| 真实 GitHub 证据入库 | **满足**（`evidence/*.txt`，无 token） |
| owner 明确批准 | 待 owner |
| 生产前置：真实两台物理 VM 联调、启用 Reaper cron + 失败告警、refs 生命周期/配额监控 | 待生产部署前完成 |

**我的建议（更新后）**：**B1 关闭后，代码/验收侧已无阻断项；建议 owner 在完成上表「生产前置」后批准解除多 VM 部署门槛。** 解除前仍需保留设计 §13 的禁止条款；解除权在 owner，不在验收方。

## 七、复审局限

1. 本轮按任务要求**未重跑真实 GitHub 52 项探针**（上轮已在 `4586d8b` 上 52/52 PASS，本轮改动仅新增测试/文档/注释，未改权威层逻辑；ref 层代码 diff 已核对：只有注释与测试变化）。
2. Reaper 的**自动 cron 触发**仍未观察到（本机/GitHub 调度）；仅确认手动 dispatch 可成功运行。
3. 真实两台物理 VM 联调未执行（无第二台 VM），仍属生产前置 NOT TESTED；同机双 checkout + 真实 GitHub 服务端裁决是跨 VM 语义的有效证据但不等于物理多机。

## 附录：本轮命令与证据

```powershell
# 最终 main（PR #66 合并提交）
git clone --depth 1 --branch main https://github.com/Jouwala/noc-soc-intelligent-platform.git D:\tmp\muse3-recheck
cd D:\tmp\muse3-recheck; git rev-parse HEAD   # a3ce8af30fd7ab72cee851bb2847a778e35ea043

# 1) 新增两条回归通过（正确代码）
python -m pytest apps/control-plane/backend/tests/test_ref_fencing.py `
    apps/control-plane/backend/tests/test_ref_completion.py -q -p no:cacheprovider   # 25 passed

# 2) 独立 B1 变异（临时副本）
git clone D:\tmp\muse3-recheck D:\tmp\muse3-recheck-mut
#   手工去掉 ref_fencing.py 的 presented_token == max_token 比较
cd D:\tmp\muse3-recheck-mut
python -m pytest apps/control-plane/backend/tests/test_ref_fencing.py `
    apps/control-plane/backend/tests/test_ref_completion.py -q -p no:cacheprovider   # 2 failed, 23 passed

# 3) 全量回归（最终 main）
cd D:\tmp\muse3-recheck
python -m pytest apps/agent-worker/tests apps/control-plane/backend/tests `
    apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider -rs            # 332 passed, 6 skipped

# 4) CI / Reaper
gh run view 43660614802 --repo Jouwala/noc-soc-intelligent-platform   # PR#66 quality 7/7 绿
gh run view 43660614194 --repo Jouwala/noc-soc-intelligent-platform   # Reaper 手动 dispatch success
```

**证据文件**：`D:\tmp\muse3-tools\recheck-full.log`（全量回归）、`D:\tmp\final-acceptance\muse3-final-verification.md`（上轮最终验收）、`D:\tmp\final-acceptance\muse3-real-github-evidence.log/.json`（上轮 52/52）、`D:\tmp\final-acceptance\muse3-r2-window-evidence.log/.json`（R2 承重 6/6）。
