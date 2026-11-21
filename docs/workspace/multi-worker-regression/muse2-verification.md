# Muse 2 独立验收报告：多 Worker 调度系统修复（T01–T30）

> 验收方：Muse 2（独立复核，不是修复方）　|　报告日期：2026-11-14
> 初验 commit：`0a91985`（发现阻断 B1）　|　**复验 commit：`64d2458`（B1 已修复，建议通过）**
> 设计文档：`docs/workspace/multi-worker-scheduler-design.md`（ADR-2026-11-14）
> 验收矩阵：`docs/workspace/multi-worker-regression.md`
> 基线证据：`docs/workspace/multi-worker-regression/evidence/baseline-defects.txt`

## 一、结论摘要

**复验结论（64d2458）：建议通过。** B1（Windows 进程树清理假阳性）已修复并经我独立探针验证；其余 T01–T30 结论维持、无新增阻断项。**初验 commit 0a91985 本身不通过**（存在阻断 B1），结论按 commit 分开记录。

- ✅ 调度器核心（原子认领 / fencing / 租约 / 范围锁 / 幂等完成 / outbox 修复 / 分页 / 分派 CAS）在本机与 CI 上均通过独立验证，**T01–T08、T11–T19、T21–T30 通过**。
- ✅ **T09 / T10 / T20 / T22 的 Windows 能力**：0a91985 上由我首次独立复现为阻断（W1–W5），64d2458 修复后同一组探针 **W1–W3 全部转为 PASS**（孙进程确实消失、`tree_exited=True` 可信、超时路径不再多等 5 秒）。
- ⚠️ 初验时矩阵把 T20 的 Windows 列写成 `PASS`、T09 写成 `PASS(Windows taskkill 路径)`，而仓库测试只覆盖直接子进程 / `FakeProcess(pid=None)`，属过强声明；修复 commit 已按实际证据更正矩阵并新增 3 条 Windows 真跑用例（我已做变异自检，见 §5.6）。
- ⚠️ **未解决风险（非阻断，保留）**：W7——Worker 根进程自行退出（崩溃）后遗留的孤儿子进程，看门狗重启时不会清理（0a91985 与 64d2458 均如此）。不属 T09/T10/T20 验收口径，但在 Windows 上仍是进程泄漏风险，见 §8 N4 与 §11。
- 安全项（无 `shell=True`、无 token 泄漏、无越权部署、fencing 阻止旧实例回写）通过，见 §9。

> 说明：0a91985 的验证全部在 `git archive 0a91985` 的**只读快照**上执行（见 §三），因为初验期间修复方已在同一工作树继续编辑 `executor.py`；复验 64d2458 同样用 `git archive 64d2458` 快照（`D:\tmp\muse2-pinned2`）。两个快照目录均未改动，仓库代码未被我修改。

## 二、验收对象与环境

| 项 | 值 |
| --- | --- |
| 仓库 | `D:\tmp\noc-soc-fix`（NOC-SOC 多 Worker 修复仓） |
| 分支 | `fix/multi-worker-scheduler` |
| 验收 commit | 初验 `0a91985f…` → **复验 `64d2458dd828eb107e9ac8853f31d486228b3a7c`**（两者均与远程分支 head 一致） |
| 基线 commit | `7bbe5ec`（main，2026-11-14） |
| 本机 | Windows（MSNOC1 / .20），Python 3.12.10，pytest 9.1.1，gh 2.102.0 |
| CI | 初验 run `43646693148`（0a91985）✅；**复验 run `43648075661`（64d2458）✅ conclusion=success** |
| 验证范围 | 只读；未 commit / push / checkout；仓库内仅写入本文件 |

初验期间（0a91985）工作树出现过的在途改动（**不属于 0a91985**，仅说明为何用快照；现已在 64d2458 提交，当前工作树干净）：

```
 M apps/agent-worker/executor.py
 M apps/agent-worker/tests/conftest.py
 M apps/agent-worker/tests/test_executor_process_tree.py
?? apps/agent-worker/windows_tree.py
```

## 三、方法：独立证据与隔离

1. **不读、不导入第三方测试**：独立探针只 import 生产模块，自写假时钟 / 假 GitHub 传输 / 假 HTTP 分页 / 假调度器替身，不使用 `sched_testkit.py`、`fake_github.py` 或对方 `conftest.py`。
2. **冻结验收 SHA**：`git archive --format=zip -o snapshot-<sha>.zip <sha>` 后解压到只读快照目录（0a91985 → `D:\tmp\muse2-pinned`，64d2458 → `D:\tmp\muse2-pinned2`），所有结论都在对应快照上重跑；探针通过 `MUSE2_REPO` 环境变量指向快照。
3. **失败注入**：分页第 2 页持续 500、评论 POST 已生效但客户端超时、标签更新 500、租约过期、多进程竞争、进程树泄漏均真实执行，不是 Mock 断言。
4. **重复性**：全量 pytest 在两个快照上各重复 3 轮；并发分派探针重复 3 轮；并发认领用 4 个真实子进程同时竞争同一 SQLite 文件。
5. **变异自检**：在 64d2458 的副本（`D:\tmp\muse2-mutation`，不影响仓库）上人为去掉 `/F`、退化作进程存活检测、把终止函数改成 no-op，确认新增 Windows 用例会变红（§5.6）。

