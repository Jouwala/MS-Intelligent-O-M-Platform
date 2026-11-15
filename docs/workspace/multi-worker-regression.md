# 多 Worker 调度系统回归验收矩阵（T01–T30）

> 基线：`7bbe5ec`（main，2026-11-14）　|　修复分支：`fix/multi-worker-scheduler`
> 设计依据：`multi-worker-scheduler-design.md`（ADR-2026-11-14）
> 基线缺陷复现：`multi-worker-regression/evidence/baseline-defects.txt`（9/9 组探针失败）

## 一、结果分级（任务书 §10.4）

| 标记 | 含义 |
| --- | --- |
| PASS | 已实际执行并通过（附命令） |
| FAIL | 已执行且失败 |
| NOT TESTED | 未执行（附原因） |
| BLOCKED | 因环境/权限无法执行（附原因） |
| STATIC REVIEW | 仅静态审查，无动态验证 |

**本机环境**：Windows（MSNOC1 / .20），Python 3.12.10，pytest 9.1.1。
**Linux 环境**：GitHub Actions `ubuntu-latest`（quality.yml 的 `agent-linux-process-tests` 作业）。
POSIX 专用的 6 条测试在 Windows 上按设计 skip（见下表「本地」列的 `SKIP(POSIX)`），
由 Linux 作业真实执行。

## 二、运行命令

```bash
# 全量回归（Windows 本机 / CI agent-tests 作业同一口径）
python -m pytest apps/agent-worker/tests apps/control-plane/backend/tests \
    apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider

# 分套件
python -m pytest apps/agent-worker/tests -q                 # Worker 运行时 / 进程 / 心跳 / 清理
python -m pytest apps/control-plane/backend/tests -q         # 调度器 / 分页 / 镜像
python -m pytest apps/control-plane/backend/dispatch/tests -q # 分派幂等 / workflow 分页

# 仅 POSIX 进程语义（Linux）
python -m pytest apps/agent-worker/tests -q -k "sigterm or grandchild or group_alive or 644"
```

CI 作业（`.github/workflows/quality.yml`，2026-11-14 起由 5 个增至 7 个）：
- `agent-tests`（windows-latest）：上面第一条全量命令；
- `agent-linux-process-tests`（ubuntu-latest）：`python -m pytest apps/agent-worker/tests -q`，
  让 POSIX 进程组 / SIGTERM→SIGKILL / 可执行位测试真实执行。

## 三、测试分层（任务书 §10.1）

| 层 | 落地 |
| --- | --- |
| Unit | `test_ref_*`（GitHub 原生 ref-CAS：认领/fencing/锁/回收/完成/视图）、`test_command_parse`、`test_hostinfo`、`test_heartbeat` |
| Integration | `test_worker_integration`（真实 `GitHubRefScheduler + RestRefStore + fake GitHub HTTP server`）、`test_scheduler_backend_factory`、`test_dispatch_*` |
| Concurrency | 多线程 + 多进程竞争同一 fake GitHub server 的 ref CAS（`test_ref_concurrency`、`_ref_race_helper`、`test_two_concurrent_dispatch_runs_register_once`） |
| Fault Injection | 分页第 2 页失败、POST 超时后成功、标签失败重试、outbox 故障、断网 5 分钟恢复、取消/完成竞争 |
| Linux Process | `test_executor_process_tree`、`test_watchdog`（POSIX 分支在 ubuntu CI 执行） |
| Workflow | `dispatch/tests`（假 GitHub API 强制 ref 唯一性 + 分页 + 故障注入；真实 Actions 并发标注 NOT TESTED） |

## 四、T01–T30 矩阵

> 「本地」= Windows 本机实测；「Linux」= 由 ubuntu-latest CI 作业执行（POSIX 专用项）。
>
> **PR-6（2026-11-15）**：SQLite 权威实现及其专用测试（`test_scheduler_*` / `test_mirror.py`）
> 已删除，下表已逐行改为 GitHub ref 权威层的对应用例（`test_ref_*` / 迁移后的
> `test_worker_integration.py`）；权威唯一，不存在第二套裁决。
>
> **2026-11-15 真实 GitHub 验证（PR #64）**：`Jouwala/noc-soc-ref-verify` 两个独立
> checkout 打真实 API，28 项 CHECK 全 PASS（证据 `evidence/real-github-multivm-verify.txt`），
> 覆盖并发认领/ fencing/过期回收/范围锁/完成幂等与真实 API 分页语义。

