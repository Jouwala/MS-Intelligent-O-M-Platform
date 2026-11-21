# Muse 2 最终独立验收报告：GitHub 原生 ref-CAS 权威层（最终 main）

> 验收方：Muse 2（独立复核，不是修复方）　|　日期：2026-11-15
> **最终 main SHA：`4586d8b6607187b35032c832bf7bcc2593340269`**（PR #65 合并提交）
> 被测 PR：**#60 ~ #65**（PR-1~PR-6 全部合入）；PR #65 `state=MERGED`，mergedAt `2026-11-15T00:32:22Z`
> 报告位置：`D:\tmp\final-acceptance\muse2-final-verification.md`（仓库外，由 Lead 整理提交；本报告未改仓库）
> 设计依据：`docs/workspace/multi-worker-github-native-authority.md`（ADR-7）、`multi-worker-scheduler-design.md`（ADR-2026-11-14）
> 验收矩阵：`docs/workspace/multi-worker-regression.md`

---

## 一、结论摘要

**代码与验收层面：通过。建议 owner 解除 #53 的多 VM 生产部署门槛（附 §十 的 4 项前置/配套条件）。**

- ✅ **权威唯一**：SQLite 权威实现（`store/service/leasing/locking/completion/mirror/common.py`）与其专用测试已删除；`scheduler` 包只导出 `GitHubRefScheduler` / `factory` / `make_scheduler` / `make_ref_store`；`scheduler.store`、`scheduler.service` 导入即 `ModuleNotFoundError`；`factory.make_scheduler` 无 sqlite 分支。
- ✅ **T01–T30 全部 PASS**（本机 Windows + CI ubuntu；明细见 §八），其中 T02/T04/T05/T11/T12/T15/T23/T24/T30 已在 **ref 权威新层**上重跑（新层测试 + 我的独立探针 + 真实 GitHub 验证）。
- ✅ **独立探针 6/6 PASS**（自写 fake GitHub HTTP server + 两个真实子进程；不 import 对方测试）：并发认领唯一赢家、旧 token 三种写操作全 `StaleFencing`、过期租约 CAS 回收后重认领、完成幂等、HWM 单调不复用、ref 范围锁重叠/不重叠。
- ✅ **真实 GitHub 多 checkout 验证：我独立复跑官方 driver，28/28 CHECK PASS、failures=[]、cleanup PASS**（两个独立 checkout 打真实 API，隔离私库）。
- ✅ 本机全量 **330 passed, 6 skipped × 3 轮**（零 flake）；CI PR-head **330 passed/6 skipped（windows）**、**102 passed/4 skipped（ubuntu）**，全作业 success。
- ⚠️ **非阻断证据/文档缺口**：矩阵与 ADR 引用的 `docs/workspace/multi-worker-regression/evidence/real-github-multivm-verify.log` **在最终 main 中不存在**（且被 `.gitignore:157 *.log` 忽略）；设计文档 §十二/§十三 仍留 SQLite 时代措辞；矩阵 §八 CI 证据仍是上一 commit 的 271/6。详见 §九。
- ⚠️ 保留风险（非阻断，见 §九/§十三）：refs 无自动 GC（HWM 每次续租新增）、Reaper cron 可靠性、真实双机联调未做（ADR §九要求的"两个独立 checkout"已满足，但 **两台真实 VM** 的部署前联调仍缺）、W7 孤儿进程、任务继承 Worker 环境。

**不存在阻断项。** 报告最后给出明确解除建议与前置条件。

## 二、验收对象与环境

| 项 | 值 |
| --- | --- |
| 仓库 | `Jouwala/noc-soc-intelligent-platform` |
| 最终 main SHA | **`4586d8b6607187b35032c832bf7bcc2593340269`**（PR #65 合并提交） |
| PR-6 head | `c8dd86e02d3a0e0df2841113e15d4252964dde5d`；`git diff --stat c8dd86e..4586d8b` 为空（**树完全相同**） |
| 被测 PR | #60（认领）→ #61（fencing）→ #62（回收）→ #63（范围锁）→ #64（完成+Worker 切权威）→ #65（删除 SQLite，权威唯一） |
| 基线 | `7bbe5ec`（main，2026-11-14）；`7bbe5ec..4586d8b` 共 23 个提交、约 +10781/−974 |
| 本机 | Windows MSNOC1/.20，Python 3.12.10，pytest 9.1.1，gh 2.102.0 |
| Linux | GitHub Actions `ubuntu-latest`（quality 的 agent-linux-process-tests） |
| 验收方式 | fresh clone（`D:\tmp\muse2-final`，最终 main）；只读；未修改仓库源码/测试/配置；未 commit/push |