证据目录：`D:\tmp\muse2-probes\`（探针源码 + `pinned-*.log` 原始输出）。

## 四、实际执行结果

### 4.1 三套 pytest（0a91985 快照，Windows 本机）

```powershell
python -m pytest apps/agent-worker/tests -q -rs -p no:cacheprovider
python -m pytest apps/control-plane/backend/tests -q -p no:cacheprovider
python -m pytest apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider
```

| 套件 | 结果 | 耗时 |
| --- | --- | --- |
| `apps/agent-worker/tests` | **97 passed, 6 skipped** | 30.15s |
| `apps/control-plane/backend/tests` | **100 passed** | 2.28s |
| `apps/control-plane/backend/dispatch/tests` | **71 passed** | 0.21s |
| 合计（274 collected） | **268 passed, 6 skipped** | 见下 |

### 4.2 全量 3 轮（查 flake，快照上）

| 轮次 | 结果 | 耗时 |
| --- | --- | --- |
| 1 | 268 passed, 6 skipped | 29.91s |
| 2 | 268 passed, 6 skipped | 34.65s |
| 3 | 268 passed, 6 skipped | 32.03s |

三轮零失败零 flake；并发/幂等/分页用例（`test_t02/t03`、`test_s09`、`test_c11`、`test_p0x`、`test_*concurrent*`）每轮均通过。

### 4.2b 复验全量 3 轮（`64d2458` 快照，含新增 3 条 Windows 用例）

```powershell
python -m pytest apps/agent-worker/tests apps/control-plane/backend/tests \
    apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider
```

| 轮次 | 结果 | 耗时 |
| --- | --- | --- |
| 1 | **271 passed, 6 skipped** | 34.56s |
| 2 | **271 passed, 6 skipped** | 33.01s |
| 3 | **271 passed, 6 skipped** | 32.51s |

单套：`apps/agent-worker/tests` = **100 passed, 6 skipped**（较初验 +3 条 Windows 真跑用例），
调度器 100 passed、分派 71 passed 未变。三轮零 flake。

### 4.3 6 个 skip 是否全是 POSIX 专用

全部是，无被跳过的 Windows 应测项：

| # | 位置 | 原因 |
| --- | --- | --- |
| 1 | `test_command_parse.py:107` | POSIX 默认 `posix=True`，Windows 不适用 |
| 2 | `test_executor_process_tree.py:99` | POSIX 进程组 / SIGTERM→SIGKILL 语义 |
| 3 | `test_executor_process_tree.py:117` | POSIX 孙进程回收语义 |
| 4 | `test_executor_process_tree.py:136` | POSIX 根退出但进程组仍活语义 |
| 5 | `test_worktree_guard.py:49` | POSIX pre-commit 可执行位 |
| 6 | `test_worktree_guard.py:62` | POSIX pre-commit 可执行位 |

CI Linux 侧 102 passed / 1 skipped，唯一 skip 是 `test_command_parse.py:102`（Windows 专用用例），与上表互为镜像，**说明 6 个 POSIX 用例在 ubuntu 上真实执行**。

复验 commit `64d2458` 新增 3 条 `@WINDOWS_ONLY` 用例后：Windows 侧仍 **6 skipped**（同一批 POSIX 用例），Linux 侧变为 **4 skipped**（上述 3 条 Windows 用例 + `test_command_parse.py:102`），说明新增用例在 windows-latest 真跑、在 ubuntu 上按语义跳过。

### 4.4 CI（GitHub Actions）

`gh run view 43646693148 --repo Jouwala/noc-soc-intelligent-platform`：

| 作业 | 结论 | 关键日志 |
| --- | --- | --- |
| `agent-tests`（windows-latest, job 132605976097） | ✅ success | `268 passed, 6 skipped in 26.99s`（与本机一致） |
| `agent-linux-process-tests`（ubuntu-latest, job 132605976038） | ✅ success | `102 passed, 1 skipped in 4.75s` |
| 其余 5 作业（compileall / ruff / portal / my_app-docs / my_app pytest） | ✅ success | — |
| 整次 run | ✅ **completed / success**，head_sha=`0a91985…` | run URL：`https://github.com/Jouwala/noc-soc-intelligent-platform/actions/runs/43646693148` |

另有一次 49s 的 run `43646681130` 被用户取消（相关作业 conclusion=`cancelled`，非失败）；当前有效证据以上表为准。

**复验 run `43648075661`（head_sha=`64d2458…`）**：整次 run **completed / success**，7 个作业全部 success；
`agent-tests`（windows-latest）日志 **`271 passed, 6 skipped in 33.09s`**（含 3 条新增 Windows 真跑用例）；
`agent-linux-process-tests`（ubuntu-latest）日志 **`102 passed, 4 skipped in 4.76s`**（4 = 3 条 Windows-only + 1 条 Windows-only 命令解析用例）。
URL：`https://github.com/Jouwala/noc-soc-intelligent-platform/actions/runs/43648075661`。

> Linux 结论来自 CI 日志，不是本机（本机无 Linux/WSL/Docker）。

## 五、独立探针结果（不依赖对方测试）

### 5.1 调度器（`probe_scheduler.py`，快照上 5/5 PASS）