| 编号 | 验收项目 | 自动化测试（文件::用例） | 本地 | Linux |
| --- | --- | --- | --- | --- |
| T01 | Issue 150 条分页 | `backend/tests/test_pagination.py::test_p01_p02_issues_full_pagination` | PASS | — |
| T02 | Comments 150 条分页 | `test_pagination.py::test_p04_comments_full_pagination_150` | PASS | — |
| T03 | 完成事件在第 2 页被正确识别 | `test_pagination.py::test_p05_completion_record_on_page_two_is_found` | PASS | — |
| T04 | 同 Agent 两实例同时认领只一个成功 | `test_ref_concurrency.py::test_threads_claim_same_task_exactly_one_wins`、`test_two_processes_claim_same_task_exactly_one_wins`、`test_interleaving_winner_lease_gap_never_yields_two_winners` | PASS | — |
| T05 | 不同 Agent 同时认领遵守任务唯一性 | `test_ref_claim.py::test_claim_other_instance_is_lease_held` | PASS | — |
| T06 | 旧实例恢复后提交结果被 fencing 拒绝 | `test_ref_fencing.py::test_renew_then_old_token_cannot_do_anything`、`test_expired_lease_old_token_fenced_after_takeover`、`test_ref_completion.py::test_complete_old_token_after_renew_is_stale` | PASS | — |
| T07 | 执行 200 秒心跳持续刷新 | `test_heartbeat.py::test_200s_task_stale_without_refresh_but_fresh_with_it`、`test_heartbeat_continues_while_task_is_running` | PASS | — |
| T08 | 执行 30 分钟不被误杀 | `test_heartbeat.py::test_background_heartbeat_keeps_30min_task_not_stale`、`test_fresh_at_heartbeat_not_stale_over_30min_when_refreshed` | PASS | — |
| T09 | 旧 Worker 忽略 SIGTERM 后最终确认退出 | `test_executor_process_tree.py::test_ignored_sigterm_escalates_to_sigkill`（POSIX）；Windows 真实停止路径由 `test_executor_process_tree.py::test_windows_timeout_kills_grandchild_and_tree_exited_true` 与 Muse 2 探针 W6 覆盖；`test_watchdog.py::test_restart_never_starts_new_before_old_confirmed_exit` | PASS（Windows 真实进程树；Muse 2 W6 独立验证） | PASS |
| T10 | 孙进程超时后进程树全部清理 | `test_executor_process_tree.py::test_grandchild_process_is_reaped`（POSIX）、`test_windows_timeout_kills_grandchild_and_tree_exited_true`（Windows 真跑） | PASS（Windows 修复后；0a91985 上为 FAIL，Muse 2/3 独立发现） | PASS |
| T11 | 租约过期后范围锁可回收 | `test_ref_reap.py::test_reap_expired_deletes_lease_keeps_hwm_and_records_fields`、`test_ref_scope.py::test_stale_holder_lease_does_not_block`、`test_cleanup_orphans_removes_only_dead_holder_and_keeps_hwm` | PASS | — |
| T12 | 两 Worker 抢重叠范围只一个成功 | `test_ref_scope.py::test_overlapping_scope_conflict_rolls_back_all_refs`、`test_concurrent_overlapping_only_one_winner` | PASS | — |
| T13 | 两 Worker 不重叠范围可并发 | `test_ref_scope.py::test_non_overlapping_scopes_both_acquired`、`test_concurrent_non_overlapping_both_win` | PASS | — |
| T14 | 完成评论成功、标签失败后重试修复 | `test_ref_completion.py::test_label_write_failure_is_retried_bounded`、`test_persistent_label_failure_does_not_fail_completion` | PASS | — |
| T15 | 完成请求重复执行幂等 | `test_ref_completion.py::test_complete_idempotent_posts_one_comment`、`test_complete_same_attempt_different_key_replays`、`test_worker_cleanup.py::test_repeated_complete_same_attempt_is_idempotent` | PASS | — |
| T16 | 执行命令失败状态正确、资源清理 | `test_worker_cleanup.py::test_execution_failure_keeps_diagnostics_but_releases_locks` | PASS | — |
| T17 | Hook 校验失败状态正确、资源清理 | `test_worker_cleanup.py::test_blocked_hooks_releases_everything` | PASS | — |
| T18 | 缺少执行命令状态正确、资源清理 | `test_worker_cleanup.py::test_blocked_no_command_releases_lease_and_scope` | PASS | — |
| T19 | 范围锁等待超时无锁残留 | `test_worker_cleanup.py::test_blocked_scope_timeout_releases_lease` | PASS | — |
| T20 | 执行超时进程树退出、资源清理 | `test_executor_process_tree.py::test_timeout_terminates_process_tree_and_sets_reason`、`test_windows_timeout_kills_grandchild_and_tree_exited_true`（含孙进程） | PASS（Windows 含孙进程，修复后） | PASS |
| T21 | Worker 内部异常不失控且保留原始异常 | `test_worker_cleanup.py::test_internal_exception_cleans_then_reraises`、`test_failure_inside_runner_keeps_original_exception_after_cleanup` | PASS | — |
| T22 | 外部取消任务及时停止且不提交结果 | `test_worker_cleanup.py::test_cancel_terminates_process_and_never_completes`、`test_executor_process_tree.py::test_cancel_event_terminates_process_group`、`test_windows_cancel_kills_grandchild_and_tree_exited_true`（Windows 真跑） | PASS | PASS |
| T23 | Worker 崩溃后租约/锁/任务可恢复 | `test_worker_integration.py::test_github_ref_scheduler_expired_lease_reaped_then_reclaimed`、`test_ref_reap.py`、`test_ref_fencing.py` | PASS | — |
| T24 | 两个 auto-dispatch 同时启动不重复分派 | `dispatch/tests/test_dispatch_registry.py::test_register_concurrent_only_one_winner`、`test_auto_dispatch.py::test_two_concurrent_dispatch_runs_register_once` | PASS | — |
| T25 | Workflow 重复触发幂等 | `test_auto_dispatch.py::test_repeated_event_delivery_is_idempotent`、`test_qa_dispatch.py::test_trigger_once_then_idempotent` | PASS | — |
| T26 | API 成功但客户端超时不产生重复副作用 | `test_auto_dispatch.py::test_registered_ref_without_comment_reconciles_once`、`test_auto_dispatch.py::test_comment_post_timeout_after_apply_does_not_duplicate`、`test_qa_dispatch.py::test_webhook_timeout_keeps_claim_to_avoid_duplicate` | PASS | — |
| T27 | Linux Hook 权限 644 不得误报通过 | `test_worktree_guard.py::test_validate_hooks_fails_when_pre_commit_is_644`、`test_hook_exec_check_rejects_644_on_posix` | SKIP(POSIX) / PASS(Windows 跳过逻辑) | PASS |
| T28 | Linux 主机信息与 capabilities 正确 | `test_hostinfo.py::test_linux_capabilities`、`test_capabilities_include_python_and_platform_label`、`test_platform_label_matches_runtime` | PASS | PASS |
| T29 | Windows 风格路径按规范处理 | `test_command_parse.py::test_windows_backslash_path_preserved_with_posix_false`、`test_default_split_uses_posix_rules_on_posix` | PASS | PASS(POSIX 分支) |
| T30 | 多项故障连续发生后状态与资源收敛 | `test_worker_integration.py::test_github_ref_scheduler_end_to_end`、`test_worker_recovery.py::test_disconnect_5min_mirror_outbox_resumes_without_duplicate`、`test_ref_completion.py` | PASS | — |