**CI 说明**：最终 merge commit `4586d8b` 在 main 上**没有** quality run —— 这是已知且文档化的行为（`agent-protocol.md:121-126`：auto-merge 用默认 `GITHUB_TOKEN` 合并时 GitHub 不再触发后续 workflow）。最终树与 PR head `c8dd86e` 完全相同，因此 CI 证据取同树的两次 PR 运行：run **43658004859**（push）与 **43658008019**（pull_request），均 7/7 作业 success；本机亦在该树上重跑全量。

## 三、方法与证据隔离

1. **不读/不导入对方测试**：独立探针只 import 生产模块（`scheduler.factory`、`rest` 传输、`ref_reap` 等），自写 fake GitHub HTTP server、假时钟、子进程竞争脚本；未使用 `sched_testkit` / `fake_github_http` / 对方 conftest。
2. **新层真并发**：S1 用**两个真实操作系统子进程**，各自完整 `RestRefStore + RestGitHubTransport` 经 TCP 打同一个本地 fake server（服务端在锁内裁决 ref 唯一性、返回 422），与多 VM 跨机语义同构。
3. **真实 GitHub**：用仓库内官方 manual driver（`apps/control-plane/backend/tests/manual/real_github_ref_verify.py`）在**两个独立 checkout**（`D:\tmp\verify-a` / `verify-b`，均为最终 main）上打真实 GitHub API，跑隔离私库 `Jouwala/noc-soc-ref-verify-muse2`；未跑生产仓库、未碰真实任务队列。
4. **失败注入**：旧 token、过期租约、Reaper CAS、重叠范围、重复完成、HWM 单调、真实 422/matching-refs 语义均真实执行，不是 Mock 断言。
5. **重复性**：全量 pytest 3 轮；真实 GitHub driver 独立复跑 1 轮；本机与 CI 结果对齐。

## 四、变更审查（PR #60–#65）

已逐文件精读 ref 权威层：

| 文件 | 审查要点 | 结论 |
| --- | --- | --- |
| `scheduler/ref_api.py` | HWM/lease/scope/reap ref 命名；lease 存活只看 epoch_ms；`require_current_token` 唯一代次校验；PR-6 删除 `AGENT_SCHEDULER_BACKEND` 常量 | 契约自洽；token 单调不复用 |
| `scheduler/github_refs.py` | `POST /git/refs` 作为唯一线性化点；POST **不重试**，异常先 GET 消歧（存在→False/安全侧，不存在→显式报错）；GET/DELETE 有限重试；list 跟随 Link 且本地按完整前缀过滤（`issue-1` 不命中 `issue-10`） | 原子性与失败语义正确 |
| `scheduler/ref_claim.py` | claim：list HWM/lease → 存活租约则 LeaseHeld → CAS `T+1` → 建 lease → **复核无其它存活租约** → 复核未被并发完成 → 删旧代 lease；失败者重读；在途窗口安全退让 | 不存在 check-then-act 双赢家；安全侧优先 |
| `scheduler/ref_fencing.py` | 所有写前 `max_token==mine 且本 attempt lease 存在`；renew = 推进 token（T→T+1）并删旧 lease；旧 token 一律 `StaleFencing`；QUEUED/RETRYABLE/终态均删 lease + 释放 scope | fencing 覆盖全部状态写 |
| `scheduler/ref_reap.py` | 过期判定 + `agent-reap/issue-N/token` 回收权 CAS（名字不含 reaper 实例，并发 Reaper 只一个赢家）；HWM 永不删；dry_run | 回收权真 CAS |
| `scheduler/ref_scope.py` | 创建后校验（create-then-verify）：建自己的 ref → 列前缀+祖先 → 对每个重叠持有者判租约存活 → 冲突则**删除自己刚建的全部 ref** 再抛 ScopeConflict；释放要求 token==max（旧 token 删不到新锁） | 不会同时保留两把有效锁 |
| `scheduler/ref_completion.py` | 完成评论 `<!-- ref-completion-id: KEY -->` 是权威幂等标记，先分页查重；同 attempt 换 key 也视为重放；标签有界重试、失败只记录（无本地 outbox） | 完成幂等且最终收敛 |
| `scheduler/ref_view.py` | refs+评论派生状态；租约被清后旧状态标记不能显示"仍在执行"；cancel_requested + 租约被回收 → cancelled（不可复活） | 派生规则安全 |
| `scheduler/ref_service.py` / `factory.py` | `GitHubRefScheduler` 组装；`pending_outbox()=[]`、`mark_outbox()` 抛错；factory **无 sqlite 分支**、缺参清晰报错 | 权威唯一 |
| `scheduler/ref_workers.py` | 注册表/心跳走 Issue 评论；无 registry issue 时本地 no-op | 展示层，不影响权威 |
| `apps/agent-worker/worker.py` / `task_lifecycle.py` | main 改为 `RestGitHubTransport → factory.make_scheduler`；删除 `--scheduler-db`；`renew()` **采纳返回的推进后 token**（后续写用新代次） | 接线正确 |
| `.github/workflows/lease-reaper.yml` | cron 5min + dispatch；`contents:write`/`issues:write`；`concurrency: lease-reaper`；timeout 10min；失败非零 | 权限最小化；无部署动作 |