| 编号 | 场景 | 实测 |
| --- | --- | --- |
| P1 | **4 个真实进程**同时 claim 同一任务 | winner=1，3 个 `LeaseHeld`；DB 内 1 条 lease、1 条 attempt、1 条 claim 审计；token=1 |
| P2 | 租约过期被新实例接管后，旧令牌提交结果 | `complete/transition/renew/release_scope` **4 条写路径全部 `StaleFencing`**；新 attempt 保持 claimed |
| P2b | 租约过期但**尚未被接管**时旧实例完成（设计声明） | `created=True`（与 completion.py 第 7–15 行声明一致，属有意语义） |
| P3 | 过期范围锁回收 | 过期后 `scope_holders()` 立即不再返回该锁；`reap_expired` 删租约+锁、任务落 `expired`、重复回收幂等；新实例拿到同一范围；旧实例 `release_scope` 被 `StaleFencing` |
| P4 | 评论成功、标签失败后 drain 修复 | 首次 drain：comment+audit 成功、labels=error（评论 1 条）；再次 drain：labels 修复、评论仍 1 条、outbox 清空；同幂等键重放 `created=False`、无新 outbox |

### 5.2 分页（`probe_pagination.py`，自写假 HTTP，5/5 PASS）

| 编号 | 场景 | 实测 |
| --- | --- | --- |
| P5a | 150 条评论 | 读到 150 条，2 次请求（Link 翻页） |
| P5b | 250 条 Issue（内含 10 条 PR） | 读到 240 条 Issue（PR 被过滤），3 次请求 |
| P5c | 服务端不给 Link | 按「满页继续」兜底读到 150 条 |
| P5d | 第 2 页持续 500 | 显式 `TransportError`，**不返回半截**；page2 请求共 4 次（1+3 重试） |
| P5e | 跨页重复 id | 121 条原始 → 去重 120 条 |

### 5.3 分派 CAS（`probe_dispatch.py`，自写线程安全假 GitHub RPC，6/6 PASS）

| 编号 | 场景 | 实测 |
| --- | --- | --- |
| P6a | 两个 auto-dispatch 并发（3 轮） | 每轮 1 个 ref、1 条 TASK_ASSIGNED，状态 {dispatched, reconciled}，3/3 一致 |
| P6b | 同一触发重复投递 | 第二次 `reconciled`，ref=1、评论=1（不派第二个任务） |
| P6c | 分派评论落在第 2 页 | 分页扫描能识别（true） |
| P6d | dispatch_id 稳定性 | 同输入相等，来源/目标不同则不等 |
| P6e | **评论 API 已生效但客户端超时** | 对账后 `reconciled`，评论仍 1 条（不重复） |
| P6f | 超时且确实没写入 | 先对账、补发仍失败则**显式抛错**，不静默当成功 |

### 5.4 Worker 清理 / 端到端（`probe_worker.py`，真实 SQLite SchedulerService，7/7 PASS）

| 编号 | 场景 | 实测 |
| --- | --- | --- |
| P7a | 执行失败清理 | 租约/锁均释放，worktree 保留，清理无告警 |
| P7b | 取消且子进程确认退出 | worktree 被强制回收（force=True） |
| P7c | 子进程树未确认退出 | **不删 worktree** 并记 `未确认退出` 告警 |
| P7d | 清理自身异常 | 只收集 2 条错误、**不外抛**、不覆盖原始异常 |
| P8 | 真实 SchedulerService + Worker + Mirror 端到端 | `completed`；completion=1；outbox 清空；1 条 TASK_COMPLETED（含 completion-id）；标签 `state:in-review`；租约/锁归零 |
| P9 | Worker 层执行超时（仓库测试只测了 executor 层） | `timeout` → `failed`；租约/锁释放；诊断产物保留；评论 TASK_FAILED |
| P10 | Hook 校验失败（真实调度器，仓库仅有 FakeScheduler 版） | `blocked-hooks` → 租约/锁全释放、`_active` 清空、无清理告警 |

> 以上 P1–P10 在复验快照 **64d2458** 上全部重跑通过（5/5、5/5、6/6、7/7，共 23 项），
> 结论与 0a91985 一致；调度器/分页/分派代码在修复 commit 中未变，属回归确认。

### 5.5 Windows 进程树（`probe_windows_tree.py`，真实进程 + ctypes 存活判定）

| 编号 | 场景 | 实测 | 判定 |
| --- | --- | --- | --- |
| **W1** | 执行超时（根+孙进程） | `killed_reason=timeout`，**`tree_exited=True` 但孙进程仍存活**（0.5s 后仍活），executor 耗时 5.86s | ❌ 缺陷(0a91985) → ✅ 已修(64d2458) |
| **W2** | 根进程正常退出、孙进程残留 | `exit_code=0`，**`tree_exited=True` 但孙进程存活** | ❌ 缺陷(0a91985) → ✅ 已修(64d2458) |
| **W3** | 外部取消 | `killed_reason=cancelled`，**`tree_exited=True` 但孙进程存活** | ❌ 缺陷(0a91985) → ✅ 已修(64d2458) |
| W4 | 对照：`taskkill /T` 不带 `/F` | rc=128 且无输出，根与孙**都没被杀** | 机制证据 |
| W5 | 对照：先只杀根，再 `/F /T` | rc=128 `ERROR: The process "…" not found.`，孙进程存活 | 机制证据 |
| W6 | 看门狗停止 Worker（T09 Windows） | `_stop()` 确认根退出，日志 `SIGTERM → 升级 SIGKILL`，**Worker 子树（孙进程）也被清掉** | ✅ T09 Windows 通过（来自本探针） |
| W7 | Worker 根进程自行退出后的孤儿子进程 | 看门狗重启 2 次，**孤儿孙进程仍存活**（非 T09 验收项；0a91985 与 64d2458 均如此） | ⚠️ 未解决风险（保留） |