> 说明：T09/T27 的 Windows 行是对「非 POSIX 平台显式跳过/替代路径」的验证；
> POSIX 语义本身由 Linux 行负责，二者不互相替代。

## 五、缺陷 → 测试映射（任务书第四节 14 项）

| # | 缺陷 | 主要回归测试 |
| --- | --- | --- |
| 1 | API 分页不完整 | `test_pagination.py`（31 例：adapter 分页/重试/完成标记）、`dispatch/tests/test_github_client.py` |
| 2 | 同 Agent 不同实例重复认领 | `test_ref_concurrency.py`（线程/双进程）、`test_ref_claim.py` |
| 3 | 心跳不覆盖长任务 | `test_heartbeat.py`（200s / 30min / 执行期持续） |
| 4 | Watchdog 不等旧进程退出 | `test_watchdog.py::test_restart_never_starts_new_before_old_confirmed_exit`、`test_ignored_sigterm_escalates_to_sigkill` |
| 5 | 过期范围锁不失效 | `test_ref_reap.py`、`test_ref_scope.py`（过期租约不挡路/孤儿清理） |
| 6 | 失败/阻塞/超时清理不完整 | `test_worker_cleanup.py`（清理矩阵 8 条） |
| 7 | complete 缺少幂等恢复 | `test_ref_completion.py`（幂等评论+标签重试）、`test_ref_fencing.py` |
| 8 | auto-dispatch 并发重复分派 | `dispatch/tests/test_dispatch_registry.py`、`test_auto_dispatch.py` |
| 9 | Linux 子进程/孙进程泄漏 | `test_executor_process_tree.py`（POSIX，Linux CI 执行） |
| 10 | Worker 缺少取消处理 | `test_worker_cleanup.py::test_cancel_*`、`test_executor_process_tree.py::test_cancel_event_terminates_process_group` |
| 11 | Linux 主机信息/capabilities 失真 | `test_hostinfo.py` |
| 12 | Hook 权限检查不完整 | `test_worktree_guard.py`、`test_hook_exec_check_rejects_644_on_posix` |
| 13 | 命令解析跨平台问题 | `test_command_parse.py`（JSON argv / Windows 反斜杠 / POSIX 引号） |
| 14 | 心跳文件写入不安全 | `test_heartbeat.py::test_atomic_write_concurrent_reads_never_see_partial_json`、`test_corrupt_heartbeat_uses_mtime_not_death` |