**权威唯一性实测**（最终 main）：

```text
grep -rn "SchedulerService|SQLiteStore|AGENT_SCHEDULER_DB|AGENT_SCHEDULER_BACKEND"（代码/workflow/脚本）
→ 仅 2 处文本命中，均非代码引用：
  scheduler/api.py:289      # 注释「实现：service.SchedulerService」← 陈旧注释（N5）
  scheduler/ref_api.py:5    # docstring「AGENT_SCHEDULER_BACKEND / sqlite 选项随 PR-6 一并移除」← 历史说明

python: import scheduler
→ has SchedulerService=False, has SQLiteStore=False, has GitHubRefScheduler=True
→ scheduler.store / scheduler.service 均 ModuleNotFoundError
→ __all__ 只有 GitHubRefScheduler / factory / make_scheduler / make_ref_store / ref_api
```

## 五、测试执行结果（最终 main `4586d8b`）

### 5.1 全量 3 轮（本机 Windows，最终 main）

```powershell
python -m pytest apps/agent-worker/tests apps/control-plane/backend/tests `
    apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider
```

| 轮次 | 结果 | 耗时 |
| --- | --- | --- |
| 1 | **330 passed, 6 skipped** | 71.60s |
| 2 | **330 passed, 6 skipped** | 71.85s |
| 3 | **330 passed, 6 skipped** | 71.30s |
| final main 直接复跑 | **330 passed, 6 skipped** | 70.91s |

零失败、零 flake。分套件（最终树）：`apps/agent-worker/tests` **100 passed/6 skipped**；
`apps/control-plane/backend/tests` **159 passed**；`dispatch/tests` **71 passed**。

### 5.2 6 个 skip 是否仍全是 POSIX 专用

是：`test_command_parse.py:107`、`test_executor_process_tree.py:155/173/192`、
`test_worktree_guard.py:49/62` —— 全部 POSIX 语义（进程组/信号/可执行位）。
Windows 专用用例（`@WINDOWS_ONLY` 3 条 + command_parse 1 条）在 Windows 真实执行。
新增的 ref 权威层测试**没有任何 skip/xfail/Mock**（见 §5.4）。

### 5.3 CI（PR head `c8dd86e`，树与最终 main 相同）

| run | 事件 | 结论 | agent-tests（windows） | agent-linux（ubuntu） |
| --- | --- | --- | --- | --- |
| 43658004859 | push | ✅ 7/7 success | **330 passed, 6 skipped**（72.49s） | **102 passed, 4 skipped**（5.54s） |
| 43658008019 | pull_request | ✅ 7/7 success | success | success |

- 与本机结果完全一致；Linux 4 条 skip = 3 条 `WINDOWS_ONLY` + 1 条 Windows 专用命令解析用例，POSIX 进程组/SIGTERM→SIGKILL 真实执行。
- **注意**：`agent-linux-process-tests` 只跑 `apps/agent-worker/tests`，**不跑 control-plane 的 ref 权威层测试**（这些只在 windows-latest 的 agent-tests 跑）。非阻断：ref 层是纯 Python + HTTP，平台无关；如需双平台覆盖可后续加。

### 5.4 测试强度抽检（不是只看绿灯）

- 并发/幂等/分页用例 3 轮无 flake；`test_ref_concurrency.py` 用**真实子进程 + fake HTTP server**（服务端锁内 422），并含「赢家已建 HWM、未建 lease」在途窗口的**确定性交错**用例。
- fake GitHub server（`tests/fake_github_http.py`）实现服务端唯一性/422/分页/故障注入，测试走真实 TCP，不 Mock 权威逻辑。
- 矩阵中 30 个 T 行的 **41 个 `file.py::test` 引用全部存在**（脚本核对，0 缺失）。
- 无 `pytest.skip`/`xfail`/`unittest.mock` 用于绕过 ref 层断言；skip 仅 POSIX/Windows 平台语义。

## 六、独立探针（自写，不 import 对方测试）——6/6 PASS

探针：`D:\tmp\muse2-final-probes\probe_final.py`；日志：`probe_final_pr6head.log` / `probe_final.log`。
实现：本地 ThreadingHTTPServer 实现 GitHub refs/issues/comments/labels 子集（服务端锁内 ref 唯一 + 422），生产 `RestGitHubTransport` + `factory.make_scheduler` 经真实 HTTP 访问。

| 编号 | 场景 | 实测 |
| --- | --- | --- |
| **S1** | **两个真实子进程**并发 claim 同一任务 | winners=1，loser=`LeaseHeld`；HWM 只有 token=1、lease 恰好 1 条 |
| **S2** | 旧 token 的 renew / complete / release_scope | 续租 1→2；旧 token 三种写操作**全部 `StaleFencing`**；task token=2 |
| **S3** | 过期租约经 `ref_reap` CAS 回收后另一实例重认领 | reaped=1；lease 已删；HWM 保留 [1]；重认领 token=2；HWM=[1,2]（不复用） |
| **S4** | 完成幂等键重复提交 | created=[True, False]；**完成评论只有 1 条**；标签收敛 `state:in-review`；lease 已删 |
| **S5** | HWM 单调、token 不复用 | claim=1 → renew=2 → release → 重新 claim=3；HWM=[1,2,3] |
| **S6** | ref 范围锁 | 重叠并发第二个 `ScopeConflict`（自己建的 ref 回退）；不重叠可并发持有 |

> Windows 进程树 W1–W7（T09/T10/T20/T22 的 Windows 证据）见仓库内 `docs/workspace/multi-worker-regression/muse2-verification.md`；
> 其实现文件（executor.py / windows_tree.py / heartbeat.py / watchdog.py）自 `64d2458` 起**零 diff**（`git diff --stat` 为空），
> 最终树沿用同一结论，且 3 条 Windows 真跑用例在最终树全量中通过。

## 七、真实 GitHub 多 checkout 验证（我独立复跑）

- 脚本：仓库内官方 manual driver `apps/control-plane/backend/tests/manual/real_github_ref_verify.py`（默认不被 pytest 收集）。
- 两个独立 checkout：`D:\tmp\verify-a`、`D:\tmp\verify-b`（均为最终 main `4586d8b`）。
- 隔离私库：**`Jouwala/noc-soc-ref-verify-muse2`**（为本次独立复跑新建，private，不承载真实任务）。
- 命令（token 只从环境变量读，未打印/未落盘）：

```powershell
$env:GITHUB_TOKEN = (gh auth token)
$env:CHECKOUT_A='D:\tmp\verify-a'; $env:CHECKOUT_B='D:\tmp\verify-b'
python apps/control-plane/backend/tests/manual/real_github_ref_verify.py driver `
  --repo Jouwala/noc-soc-ref-verify-muse2 `
  --log D:\tmp\final-acceptance\real-github-multivm-verify.log