> 原始输出：`D:\tmp\muse2-probes\pinned-probe_windows_tree.log`（0a91985）与
> `D:\tmp\muse2-probes\pinned2-probe_windows_tree.log`（64d2458）。（探针源码同目录。）

### 5.6 阻断 B1 修复复验（`64d2458`，同一组探针重跑）

| 编号 | 0a91985 | 64d2458 实测 | 判定 |
| --- | --- | --- | --- |
| W1 超时 | tree_exited=True 但孙进程存活，耗时 5.86s | `tree_exited=True`，孙进程 pid=864 **立即/0.5s 后均不存活**，耗时 **0.78s**（不再被管道拖 5s） | ✅ **PASS** |
| W2 根退出遗留 | tree_exited=True 但孙进程存活 | `exit_code=0`、`tree_exited=True`、孙进程 pid=19796 **不存活** | ✅ **PASS** |
| W3 取消 | tree_exited=True 但孙进程存活 | `killed_reason=cancelled`、`tree_exited=True`、孙进程 pid=24524 **不存活** | ✅ **PASS** |
| W4/W5 | taskkill 语义对照 | 与初验一致（仍记录旧机制） | 对照有效 |
| W6 看门狗停止 | 根死、子树死 | 根死、子树死（不变） | ✅ PASS |
| W7 崩溃孤儿 | 孤儿存活 | 孤儿仍存活 | ⚠️ 未解决（非阻断，见 §11） |

**新增用例真实通过（本机与 CI）**：`test_windows_timeout_kills_grandchild_and_tree_exited_true`、
`test_windows_cancel_kills_grandchild_and_tree_exited_true`、
`test_windows_root_exit_leaving_orphans_is_cleaned` 在本机 `-k windows` 为 **3 passed**，
CI windows-latest 亦通过（整体 271 passed）。

**变异自检（在 64d2458 的副本 `D:\tmp\muse2-mutation` 上做，不改仓库）**：

| 变异 | 结果 | 说明 |
| --- | --- | --- |
| A：`kill_tree` 去掉 `/F`（退回 `taskkill /T`） | **3/3 新用例失败** | 证明用例能抓住原始缺陷 |
| B：`alive_pids` 恒返回空集（永不判残留） | 1/3 失败（根退出遗留用例） | 证明检测退化会被抓 |
| C：`kill_tree` 完全不杀进程 | **3/3 新用例失败**（`tree_exited=False`，summary 带"进程树未确认退出"） | 证明"清理失败必须 tree_exited=False、不得假阳性" |

> 对照：未变异副本同一组用例 3 passed，确认变异结果不是环境噪声。

## 六、逐 T01–T30 判定

> 「独立复核」= 本报告 §五 探针实测；「仓库测试」= §四 实测；Linux 列依据 CI 日志。