## 六、未执行 / 受限项（如实标注）

| 项 | 状态 | 原因与补救 |
| --- | --- | --- |
| 真实 GitHub Actions 上两个 workflow 真并发执行 | NOT TESTED / BLOCKED | 本机无 act/runner、无凭据；已用可注入假 GitHub API 做真实多线程 CAS 与故障注入测试。真实环境验证需在隔离仓库触发两条 workflow |
| 真实 GitHub API 的 Link/422/限流在线行为 | **PASS（PR #64）** | 真实仓库验证覆盖 matching-refs 真实语义、重复 create 422、分页；并发现并修复「matching-refs 不分页」的真实语义差异（假 server 模拟不到） |
| 真实 GitHub 多 checkout（模拟多 VM，真实 API） | **PASS（PR #64）** | 隔离私库 `Jouwala/noc-soc-ref-verify` + 两个独立 checkout + 真实 API：并发认领唯一赢家、旧 token fencing、Reaper CAS 回收后重认领、重叠/不重叠 scope、完成幂等与标签收敛，28/28 CHECK PASS；证据 `evidence/real-github-multivm-verify.txt` |
| 真实多主机（跨机器）生产部署 | **已解除（owner 2026-11-15 批准）** | 代码权威层已跨机可用并有真实 API 证据；Muse 2/3 独立验收通过，owner 2026-11-15 批准解除 #53 门槛。生产部署前仍须完成真实两台物理 VM 联调（见最终验收报告 §五）。 |
| 生产训练机部署 | BLOCKED | 任务书 §3.2/§15：部署需项目所有者另行授权，第三方不得部署 |
| 真实任务队列/生产 Worker 凭据破坏性测试 | BLOCKED | 同上，全程只用隔离测试环境 |

## 六点五、独立验收发现与修复记录（2026-11-14）