```

- **结果：28/28 CHECK PASS，`DRIVER done failures=[]`，f1 清理断言 PASS**（6 个测试 Issue 删除、全部测试 refs 删除）。
  覆盖：a 并发认领唯一赢家(4)、b 续租推进+旧 token renew/complete/release 被拒+释放(6)、
  c 过期租约 Reaper CAS 回收→另一 checkout 重认领(5)、d 重叠/不重叠 scope(6)、
  e 完成幂等 1 条评论+标签(4)、g 真实 matching-refs 分页/422(2)、f 清理(1)。
- 证据：`D:\tmp\final-acceptance\real-github-multivm-verify.log`（8785B）、`real-github-driver-console.log`（16KB）。
  已扫描：**无 `gho_/ghp_/github_pat_/Bearer/Authorization/GITHUB_TOKEN` 泄漏**（文件头也声明无 token/鉴权头）。

**与仓库内声明的差异（重要）**：矩阵 §四/§六与 ADR-7 §九声称证据文件
`docs/workspace/multi-worker-regression/evidence/real-github-multivm-verify.log`，但该文件
**不在最终 main**（evidence 目录只有 `baseline-defects.txt`、`probe_baseline.py`），
且根 `.gitignore:157` 的 `*.log` 会让它**无法被直接提交**。因此：仓库自带的"证据"不可检查；
**但结论本身经我用同一 driver 独立复跑验证为真**（28/28）。这份复跑日志可作为补交证据（建议转 `.txt` 或 `-f` 入库）。

另：我检查期间共享验证仓库 `Jouwala/noc-soc-ref-verify` 正被 Muse 3 并发使用（可见其 issue/lease refs），
为避免互相污染 Reaper 的全仓扫描断言，我用了独立私库；其运行结束后只剩 1 个未关闭的验证 Issue（其自身清理未完全）。
隔离库残留不影响生产，但说明 **driver 的清理只覆盖本次 run 创建的 issue，崩溃/并发会留下残留**（§九 N8）。

## 八、逐 T01–T30 判定（最终 main）

> 「新层」列标 ★ 的是任务书要求必须在 GitHub ref 权威新层上重跑的项目。
> 证据均为最终树实测（330 passed/6 skipped + 独立探针 + 真实 GitHub）。

| 编号 | 验收项（简） | 新层 | 判定 | 证据 |
| --- | --- | --- | --- | --- |
| T01 | Issue 150 条分页 | — | **PASS** | `test_pagination.py::test_p01_p02_issues_full_pagination` 等 21 条；分页适配器自 64d2458 未改 |
| T02 | Comments 150 条分页 | ★ | **PASS** | `test_pagination.py::test_p04_comments_full_pagination_150`（最终树全量通过；适配器层未改，权威层不依赖列表完整性） |
| T03 | 完成事件在第 2 页被识别 | — | **PASS** | `test_p05_completion_record_on_page_two_is_found`；ref 完成标记分页查重 |
| T04 | 同 Agent 两实例并发认领只 1 成功 | ★ | **PASS** | `test_ref_concurrency.py` 3 条（线程 8 + **两真实子进程** + 在途窗口确定性交错）；我的 **S1** |
| T05 | 不同 Agent 遵守任务唯一性 | ★ | **PASS** | `test_ref_claim.py::test_claim_other_instance_is_lease_held` 等 12 条；S2 的 LeaseHeld 语义 |
| T06 | 旧实例提交被 fencing 拒绝 | — | **PASS** | `test_ref_fencing.py` 13 条、`test_ref_completion.py::test_complete_old_token_*`；我的 **S2** |
| T07 | 200 秒心跳持续刷新 | — | **PASS** | heartbeat 实现自 64d2458 零 diff；`test_200s_*` / `test_heartbeat_continues_*` 最终树通过 |
| T08 | 30 分钟不被误杀 | — | **PASS** | `test_background_heartbeat_keeps_30min_*`、watchdog 30 分钟用例 |
| T09 | 旧 Worker 忽略 SIGTERM 后最终确认退出 | — | **PASS** | POSIX：Linux CI 真实执行；Windows：3 条真跑用例 + Muse 2 W6；实现零 diff |
| T10 | 孙进程超时后进程树全部清理 | — | **PASS** | POSIX CI + `test_windows_timeout_kills_grandchild_*`（最终树全量通过） |
| T11 | 过期租约后范围锁可回收 | ★ | **PASS** | `test_ref_reap.py` 9 条、`test_ref_scope.py::test_stale_holder_lease_does_not_block`、`test_cleanup_orphans_*`；我的 **S3** |
| T12 | 两 Worker 抢重叠范围只 1 成功 | ★ | **PASS** | `test_ref_scope.py` 17 条（含 `test_concurrent_overlapping_only_one_winner`）；我的 **S6** |
| T13 | 不重叠范围可并发 | ★ | **PASS** | `test_non_overlapping_scopes_both_acquired` 等；S6 |
| T14 | 完成评论成功、标签失败后重试修复 | — | **PASS** | `test_ref_completion.py::test_label_write_failure_is_retried_bounded`、`test_persistent_label_failure_does_not_fail_completion` |
| T15 | 完成请求重复执行幂等 | ★ | **PASS** | `test_ref_completion.py` 10 条（`complete_idempotent_posts_one_comment`、同 attempt 换 key）；我的 **S4** |
| T16 | 执行命令失败状态正确、资源清理 | — | **PASS** | `test_worker_cleanup.py::test_execution_failure_*`（worker 运行时自 64d2458 仅 renew/token 接线改动） |
| T17 | Hook 校验失败状态正确、资源清理 | — | **PASS** | `test_blocked_hooks_releases_everything` |
| T18 | 缺少执行命令状态正确、资源清理 | — | **PASS** | `test_blocked_no_command_releases_lease_and_scope` |
| T19 | 范围锁等待超时无锁残留 | — | **PASS** | `test_blocked_scope_timeout_releases_lease` + ref 层 `test_stale_holder_lease_does_not_block` |
| T20 | 执行超时进程树退出、资源清理 | — | **PASS** | `test_timeout_terminates_process_tree_*` + Windows 孙进程真跑用例 |
| T21 | 内部异常不失控且保留原始异常 | — | **PASS** | `test_internal_exception_cleans_then_reraises`、`test_failure_inside_runner_*` |
| T22 | 外部取消及时停止且不提交结果 | — | **PASS** | `test_cancel_terminates_process_and_never_completes` + Windows cancel 真跑 |
| T23 | Worker 崩溃后租约/锁/任务可恢复 | ★ | **PASS** | `test_worker_integration.py::test_github_ref_scheduler_expired_lease_reaped_then_reclaimed`（真实 ref 层）；我的 **S3** |
| T24 | 两个 auto-dispatch 同时启动不重复分派 | ★ | **PASS** | dispatch 层（PR #52 ref CAS）未改；`test_two_concurrent_dispatch_runs_register_once` 等最终树通过 |
| T25 | Workflow 重复触发幂等 | — | **PASS** | `test_repeated_event_delivery_is_idempotent`、`test_trigger_once_then_idempotent` |
| T26 | API 成功但客户端超时不产生重复副作用 | — | **PASS** | `test_comment_post_timeout_after_apply_does_not_duplicate` 等 |
| T27 | Linux Hook 权限 644 不得误报通过 | — | **PASS** | Linux CI 真执行；Windows 显式跳过并断言「跳过」 |
| T28 | Linux 主机信息与 capabilities 正确 | — | **PASS** | `test_hostinfo.py`；Linux CI |
| T29 | Windows 风格路径按规范处理 | — | **PASS** | `test_command_parse.py`；Linux CI 跑 POSIX 分支 |
| T30 | 多项故障连续后状态与资源收敛 | ★ | **PASS** | `test_worker_integration.py::test_github_ref_scheduler_end_to_end`、`test_worker_recovery.py`、`test_ref_completion.py`；我的 S1–S6 |

**汇总：T01–T30 全部 PASS（30/30），无 FAIL；平台限制项见 §十二（真实双机部署联调 BLOCKED）。**

## 九、发现的问题（无阻断；均为非阻断）

| # | 级别 | 问题 | 位置 |
| --- | --- | --- | --- |
| N1 | 证据缺口（非阻断） | 矩阵/ADR 引用的真实 GitHub 证据文件不存在于最终 main；且 `.gitignore *.log` 使之无法直接入库。我已用同一 driver 在独立私库独立复跑 **28/28 PASS**，结论可信但仓库证据缺失。建议把脱敏日志转 `.txt`（或 `git add -f`）提交。 | `docs/workspace/multi-worker-regression.md:62-63,127`；`docs/workspace/multi-worker-github-native-authority.md:207-208,230-232`；`.gitignore:157` |
| N2 | 文档过时 | 设计文档 §十二/§十三 仍写「当前实现是单机 SQLite」「多 VM 部署 BLOCKED 直到 ref-CAS 合入」，与最终 main 不符；§十二 item 4 的「异步 outbox/mirror」在 GitHub 权威层已不存在。§零顶部有 PR-6 说明但未回改这些节。 | `docs/workspace/multi-worker-scheduler-design.md:232-236,260-275` |
| N3 | 文档过时 | 矩阵 §八 CI 证据仍是 `64d2458` 的 271/6；应为最终树的 330/6（run 43658004859/43658008019）。 | `docs/workspace/multi-worker-regression.md:171-180` |
| N4 | 陈旧注释 | `api.py` 注释指已删除的 `service.SchedulerService`；`task_lifecycle.py` / `worker.py` 文档/日志仍提 `GitHubMirror.drain`/outbox（GitHub 后端 mirror=None、无 outbox，实际 no-op）。建议清理，避免维护者误解。 | `scheduler/api.py:289`；`apps/agent-worker/task_lifecycle.py:268,271`；`apps/agent-worker/worker.py:12` |
| N5 | CI 证据链 | 最终 merge commit 在 main 上无 quality run（GITHUB_TOKEN 合并事件被 GitHub 抑制，文档已记载）；证据取自同树 PR-head run。建议在合并后手动 dispatch 一次 main quality 或由 owner 接受同树证据。 | `agent-protocol.md:121-126`、`github-automation.md:40` |
| N6 | 运维/规模化风险 | **refs 无自动 GC**：HWM 每次 renew 新增一条且永不删除；dispatch/reap/scope refs 同样只增；真实 GitHub `matching-refs` **不分页**、单次返回全部匹配 → 长任务多、续租频繁时 list 响应/延迟/API 配额会持续放大（ADR §七/§十.3 已注「清理策略后续制定」）。建议上线前定 GC/归档策略与监控。 | `ref_fencing.py:157-227`、`ref_api.py:105-112`、`github_refs.py:295-349` |
| N7 | 运维风险 | Reaper 依赖 GitHub Actions schedule（可能延迟/被禁用/需默认分支）；若 Reaper 停摆，过期租约不被回收（任务卡住）。建议加失败告警、`workflow_dispatch` 预案与「最老 lease 年龄」监控。 | `.github/workflows/lease-reaper.yml` |
| N8 | 验证 harness | 官方 driver 的清理只覆盖**本次 run 创建的 issue**；崩溃/并发运行会在隔离仓库留下 refs/Issue（实测共享库出现过 issue-30 refs 与未关闭 Issue）。隔离库不影响生产，但建议按 `run_id`/前缀做崩溃安全清理。 | `tests/manual/real_github_ref_verify.py:430-448` |
| N9 | 平台限制 | 真实**两台物理 VM** 联调未做：我与 Muse 3 都是「同机两个独立 checkout + 真实 API」（ADR §九要求的最小验证形式已满足），但部署前双机联调仍是部署步骤必需项。 | ADR §九 / `agent-protocol.md:180-181` |
| N10 | 历史风险（保留） | W7：Worker 根进程自行退出后遗留子进程，看门狗重启不清理（既有非阻断项，design §十二 item 5 已记录）；另「任务命令继承 Worker 环境」风险（Muse 3 N4，design §十二 item 6 已记录）。 | `apps/agent-worker/watchdog.py`、`apps/agent-worker/executor.py` |
| N11 | 配置小项 | `lease-reaper.yml` 用 `secrets.GITHUB_TOKEN`（可用），建议统一为 `${{ github.token }}` 口径；权限已最小化（contents/issues write）+ concurrency + timeout。 | `.github/workflows/lease-reaper.yml:46` |

## 十、多 VM 部署门槛（#53）检查与解除建议

### 10.1 门槛仍生效

- PR **#53**（docs，2026-11-14 23:21 合入）确立「多 VM 生产拓扑禁止部署」门槛；
- 最终 main 中门槛文档仍在：`multi-worker-scheduler-design.md` §十三、`agent-protocol.md` §8.4、`apps/agent-worker/README.md`；
- 验收矩阵把「真实多主机（跨机器）生产部署」标为 **BLOCKED（部署门槛）**，写明「代码已跨机可用并有真实 API 证据；但生产多 VM 部署仍需 owner 批准解除 #53 门槛」（`multi-worker-regression.md:128`）；
- 解除条件（#53/design §十三）：① ref-CAS 权威层落地；② T02/T04/T05/T11/T12/T15/T23/T24/T30 回归 + ③ **Muse 2 / Muse 3 独立验收通过**；④ **owner 批准**。

### 10.2 我的判断：**建议解除（附 4 项前置/配套条件）**

**理由（代码/验收层面已满足 ①–③）**：

1. 权威唯一已实现并可证明（SQLite 代码/测试/依赖已删、runtime 无第二权威、factory 无 sqlite 分支）；
2. 新层协议正确性：本机 330/6 × 3 轮零 flake；CI windows 330/6、ubuntu 102/4 全绿；
3. 我的独立探针 6/6（含两个真实子进程竞争 + 旧 token 全拒 + CAS 回收 + 幂等 + HWM 单调 + scope）；
4. 真实 GitHub 多 checkout **28/28 CHECK PASS**（我独立复跑，含真实 422/matching-refs 语义与清理）；
5. 真实 GitHub 上的原子性来自 GitHub 服务端 ref 名唯一性（POST 422），与 fake server 同构，协议可证明跨 VM 互斥。

**解除前/同时必须落实（否则不建议以「生产多 VM」名义上线）**：

1. **补交证据文件**：把真实 GitHub 28/28 脱敏日志以 `.txt`（或 `-f`）提交入库（N1）；或由 owner 接受本报告的独立复跑证据。
2. **部署前双机联调**：两台真实 VM 各跑 Worker 做一次生产拓扑联调（当前只有同机两个 checkout + 真实 API；N9）。
3. **Reaper 可观测性**：确认 `lease-reaper` cron 在生产仓库启用并有失败告警（N7）；确认租约 TTL 10min 与 Worker 轮询/续租间隔匹配。
4. **refs 生命周期与配额**：定 HWM/dispatch/reap/scope refs 的清理/归档策略与 API 限流监控（5000 次/小时/身份；N6）。

**若 owner 把「证据文件入库」视为解除硬条件**：先完成第 1 项再解除；除此之外，无代码或验收层面的阻断理由。

## 十一、安全检查

| 检查项 | 结论 | 证据 |
| --- | --- | --- |
| `shell=True` / `os.system` / `os.popen` / `eval` / `exec` | **无实际调用**（仅注释/测试文档表述"永不 shell=True"） | 全量 grep 新增/修改的 scheduler、worker、dispatch 代码 0 命中 |
| 命令注入面 | 命令解析为 argv 列表（JSON argv 优先 + 平台感知 shlex），执行器 `Popen(list)`，永不 shell 解释 | `command_parse.py`、`executor.py` |
| token/secret 泄漏 | ref 层 token 只放 `Authorization` 头；调度器/Reaper/driver **不打印 token**；真实验证日志无 token/鉴权头（已扫描）；`lease-reaper` 用 `secrets.GITHUB_TOKEN` 不落日志 | `github_refs.py:152-162`、`lease_reaper.py`、`real_github_ref_verify.py`、我的 log 扫描 |
| 越权部署 | 新增/修改 workflow 无 ssh/scp/kubectl/云部署/生产写操作；`lease-reaper` 只删过期 ref/孤儿 scope；生产部署仍受 #53 门槛约束 | `.github/workflows/*.yml` |
| Workflow 触发安全 | 无 `pull_request_target`；权限最小化；`lease-reaper` 有 concurrency 与 timeout | 同上 |
| 旧实例写入 | 新层 fencing：任何写前 `max_token==mine 且本 attempt lease 存在`；旧 token renew/complete/release_scope 全 `StaleFencing`（S2 + 真实 GitHub b2/b3） | `ref_fencing.py` |
| 范围锁路径安全 | 沿用 `scopes.py` 规范化：反斜杠/`.`/`..`/绝对路径/UNC 拒绝；ref 名按段安全编码 | `ref_api.py:241-252`、`scopes.py` |
| 真实操作边界 | 只操作隔离私库 `noc-soc-ref-verify-muse2`；未碰生产仓库/真实任务队列/凭据；未部署 | 本报告执行记录 |

## 十二、未执行 / 受限项（如实标注）

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 真实**两台物理 VM** 部署联调 | **NOT TESTED / BLOCKED** | 本机只有一台 Windows；ADR §九要求的「两个独立 checkout + 真实 API」已由我完成，但"两台 VM"属部署步骤，需 owner 授权与环境 |
| 真实 GitHub Actions 上两个 workflow 真并发 | NOT TESTED | 无 act/runner、无法在同一仓库制造真实并发触发；dispatch 层用 ref CAS 假 API 覆盖 |
| 生产训练机部署 | **BLOCKED** | #53 门槛 + 任务书 §15：需 owner 授权；第三方不部署 |
| 真实任务队列/生产 Worker 凭据破坏性测试 | BLOCKED | 全程隔离测试环境 |
| Linux 上运行 control-plane ref 层测试 | NOT TESTED（本机无 Linux） | CI 的 ubuntu 作业只跑 agent-worker 测试；ref 层测试仅 windows-latest 执行（纯 Python/HTTP，平台无关，非阻断） |
| 长时压力/大规模 refs（数千 refs）分页行为 | NOT TESTED | 真实库只做了小规模 28 CHECK；规模化性能与限流属上线后观察项（N6/N7） |

## 十三、验收建议

1. **代码与验收：建议通过**（最终 main `4586d8b`；T01–T30 全 PASS；权威唯一满足 ADR-7 §零）。
2. **多 VM 门槛：建议 owner 解除 #53**，并落实 §十 的 4 项前置/配套条件（证据入库、双机联调、Reaper 监控、refs 生命周期/配额）。
3. 合并后整理：把本报告 + 我的真实 GitHub 日志（脱敏）与 §九 的 N1–N4 文档修正一起提交；修正后重跑文档守卫。
4. 保留跟踪（非阻断）：N6 refs GC、N7 Reaper 可靠性、N8 driver 清理、N10 W7/环境继承。

## 十四、证据清单

| 文件 | 说明 |
| --- | --- |
| `D:\tmp\muse2-final\` | fresh clone，checkout 最终 main `4586d8b`（`git status` 干净） |
| `D:\tmp\verify-a\`、`D:\tmp\verify-b\` | 两个独立 checkout（最终 main）用于真实 GitHub 验证 |
| `D:\tmp\muse2-final-probes\probe_final.py` | 自写独立探针 S1–S6（fake GitHub HTTP server + 真实子进程） |
| `D:\tmp\muse2-final-probes\probe_final_pr6head.log`、`probe_final.log` | 探针原始输出（6/6 PASS） |
| `D:\tmp\final-acceptance\real-github-multivm-verify.log` | **我独立复跑的真实 GitHub 多 checkout 日志（28/28 PASS，无 token/鉴权头）** |
| `D:\tmp\final-acceptance\real-github-driver-console.log` | driver 完整控制台输出 |
| `D:\tmp\final-acceptance\muse2-final-verification.md` | 本报告 |
| 本地全量测试 | `330 passed, 6 skipped` × 3 轮 + final main 直接复跑 1 轮；分套件 100/159/71 |
| CI | run `43658004859`（push）、`43658008019`（pull_request），均 7/7 success；windows 330/6，ubuntu 102/4 |
| 真实 GitHub 隔离库 | `Jouwala/noc-soc-ref-verify-muse2`（private；验证后 refs/Issue 已清理，仅剩 main 分支） |

---

**最终判定：最终 main `4586d8b` 通过独立验收（T01–T30 30/30 PASS；权威唯一；真实 GitHub 28/28 独立复跑通过）。建议 owner 在落实 §十 的 4 项前置/配套条件后解除 #53 多 VM 部署门槛。**