| 编号 | 验收项（简） | 本地实测 | Linux | 独立复核 | 判定 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| T01 | Issue 150 条分页 | PASS | — | PASS（P5b 250 条/3 页） | **PASS** | `test_p01/p02`；P5b |
| T02 | Comments 150 条分页 | PASS | — | PASS（P5a 150 条） | **PASS** | `test_p04`；P5a |
| T03 | 完成记录在第 2 页能识别 | PASS | — | PASS（P6c 第 2 页分派评论；P5e 去重） | **PASS** | `test_p05`、`test_p19`；P6c |
| T04 | 同 Agent 两实例并发认领只一个成功 | PASS | — | PASS（P1 4 进程） | **PASS** | `test_t02/t03`；P1 |
| T05 | 不同 Agent 遵守任务唯一性 | PASS | — | PASS（P2 token 推进后旧写全拒） | **PASS** | `test_t05`；P2 |
| T06 | 旧实例提交被 fencing 拒绝 | PASS | — | PASS（P2 4 条写路径） | **PASS** | `test_c05`、`test_t07`；P2 |
| T07 | 200 秒心跳持续刷新 | PASS | — | —（假时钟语义已足够） | **PASS** | `test_200s_*`、`test_heartbeat_continues_*` |
| T08 | 30 分钟不被误杀 | PASS | — | — | **PASS** | `test_background_heartbeat_keeps_30min_*`、`test_fresh_at_heartbeat_*` |
| T09 | 旧 Worker 忽略 SIGTERM 后最终确认退出 | PASS\* | PASS | PASS（W6 真实进程：根死+子树清） | **PASS** | Linux CI；W6（\*仓库测试仅 FakeProcess，见 N2；64d2458 新增 Windows 真跑用例） |
| T10 | 孙进程超时后进程树全部清理 | SKIP(POSIX) | PASS | 0a91985 **FAIL** → 64d2458 **PASS**（W1–W3 孙进程消失） | **PASS（64d2458 修复后）** | Linux CI；W1–W3、§5.6 |
| T11 | 租约过期后范围锁可回收 | PASS | — | PASS（P3） | **PASS** | `test_s06/s07`；P3 |
| T12 | 两 Worker 抢重叠范围只一个成功 | PASS | — | — | **PASS** | `test_s02/s09` |
| T13 | 不重叠范围可并发 | PASS | — | — | **PASS** | `test_s01/s10` |
| T14 | 评论成功、标签失败后重试修复 | PASS | — | PASS（P4） | **PASS** | `test_m04`；P4 |
| T15 | 完成请求重复执行幂等 | PASS | — | PASS（P4 重放 created=False） | **PASS** | `test_c02/c03`、`test_repeated_complete_*`；P4 |
| T16 | 命令失败状态正确、资源清理 | PASS | — | PASS（P7a 真实调度器） | **PASS** | `test_execution_failure_*`；P7a |
| T17 | Hook 校验失败状态正确、资源清理 | PASS | — | PASS（P10 真实调度器） | **PASS** | `test_blocked_hooks_*`；P10 |
| T18 | 缺少执行命令状态正确、资源清理 | PASS | — | PASS（P7a 同类路径 + 集成测试） | **PASS** | `test_blocked_no_command_*`、`test_real_scheduler_blocks_then_retry_recovers` |
| T19 | 范围锁等待超时无锁残留 | PASS | — | PASS（P3/P7a 锁释放语义） | **PASS** | `test_blocked_scope_timeout_*`、`test_s12` |
| T20 | 执行超时进程树退出、资源清理 | PASS\*\* | PASS | 0a91985 **FAIL** → 64d2458 **PASS**（W1 + 新增 Windows 孙进程用例） | **PASS（64d2458 修复后）** | `test_timeout_*`、`test_windows_timeout_*`；W1、§5.6 |
| T21 | 内部异常不失控且保留原始异常 | PASS | — | PASS（P7d） | **PASS** | `test_internal_exception_*`；P7d |
| T22 | 外部取消及时停止且不提交结果 | PASS\*\* | PASS | 0a91985 Windows 子树残留 → 64d2458 PASS（W3 孙进程消失；不提交/P7b） | **PASS（64d2458 修复后）** | `test_cancel_*`、`test_windows_cancel_*`；P7b；W3 |
| T23 | Worker 崩溃后租约/锁/任务可恢复 | PASS | — | PASS（P3 回收+重认领） | **PASS** | `test_t07`、`test_c07`、集成测试；P3 |
| T24 | 两个 auto-dispatch 并发不重复分派 | PASS | — | PASS（P6a 3 轮） | **PASS** | `test_two_concurrent_*`；P6a |
| T25 | Workflow 重复触发幂等 | PASS | — | PASS（P6b） | **PASS** | `test_repeated_event_*`；P6b |
| T26 | API 成功但客户端超时不产生重复副作用 | PASS | — | PASS（P6e/P6f） | **PASS** | `test_comment_post_timeout_*`、`test_webhook_timeout_*`；P6e/P6f |
| T27 | Linux Hook 权限 644 不得误报通过 | SKIP(POSIX)/Windows 显式跳过 | PASS | — | **PASS** | Linux CI；`test_hook_exec_check_rejects_644_on_posix`（Windows 断言"跳过"） |
| T28 | Linux 主机信息与 capabilities 正确 | PASS（单测按平台注入） | PASS | — | **PASS** | `test_hostinfo.py`；Linux CI |
| T29 | Windows 风格路径按规范处理 | PASS | PASS(POSIX 分支) | — | **PASS** | `test_command_parse.py`；Linux CI |
| T30 | 多项故障连续后状态与资源收敛 | PASS | — | PASS（P8 端到端） | **PASS** | `test_c11`、`test_disconnect_5min_*`、`test_real_scheduler_end_to_end`；P8 |

\* T09 仓库内两个"Windows 证据"实际是 `FakeProcess(pid=None)` 假进程（`test_watchdog.py:40-65`），未走真实 `taskkill`；本报告用 W6 真实进程补测后判 PASS。64d2458 已新增 3 条 Windows 真跑用例，矩阵 T09/T10/T20 行也按其真实证据更正。
\*\* T20/T22 的 Windows 行在 0a91985 上只在"直接子进程"语义下成立；**孙进程场景在 0a91985 为 FAIL**，64d2458 修复后 W1/W3 复验转 PASS（见 §5.6）。

## 七、阻断问题 B1：Windows 分支进程树清理假阳性

**级别：阻断（P0）**　|　影响：T10 的 Windows 等价能力、T20 Windows 验收点、T22 Windows 子树清理；`worker` 会据"已退出"继续释放 worktree / 上报完成。

> **修复状态（64d2458）：已修复，复验通过。** 修复 commit `64d2458` 新增 `apps/agent-worker/windows_tree.py`
> （终止前快照整棵树 → 宽限阶段不杀根 → 根存活时 `taskkill /F /T` → 逐个确认快照子孙消失；
> 查询失败按"可能存活"处理），`executor.py` Windows 分支改走它，并新增 3 条 Windows 真跑用例。
> 我用同一组 W1–W7 在 64d2458 上复验：**W1/W2/W3 全部 PASS**（孙进程消失、`tree_exited` 可信），
> 变异自检 A/C 会变红（§5.6）。初验证据保留在 §7.1–§7.4 作为缺陷回归依据。

### 7.1 现象（0a91985 实测）