Muse 2 / Muse 3 在验收 commit `0a91985` 上**独立复现**同一个阻断缺陷：Windows 分支
在超时/取消后孙进程仍存活，而 `ExecutionResult.tree_exited=True`（假阳性），原因叠加为
「`taskkill /T`（无 /F）对控制台进程无效 → `proc.terminate()` 只杀根 → `_group_alive`
只看根 poll → 强制 `/F /T` 永远不可达」。修复见 commit `fix(agent-worker): 修 Windows
进程树清理假阳性`：

- 新增 `apps/agent-worker/windows_tree.py`：终止前快照整棵树（Toolhelp32 原生 →
  wmic → PowerShell CIM 兜底），宽限阶段不杀根，根存活时执行 `taskkill /F /T`，
  之后逐个确认快照子孙消失；`tree_exited` 查询失败按「可能存活」处理（不给假阳性）。
- 新增 Windows 真跑测试：`test_windows_timeout_kills_grandchild_and_tree_exited_true`、
  `test_windows_cancel_kills_grandchild_and_tree_exited_true`、
  `test_windows_root_exit_leaving_orphans_is_cleaned`；去掉 `/F /T` 的变异自检会让三条全红。
- 原矩阵中 T09/T20 的 Windows「PASS」曾在 `0a91985` 上过强声明，已按实际证据更正；
  Muse 2/3 的独立验收报告见 `muse2-verification.md` / `muse3-verification.md`；
> **最终验收**（最终 main `4586d8b` + 本 PR 的 B1/R2 回归测试）见
> `muse2-final-verification.md` / `muse3-final-verification.md`。
> Muse 3 在最终层发现并复现的 **B1/R2 测试缺口**（HWM 已推进但旧 lease ref 未删的窗口）
> 已在本追加回归：`test_ref_fencing.py::test_old_token_writes_rejected_when_hwm_advanced_but_old_lease_ref_present`、
> `test_ref_completion.py::test_old_token_complete_rejected_when_hwm_advanced_but_old_lease_ref_present`；
> 变异自检（去掉 `require_current_token` 的 token 比较）下这两条必红、原有 23 条仍绿，证明覆盖有效。

## 七、基线证据

`evidence/baseline-defects.txt`（在 `7bbe5ec` 原始代码上运行 `evidence/probe_baseline.py` 的输出）：

```
150 个 Issue 只读到 100 个；150 条评论只读到 100 条；完成记录在第 101 条以外，lease_of() 仍返回有效租约
并发认领结果 ['实例B:成功', '实例A:成功']，issue 42 上出现 2 条认领评论（2 个有效租约）
首次 complete 异常=模拟标签更新 500；重试结果=LeaseConflict；标签=state:claimed（永久不一致）
执行 200 秒期间心跳文件仅写一次，结束时心跳年龄 200s
看门狗已拉起 1 个 Worker 进程，其中 1 个旧进程仍存活
执行体抛异常后 worker._active 仍留有任务；scope-unlock 评论数=0；worktree 仍在
新实例持锁后旧实例 release_scope，新实例读取锁 = None
Windows 路径命令被解析为 ['C:toolspython.exe', ...]
```

## 八、CI 证据

最终验收 head：PR #65 head `c8dd86e`（最终 main `4586d8b` 与该 head 树完全相同）。
GitHub Actions quality（7/7 作业 success）：

| 作业 | Runner | 实际命令 | 结果 |
| --- | --- | --- | --- |
| agent-tests | windows-latest | `python -m pytest apps/agent-worker/tests apps/control-plane/backend/tests apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider` | **330 passed, 6 skipped**（本 PR 新增 B1 回归后 332） |
| agent-linux-process-tests | ubuntu-latest | `python -m pytest apps/agent-worker/tests -q -p no:cacheprovider` | **102 passed, 4 skipped**（POSIX 进程组/SIGTERM→SIGKILL 真实执行） |

Run：push `43658004859` / pull_request `43658008019`，7/7 success。

> 最终 merge commit 在 main 上**没有** quality push run：auto-merge 用默认 `GITHUB_TOKEN`
> 合并，GitHub 不为该 token 触发 push 工作流（仓库已知行为，见 `github-automation.md`）；
> 同树 PR head 的 CI 是有效证据。若要求 main 自身有 CI 记录，可手动 `workflow_dispatch`
> quality 或改用 PAT 合并。