在 `D:\tmp\muse2-pinned`（`git archive 0a91985` 快照）上，用真实进程 + `ctypes` 判活：

```
W1 超时：killed_reason=timeout, tree_exited=True, 孙进程存活=True, executor 耗时 5.86s
W2 根进程正常退出：exit_code=0, tree_exited=True, 孙进程存活=True
W3 取消：killed_reason=cancelled, tree_exited=True, 孙进程存活=True
```

### 7.2 机制（已用对照实验钉死）

1. 软阶段 `_signal_group(force=False)` 在 Windows 执行 `taskkill /T /PID <root>`（**不带 `/F`**）；
   W4 实测：对控制台进程 rc=128、无输出，**根进程与孙进程都没被杀**。
2. 软阶段随后回退 `proc.terminate()`（`executor.py:175-178`），TerminateProcess **只杀根进程**。
3. 宽限循环里 `_group_alive()` 的 Windows 分支是 `return proc.poll() is None`（`executor.py:141-150`），
   根已死 ⇒ 返回 False ⇒ `terminate_tree()` 在第一次轮询就 `return True`，**`/F /T` 升级（`executor.py:201`）永不执行**。
4. 即使升级，根 PID 已消失：W5 实测 `taskkill /F /T /PID <root>` rc=128 `ERROR: The process "…" not found.`，孙进程存活。
5. `Executor.run` 的 `finally` 分支同样只看根进程 `poll()`（`executor.py:269-276`），所以"根正常退出、孙进程残留"也判 `tree_exited=True`（W2）。

**附带影响**：孙进程继承了 root 的 stdout 管道，`reader.join(timeout=5.0)`（`executor.py:277`）会等满 5 秒，超时路径整体被拖慢约 5s（实测 5.86s vs 设定的 0.5s 超时），且每次泄漏一个孙进程。

### 7.3 代码位置

| 文件:行 | 内容 |
| --- | --- |
| `apps/agent-worker/executor.py:141-150` | `_group_alive()`：Windows 只看根进程 poll |
| `apps/agent-worker/executor.py:165-169` | Windows `taskkill /T /PID`（不带 `/F`） |
| `apps/agent-worker/executor.py:175-178` | 软阶段回退 `terminate/kill`（只作用于根进程） |
| `apps/agent-worker/executor.py:191-199` | 宽限循环用 `_group_alive()` 判"已退出" → 提前返回 True |
| `apps/agent-worker/executor.py:265-278` | 超时/取消/正常退出后均按根进程判断树是否退出 |
| `docs/workspace/multi-worker-scheduler-design.md:165-167` | 声明 Windows 用 `taskkill /F /T /PID`，但实际升级路径不可达 |
| `docs/workspace/multi-worker-regression.md:79` | T20 Windows 列写 PASS，与实测不符 |

### 7.4 为什么现有测试没发现（测试强度问题）

- `apps/agent-worker/tests/test_executor_process_tree.py:43-50` 的超时用例只启动**直接子进程**，没有孙进程；`tree_exited` 只看该直接子进程，Windows 下恒为 True。
- 三个真正验证孙进程/进程组的用例都标了 `POSIX_ONLY`（`test_executor_process_tree.py:19-20`），Windows 全跳过。
- `test_watchdog.py:40-65` 的 `FakeProcess` 故意设 `pid=None`，从未走真实 Windows `taskkill` 分支。

### 7.5 修复验收要求（建议）

> 64d2458 已满足第 1–4 项（W1–W3 复验 + 3 条 Windows 真跑用例 + 变异自检，见 §5.6）；
> 第 5 项的矩阵 T09/T10/T20 行已在该 commit 更正（设计文档只保留原概述，缺"快照→/F /T→逐个确认"
> 的实现细节，见 N6，不阻断）。

1. Windows 分支必须**先建立"谁属于这棵树"的确定性**再杀根，例如：
   - 用 Windows Job Object（`CREATE_SUSPENDED` 起进程 → 加入 Job → 设置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` → Resume），或
   - 杀根前枚举并保存后代 PID（Toolhelp32/WMI），杀根后逐个确认退出。
2. `tree_exited` 必须基于**真实后代存活**判断，不能只 `proc.poll()`。
3. 软阶段失败时不要先 `terminate()` 根进程把 `/F /T` 的定位点毁掉；顺序应为「先尝试带 `/F` 的树终止，再兜底单进程」。
4. 新增 **windows-latest 真跑**的孙进程回归（不允许 `skipif os.name != "posix"`）：超时 / 取消 / 根正常退出三条路径各一条，断言 `tree_exited=True` 时孙进程已死。
5. 修正矩阵 T20 Windows 行与设计文档 §八 的口径；T09 Windows 行的证据来源应写明（可用真实进程用例）。

## 八、非阻断问题与观察

| # | 类型 | 描述 | 位置 |
| --- | --- | --- | --- |
| N1 | 测试替身语义漂移 | `FakeScheduler.release()` 总是落 `RETRYABLE`，真实 `SchedulerService.release()` 对 `BLOCKED` 会落 `QUEUED`；因此 Worker 单测的 `state in ("blocked","retryable")` 断言无法校验真实状态机（真实行为由集成测试覆盖，结论不受影响） | `apps/agent-worker/tests/conftest.py:276-283` vs `scheduler/leasing.py:346-401` |
| N2 | 证据强度 | 初验时 T09 Windows 列写"PASS(Windows taskkill 路径)"，但仓库测试只有 `FakeProcess(pid=None)`，从未执行真实 taskkill；**64d2458 已新增 Windows 真跑用例并更正矩阵 T09/T10/T20 行**。本报告 W6 实物补测为 PASS | `docs/workspace/multi-worker-regression.md`（64d2458）、`apps/agent-worker/tests/test_watchdog.py:40-65` |
| N3 | 覆盖缺口 | Worker 层"执行超时 → failed + 清理"分支（`worker.py:306-309`）没有仓库测试（只有 executor 层用例）；本报告 P9 实测通过，建议补进回归 | `apps/agent-worker/worker.py:306-309` |
| N4 | 同类风险（未解决，保留） | Worker 根进程自行退出（崩溃）时，其孤儿子进程不会被看门狗清理（W7：restarts=2、孤儿存活；**0a91985 与 64d2458 均如此**）。不属 T09/T10/T20 验收口径，但 Windows 上仍是泄漏风险，建议后续单独立项 | `apps/agent-worker/watchdog.py`、W7 |
| N5 | 静态风险（低） | `release()` 对 `CANCEL_REQUESTED` 会回落到 `RETRYABLE`，理论上可让已请求取消的任务被重新认领而丢失取消语义；未构造出可达场景（正常路径走 `ack_cancel`），仅记录 | `scheduler/leasing.py:366-375`、`service.py:227-273` |
| N6 | 文档一致性 | 修复 commit 已更正矩阵 T09/T10/T20 行并新增「独立验收发现与修复记录」（§6.5）；此外工作树里还有**他人未提交**的矩阵更新（T22 行补 Windows 用例、修正 `test_mirror.py` 引用、§八 CI 证据回填 run 43648075661 的 271/6 与 102/4），内容与我实测一致。仍缺：设计文档 §八 未写入 Windows 实现细节（快照→/F /T→逐个确认）。均不阻断 | `docs/workspace/multi-worker-regression.md`（工作树）、`multi-worker-scheduler-design.md:165-167` |
| N7 | 性能/副作用 | 初验时孙进程继承 stdout 管道导致 `reader.join(5.0)` 等满 5 秒（B1 附带后果）；**64d2458 复验 W1 耗时从 5.86s 降到 0.78s**，已消失 | `apps/agent-worker/executor.py`、W1 |
| N8 | 并发写窗口（低） | mirror 标签回写是"读当前标签 → 收敛 → PUT 全量标签"，与人工并发改标签时可能覆盖对方的非 state 标签；属可用性/展示层风险，不影响权威状态 | `scheduler/mirror.py:116-127` |
| N9 | 工作区状态 | 本报告是仓库内**我**唯一新增/写入的文件；同目录另有 Muse 3 的 `muse3-verification.md`（他人产出），且 `docs/workspace/multi-worker-regression.md` 有他人未提交修改（见 N6）。这些都不是我的改动，我未修改任何源码/测试/配置 | `git status` |

## 九、安全检查

| 检查项 | 结论 | 证据 |
| --- | --- | --- |
| `shell=True` / `os.system` / `os.popen` / `eval` / `exec` | **无**（仅注释/文档提及 "永不 shell=True"） | 全量 grep 变更代码，0 命中 |
| 命令注入面 | 执行命令解析为 argv 列表（JSON argv 优先，平台感知 shlex），Executor 始终 `Popen(list)`；引号不成对只回退 `str.split()`，仍是 argv | `command_parse.py:37-100`、`executor.py:123-138` |
| token / secret 泄漏 | 未发现日志打印 token/secret；GitHub token 只放 `Authorization` 头；异常文本不含 token；`qa_dispatch` 只打印事件/PR 号，不打印 webhook key | `adapters/github_issue.py:200-233`、`dispatch/github_client.py:400-443`、`dispatch/qa_dispatch.py` |
| 越权部署 | 变更的四个 workflow 无 ssh/scp/kubectl/云部署/生产写操作；仅 GitHub API 读写 | `.github/workflows/{auto-dispatch,agent-orchestrator,dispatch-check,quality}.yml` |
| Workflow 触发安全 | 无 `pull_request_target`；`auto-dispatch` 只处理 merged + base=main；权限最小化（`auto-dispatch`：contents:write（创建 ref 必需）+ issues:write；`dispatch-check`：issues:write + contents:read；`quality`：contents:read） | 同上 |
| 旧实例提交结果 | fencing 阻止：P2 实测 4 条写路径全部 `StaleFencing`，旧结果作废 | P2、`scheduler/leasing.py:70-114` |
| 路径穿越/范围锁 | 绝对路径/盘符/UNC/`..` 越界一律 `ValueError`；越界改动不进提交 | `scheduler/scopes.py:23-54`、`executor.py:330-344`（只 `git add` 范围内文件） |
| 生产操作 | 未部署、未操作真实任务队列/凭据；全程隔离测试环境 | 本报告执行记录 |

## 十、未执行 / 受限项（如实标注）

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 真实 GitHub API 在线行为（Link/422/限流） | NOT TESTED | 无 token、无网络写权限；用自写假 HTTP + 假 RPC 覆盖分页与 CAS 语义 |
| 真实 GitHub Actions 上两个 workflow 真并发 | NOT TESTED / BLOCKED | 本机无 act/runner、无凭据；已用本地多线程 + 原子 CAS 假服务验证 |
| 真实多主机调度 | NOT TESTED | 本轮存储为单主机 SQLite（ADR-2 已声明迁移点） |
| POSIX 本机实测 | BLOCKED | 无 Linux/WSL/Docker；以 CI ubuntu-latest 日志为准（§4.4） |
| 基线 `7bbe5ec` 缺陷复现重跑 | NOT TESTED（未重跑） | 验收规则不允许 checkout；基线证据按 `evidence/baseline-defects.txt` 采信并核对代码路径（不是本轮通过依据） |
| 生产部署 | BLOCKED | 任务书 §15：需项目所有者另行授权，第三方不部署 |
| Windows 孙进程清理 | **PASS（64d2458）** | 0a91985 为 FAIL（B1）；64d2458 修复后 W1–W3 复验通过，变异自检见 §5.6 |

## 十一、验收建议

**建议通过（以复验 commit `64d2458` 为准）。**

理由：初验发现并独立复现的唯一阻断 B1（Windows 进程树清理假阳性）已在 `64d2458` 修复；
我用同一组探针在 64d2458 上复验 W1/W2/W3 全 PASS（孙进程确实消失、`tree_exited` 可信、
超时路径不再多等 5 秒），新增的 3 条 Windows 真跑用例在本机与 CI 均通过，
去掉 `/F`/禁用杀进程的变异自检会让用例变红；三套 pytest 在 64d2458 上 3 轮均
271 passed / 6 skipped；CI run `43648075661` 全部 7 作业 success。

**通过时保留的未解决问题 / 风险（不要求本轮修复，但需继续跟踪）：**

1. **W7（未解决，非阻断）**：Worker 根进程自行退出（崩溃）后遗留的孤儿子进程，看门狗重启
   时不会清理（0a91985 与 64d2458 均复现：restarts=2、孤儿存活）。不属 T09/T10/T20 验收口径，
   但在 Windows 上是真实进程泄漏风险；建议后续单独立项（看门狗在重启前也做一次子孙快照清理）。
2. **N3**：Worker 层超时分支（`worker.py:306-309`）仍无仓库测试（本报告 P9 已实测通过），建议补回归。
3. **N5**：`release()` 对 `CANCEL_REQUESTED` 回落 `RETRYABLE` 的静态风险（未构造出可达路径）。
4. **N8**：mirror 标签全量 PUT 与人工并发改标签的覆盖窗口（展示层）。
5. **受限项**：真实 GitHub API 在线行为、真实 Actions 并发、多主机调度未测（§10）。

**复验项（供最终合并前抽验）：**

1. 64d2458 上重跑本报告 §5.6 的 W1–W3（重点：`tree_exited=True` 时孙进程确实已死）；
2. 三套 pytest + CI `agent-tests` / `agent-linux-process-tests`；
3. 确认 `docs/workspace/multi-worker-regression.md` 的 T09/T10/T20 口径与 §5.6 一致（已核对，一致）。

**初验 commit 0a91985 的结论（保留记录）：不通过 —— 存在阻断 B1。**

## 十二、证据清单

| 文件 | 说明 |
| --- | --- |
| `D:\tmp\muse2-probes\probe_scheduler.py` / `pinned-*`、`pinned2-*` `.log` | P1–P4（多进程认领 / fencing / 锁回收 / 完成幂等），0a91985 与 64d2458 各一份 |
| `D:\tmp\muse2-probes\probe_pagination.py` / 同名 `.log` | P5（150 评论 / 250 Issue / 故障注入 / 去重） |
| `D:\tmp\muse2-probes\probe_dispatch.py` / 同名 `.log` | P6（ref CAS 并发 / 对账 / 超时不重复） |
| `D:\tmp\muse2-probes\probe_worker.py` / 同名 `.log` | P7–P10（清理矩阵 / 端到端 / Worker 超时 / Hook 失败） |
| `D:\tmp\muse2-probes\probe_windows_tree.py` / `pinned-probe_windows_tree.log`、`pinned2-probe_windows_tree.log` | W1–W7：**B1 阻断证据（0a91985）与修复复验（64d2458）** |
| `D:\tmp\muse2-probes\claim_race_helper.py` | P1 多进程竞争子进程 |
| `D:\tmp\muse2-pinned\` | `git archive 0a91985` 只读快照（初验基线） |
| `D:\tmp\muse2-pinned2\` | `git archive 64d2458` 只读快照（复验基线） |
| `D:\tmp\muse2-mutation\` | 64d2458 副本，用于变异自检 A/B/C（§5.6） |
| CI run `43646693148`（0a91985） | agent-tests `268 passed, 6 skipped`；agent-linux `102 passed, 1 skipped`；全 7 作业 success |
| CI run `43648075661`（64d2458） | agent-tests `271 passed, 6 skipped`；agent-linux `102 passed, 4 skipped`；全 7 作业 success |

---

**报告状态：初验 0a91985 = 不通过（阻断 B1，已独立复现）；复验 64d2458 = 建议通过（B1 已修复，W1–W3 转 PASS，变异自检有效，CI 全绿）。**
唯一保留的未解决风险是 W7（Worker 崩溃后孤儿子进程，看门狗不清理；非阻断、非本轮验收口径），已在 §8 N4 与 §11 保留。
