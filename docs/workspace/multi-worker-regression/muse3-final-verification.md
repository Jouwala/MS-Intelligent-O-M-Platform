# Muse 3 最终独立验收报告（对抗路径：ref-CAS 权威层变异测试 + 独立真实 GitHub 双 checkout 探针）

> 验收方：Muse 3（最终验收，对抗路径；不采信 Lead / Muse 2 的结论）
> 验收对象：**最终 main** = `4586d8b6607187b35032c832bf7bcc2593340269`（PR #65 合并提交，mergedAt `2026-11-15T00:32:22Z`）
> 已合并 PR：#60（PR-1 ref 认领）→ #61（PR-2 fencing）→ #62（PR-3 回收）→ #63（PR-4 范围锁）→ #64（PR-5 完成幂等+Worker 切换）→ #65（PR-6 删除 SQLite 权威实现）
> 设计依据：`docs/workspace/multi-worker-github-native-authority.md`（ADR-7）、`multi-worker-scheduler-design.md`
> 验收矩阵：`docs/workspace/multi-worker-regression.md`（T01–T30，PR-6 已按 ref 权威层重编号）
> 报告位置：`D:\tmp\final-acceptance\muse3-final-verification.md`（**未修改仓库、未 commit/push**）
> 环境：Windows（MSNOC1 / .20），Python 3.12.10，pytest 9.1.1，gh 2.102.0；无 Linux / WSL / Docker。

## 〇、结论先行

| 判定 | 内容 |
| --- | --- |
| **总体结论** | **有条件通过（不建议当前解除多 VM 部署门槛）**。ref-CAS 权威层的实现经 10 处变异 + 真实 GitHub 52 项独立 CHECK 验证：**9/10 变异变红、真实 API 52/52 PASS**；但发现 **1 个阻断级测试缺口（R2）**——`_require_current` 的 token 比较被去掉后**全部 23 个相关测试仍绿**，而该检查在真实窗口（HWM 已推进、旧 lease ref 尚未删除）下是承重的（我用真实 GitHub 确定性探针证明）。按本任务「仍绿 = 测试无效（阻断）」规则，该项阻断。 |
| **是否建议解除多 VM 部署门槛** | **当前（`4586d8b`）不建议解除**。实现与真实跨 checkout 行为已满足设计 §13 的架构条件，但验收规则要求补上能杀掉 R2 变异的测试；**补 1 条确定性测试（约 15 行）并通过 CI 后，建议 owner 批准解除**。 |
| 变异测试 | 10 处：**9 RED**（HWM token+1 / reap CAS / scope 创建后校验 / 完成幂等 / lease 过期判定 / `_end_lease` QUEUED / 分页首屏 / 422 消歧 / dispatch CAS）；**1 GREEN（R2，阻断）**。 |
| 独立真实 GitHub | 两个独立 checkout（不同目录、各自进程）对隔离私库 `Jouwala/noc-soc-ref-verify` 打真实 API：**52/52 CHECK PASS**（并发认领唯一赢家、旧 token fencing、Reaper 回收后重认领、重叠/不重叠 scope、完成幂等、分派 CAS、权威唯一静态检查）；清理：本次创建 11 个 ref 全部删除（剩余 0）、10 个测试 Issue 全部关闭（无 open）。 |
| 全量回归 | 最终 main 干净 clone：**330 passed, 6 skipped**（336 collected）；分套件 106(+6 skipped) / 159 / 71；6 个 skip 全部平台专用（POSIX 5 + Windows 专用反向 1）。 |
| CI | PR #65 head quality run `43658008019` **7/7 job 全绿**（Windows agent-tests **330 passed/6 skipped**；Linux process **102 passed/4 skipped**）；最终 main 合并提交上另有 `writeback`/`dispatch-next` 成功（无 main push 的 quality run，见 §六 观察项 N3）。 |

## 一、环境、方法与被测 SHA

- **最终 main SHA**：`4586d8b6607187b35032c832bf7bcc2593340269`（`git ls-remote ... refs/heads/main` 与 fresh clone `git rev-parse HEAD` 一致；PR #65 `state=MERGED`、`mergedAt=2026-11-15T00:32:22Z`）。
- 独立隔离目录（**不修改仓库源码/测试**；不得 commit/push）：
  - `D:\tmp\muse3-final`：`git clone --depth 1 --branch main https://github.com/Jouwala/noc-soc-intelligent-platform.git` 的 fresh clone，用于全量回归 + 变异测试（每处变异后 `git checkout --` 还原，最终 `git status` 干净）；
  - `D:\tmp\muse3-gha`、`D:\tmp\muse3-ghb`：**两个独立 checkout**（不同目录、同一最终 SHA），真实 GitHub 探针的 A/B 进程分别 import 各自 checkout 的 `scheduler` 代码；
  - `D:\tmp\muse3-gha-mut`：gha 的副本 + 手工注入 R2 变异（去掉 token 比较），用于证明 R2 检查承重；
  - `D:\tmp\muse3-tools`：自写变异驱动与探针（`mutate_ref.py` / `real_gh_probe.py` / `gh_probe_worker.py` / `r2_window_probe.py`）。
- 真实 GitHub 验证使用隔离私库 `Jouwala/noc-soc-ref-verify`（private），**从未访问生产仓库**；令牌 `$env:GITHUB_TOKEN = (gh auth token)` 只注入子进程环境变量，**未打印**，日志内做了 token 脱敏断言。
- 方法：① 对 ref 权威层做 10 处变异，验证「测试真的能发现问题」；② 自建两个独立 checkout/进程打真实 GitHub API 验证跨 VM 语义；③ 静态审计测试有效性/workflow/安全/权威唯一；④ 独立核对 CI。

## 二、验收对象（最终 main 的权威层结构）

PR-6 后 `apps/control-plane/backend/scheduler/` 只剩 GitHub 原生 ref-CAS 实现：

```
api.py（冻结契约） / ref_api.py（ref 命名/存活/校验/RefStore 协议）
github_refs.py（RestRefStore：POST refs 原子 CAS + 分页 + 422 消歧 + 重试）
ref_claim.py（HWM CAS 认领）  ref_fencing.py（代次校验/续租/迁移/释放/取消）
ref_reap.py（回收权 CAS）     ref_scope.py（创建后校验的范围锁）
ref_completion.py（评论标记幂等完成） ref_service.py（GitHubRefScheduler 组装）
ref_view.py / ref_workers.py / factory.py（只组装 ref 后端，无 sqlite 分支）
```

- **权威唯一已核实**：scheduler 目录无 `import sqlite3` / `sqlite3.connect` / `SQLiteStore`；`factory.py` 无 sqlite 回退、全仓无 `AGENT_SCHEDULER_BACKEND` 运行时读取（仅 `ref_api.py` 文档注释提到「已随 PR-6 移除」）。真实 API 探针 G1–G3 PASS。
- 基线（干净 clone）：`python -m pytest apps/agent-worker/tests apps/control-plane/backend/tests apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider -rs` → **330 passed, 6 skipped in 71.78s**；分套件：agent-worker `100 passed, 6 skipped`、control-plane backend `159 passed`、dispatch `71 passed`。

## 三、变异测试（证明测试是否真的能发现问题）

方法：在 `D:\tmp\muse3-final`（最终 main 干净 clone）上逐处注入变异 → 跑仓库自带相关测试 → 记录红/绿 → `git checkout --` 还原（`git status` 最终干净）。判定：**变红 = 测试有效；全绿 = 测试无效（阻断）**。

| # | 变异（文件:行） | 注入内容 | 跑的测试 | 结果 |
| --- | --- | --- | --- | --- |
| R1 | `scheduler/ref_claim.py:300` | HWM CAS `candidate = max_token + 1` → 不 +1 | `test_ref_claim.py`、`test_ref_concurrency.py` | **RED**：12 failed, 3 passed |
| R2 | `scheduler/ref_fencing.py:73` | 去掉 `_require_current` 的 `presented_token == max_token` 比较（只要求自己的 lease ref 存在） | `test_ref_fencing.py`、`test_ref_completion.py` | **GREEN**：23 passed（**测试未发现 → 阻断 B1**） |
| R3 | `scheduler/ref_reap.py:134` | 回收权 ref CAS 结果被忽略（所有 Reaper 都当赢家） | `test_ref_reap.py`、`test_lease_reaper.py` | **RED**：3 failed（两 Reaper 竞争 / 真实 HTTP CAS 输家） |
| R4 | `scheduler/ref_scope.py:237` | 跳过范围锁的「创建后校验」 | `test_ref_scope.py` | **RED**：4 failed（重叠/祖先/规范化/并发重叠） |
| R5 | `scheduler/ref_completion.py:67` | 去掉完成幂等查重（评论标记重放判定） | `test_ref_completion.py` | **RED**：2 failed（幂等只一条评论 / 同 attempt 换 key 重放） |
| R6 | `scheduler/ref_api.py:147` | `LeaseRef.is_live()` 恒为 True（去掉租约过期判定） | `test_ref_claim.py`、`test_ref_reap.py`、`test_ref_scope.py` | **RED**：13 failed（过期可重认领 / 回收 / 过期锁不挡路） |
| R7 | `scheduler/ref_fencing.py:148` | `_end_lease` 不再处理 `QUEUED`（不删 lease ref） | `test_ref_fencing.py`、`test_ref_service.py` | **RED**：2 failed（blocked→queued 释放后租约 ref 残留） |
| R8 | `scheduler/github_refs.py:336` | `list()` 分页只读第一页（忽略 Link） | `test_ref_store.py` | **RED**：1 failed（Link 分页收集不全） |
| R9 | `scheduler/github_refs.py:267` | CREATE 的 409/422 不再区分「ref 已存在」与「锚点 SHA 无效」 | `test_ref_store.py` | **RED**：1 failed（422-without-ref 必须显式报错） |
| R10 | `scheduler/ref_service.py:284` | `register_dispatch` 忽略 ref CAS 结果（永远当首次登记） | `test_ref_service.py` | **RED**：1 failed（分派幂等 True/False） |

### 3.1 阻断项 B1｜R2 测试缺口（token 比较未被测试覆盖）

- **现象**：只删掉 `_require_current` 里 `ref_api.require_current_token(...)` 的「`presented_token != max_token`」比较后，`test_ref_fencing.py` + `test_ref_completion.py` **23 个测试全绿**。
- **为什么现有测试没抓住**：现有唯一「手工推进 HWM」的用例是 `test_ref_fencing.py:79-88`（`create(hwm_ref(1, lease.token+1))` 后调 `renew`），它被**续租自身的下一跳 CAS**（`create(hwm T+1)` 返回 False → StaleFencing）兜住；而 `transition` / `complete` / `release` / `ack_cancel` **没有后续 CAS**，只靠 `_require_current` 的 token 比较拒绝旧代次。仓库**没有任何测试**覆盖「HWM 已推进、旧 lease ref 仍存在时，旧 token 的写操作必须 StaleFencing」这个窗口（已 grep 全部 `hwm_ref(`/`seed_ref(` 用法确认）。
- **该窗口真实存在**：`renew` 的顺序是「建 HWM T+1 → 建 lease T+1 → 删 lease T」；`claim` 的顺序是「建 HWM T+1 → 建 lease T+1 → 删低代次 lease」。窗口内 HWM 已是 T+1 而旧 lease T 仍在——此时旧实例调用 `transition/complete`，token 比较是唯一拦截点。
- **独立真实 GitHub 承重证明**（`r2_window_probe.py`，`Jouwala/noc-soc-ref-verify` 真实 API，**6/6 CHECK PASS**）：
  1. 正确代码正常 claim（token=1）；
  2. 直接 `POST refs/heads/agent-hwm/issue-45/00000002`（HWM=2），**保留 lease token=1**（确定性重现窗口，不依赖竞态）；
  3. 正确 checkout 上旧 token `transition(running)` → **StaleFencing（拒绝）**；
  4. 变异副本（R2）上同一调用 → **成功（接受）**；
  → 证明 token 比较是可观测的承重安全检查，R2 变异**不是等价变异**；现有测试套件对该窗口无效。
- **修复建议（测试即可，实现无需改）**：在 `apps/control-plane/backend/tests/test_ref_fencing.py` 增加一条与 `test_renew_rejects_token_advanced_by_another_instance` 同构、但把 `renew` 换成 `transition`（或 `complete`）的用例：

```python
def test_old_token_write_rejected_when_hwm_advanced_but_lease_present(fake_github, ref_scheduler, ref_store):
    lease = ref_scheduler.claim("issue-1", agent_id="muse-2", instance_id="a"*8, attempt_id="b"*8)
    # 手工推进 HWM、保留旧 lease（模拟 renew/claim 的中间窗口）
    assert ref_store.create(ref_api.hwm_ref(1, lease.fencing_token + 1), "a" * 40)
    with pytest.raises(StaleFencing):
        ref_scheduler.transition("issue-1", attempt_id="b"*8,
                                 fencing_token=lease.fencing_token, state="running")
```

  加上后 R2 变异应变为 RED；同类可再补 `complete`/`release` 各一条（可选）。

## 四、独立真实 GitHub 验证（owner 要求的跨 checkout/真实 API）

- 目标：隔离私库 **`Jouwala/noc-soc-ref-verify`**（private，非生产）；A/B 两个**独立 checkout**（`D:\tmp\muse3-gha` / `D:\tmp\muse3-ghb`，同一最终 SHA），每个动作由**独立进程**加载各自 checkout 的代码执行；并发认领由两个进程墙钟对齐同时起跑。
- 令牌：`$env:GITHUB_TOKEN = (gh auth token)`，只注入子进程环境变量，未出现在命令行/日志（日志做 token 脱敏）；报告不含令牌。
- 结果：**52/52 CHECK PASS**（证据：`D:\tmp\final-acceptance\muse3-real-github-evidence.log` 与 `.json`）。

| 阶段 | CHECK | 结果要点 |
| --- | --- | --- |
| A 并发认领 | A1–A4 | 两个独立进程同时 claim 同一 Issue：**恰好 1 个成功、1 个 LeaseHeld**；真实 GitHub 上恰好 1 条 HWM token=1、1 条 lease token=1 |
| B fencing | B1–B12 | 赢家 `renew` 推进 token 1→2；旧 lease 删除、HWM 单调保留；旧 token=1 的 `complete`/`transition`/`release_scope` **全部 StaleFencing**；新 token=2 走 running→completed；完成评论恰好 1 条、终态无租约 |
| C 过期回收+重认领 | C1–C9 | 5s 短租约认领 → 另一 checkout 的 Reaper 回收（`expired_leases`/`retryable` 含该任务，lease ref 删除）→ 新实例重新认领 **token=2**（单调）→ 旧 token 迟到 complete 被 StaleFencing → 新实例完成 |
| D 范围锁 | D1–D8 | 任务1 获取 `apps/shared/verify_file.py`；任务2 同路径 → **ScopeConflict**；任务2 不重叠路径 → 成功；任务1 释放后任务2 可获取重叠范围；两者均可释放 |
| E 完成幂等 | E1–E12 | 首次 `created=True`；同 key 重放 `created=False`；同 attempt 换 key 也 `created=False`；终态用**新 attempt+新 key** → `SchedulerError`；真实 GitHub 完成评论恰好 1 条；终态无租约 |
| F 分派 CAS | F1–F3 | 同一 dispatch_key 连续两次 `register_dispatch` = **(True, False)**；真实 GitHub 上存在 `agent-dispatch` ref |
| G 权威唯一（静态） | G1–G3 | scheduler 目录无 SQLite 实现；factory 无 sqlite 回退；无 `AGENT_SCHEDULER_BACKEND` 运行时读取 |
| Z 清理 | Z1 | 本次创建 11 条 agent-* ref 全部删除（剩余 0）；测试 Issue 全部关闭（`DELETE issue` 返回 404 = 令牌无删 Issue 权限，改为关闭；仓库无 open Issue） |

- **R2 承重探针**（`r2_window_probe.py`，真实 API）：**6/6 CHECK PASS**（见 §3.1；证据 `muse3-r2-window-evidence.log/.json`）。
- 真实 API 观察：PR #64 已发现 GitHub `matching-refs` **不分页**（与假 server 语义不同），本实现用「有 Link 才翻页」处理，真实 API 验证通过；ref 名身份限制（instance/attempt 必须十六进制）由实现显式校验，探针第一次误用非十六进制 ID 时被明确 `ValueError` 拒绝，属正确行为。

## 五、测试有效性审查

| 审查项 | 结论 | 证据 |
| --- | --- | --- |
| 是否有只测 Mock | **否（ref 层用真实本地 HTTP server）** | `apps/control-plane/backend/tests/fake_github_http.py` 是 **真实 `ThreadingHTTPServer`**（TCP + 服务端锁内裁决 ref 唯一性/422/分页/故障注入），`RestRefStore`/`RestGitHubTransport` 走真实 urllib 代码路径；`test_worker_integration.py` 用 `factory.make_scheduler` + fake HTTP server 端到端。测试中未使用内存假 RefStore（grep `FakeRefStore/seed_ref` 无 ref 实现替身）。 |
| 断言是否过弱 | **未发现空断言** | AST 扫描最终 main：326 个测试函数、1003 条断言、**0 个无断言测试**；49 个单断言用例抽查为状态/计数/异常类型等具体等值断言，未发现「仅断言不抛异常」。 |
| 6 个 skip 是否全 POSIX 专用 | **是** | Windows 上 6 skip：`test_command_parse.py:107`（POSIX 默认行为）+ `test_executor_process_tree.py` 3 条 `POSIX_ONLY` + `test_worktree_guard.py:49/62`（POSIX 可执行位）；Linux CI 上反向 4 skip = 1 条 Windows 专用 + 3 条 `WINDOWS_ONLY`（新增的 Windows 进程树用例在 Windows 真跑，不 skip）。 |
| 有无被跳过的失败用例 | **未发现** | 无 `xfail`、无 deselect、目标测试目录无 `pytest.ini`/`pyproject.toml`/`addopts` 过滤；`--collect-only` 336 = 330 passed + 6 skipped；分套件与合并收集一致。 |
| 有无假绿 | **发现 1 处（R2）** | 9/10 变异可被测试发现；R2 的 token 比较无测试覆盖（§3.1）。其余「绿」均有变异/独立真实 API 交叉验证。 |
| 真实 API 证据是否可信 | **可信（我独立复算）** | 我自建两个独立 checkout + 自写 52 项 CHECK 全部 PASS，与仓库文档声称的 28/28 同向且更细；但注意文档引用的证据文件未入库（§六 N1）。 |

## 六、Workflow / 权威唯一 / 安全复核

### 6.1 Workflow

- 9 个 workflow 文件 `yaml.safe_load` **全部可解析**（PyYAML；`on:` 在 YAML 1.1 下解析为 `True`，属已知行为）。
- `lease-reaper.yml`：
  - 触发 `schedule: cron "*/5 * * * *"` + `workflow_dispatch`；
  - permissions 仅 `contents: write`（删 refs）+ `issues: write`（可选标签修正）——最小必要；
  - `concurrency: group: lease-reaper, cancel-in-progress: false`（同刻唯一 Reaper）；
  - 回收权 CAS 语义在 `ref_reap.reap_expired_leases`（`agent-reap/issue-N/<token>` 全局唯一，422=输家跳过），本地真实 HTTP 测试 + 我的真实 API `reap` 路径均验证；**但该 cron 从创建（2026-11-15T00:05:49Z）到复验（00:52）0 次运行**（API `total_count=0`，workflow `state=active`）→ 见 N2。
- `auto-dispatch.yml`：仍以 `POST /git/refs` 创建 `refs/heads/dispatch/...` 为唯一线性化点（`dispatch_registry.py`，422 二次 GET 消歧），评论头带 `dispatch_id`，重试/重复投递幂等；本地 T24–T26 测试全绿；最终 merge 后 `auto-dispatch` run `43658673315` success。`agent-orchestrator` 为 `disabled_manually`（文档明示 Grok 退役），符合预期。
- **权威唯一**：见 §二；PR-6 已删除 SQLite 权威实现与 `test_scheduler_*`/`test_mirror.py`，无运行时后端选择。

### 6.2 观察项（非阻断）

- **N1｜文档引用的真实 API 证据文件不在仓库**：`multi-worker-regression.md:63/127` 引用 `evidence/real-github-multivm-verify.log`，但仓库 `evidence/` 下只有 `baseline-defects.txt` / `probe_baseline.py`；该文件实际在 `D:\tmp\final-acceptance\`，且**被 `.gitignore:157:*.log` 忽略**。建议改名为 `.txt` 或把关键摘要内联进文档后再提交，否则审计链缺环。
- **N2｜lease-reaper cron 未观察到运行**：workflow 创建于 00:05:49Z，复验时 00:52 仍 0 次 run（其他 cron workflow 正常，如 dispatch-check 每小时的 run 存在）。建议 owner 手动 `workflow_dispatch` 一次验证链路，并观察下一轮 cron；安全性不受影响（过期租约在 claim 侧按 `is_live` 判定可被接管，Reaper 只做清理/标签收敛），但「自动回收」当前未获真实运行证据 → **NOT TESTED**。
- **N3｜最终 main 合并提交无 quality push run**：`4586d8b` 的 check-runs 只有 `writeback`/`dispatch-next`（均 success）；`quality` 最近一次 main push run 是 2026-11-13。原因是本次合并由 auto-merge 用默认 `GITHUB_TOKEN` 触发（push 事件被 GitHub 抑制）。当前以「PR #65 head 的 quality 7/7 绿 + 合并树与 head 树一致 + 我在 fresh main 上独立跑 330 passed」作为最终 main 的 CI 证据；若要求 main 分支级 CI，建议合并改用 PAT 或合并后手动 `workflow_dispatch` quality。
- **N4｜矩阵 §五 小瑕疵**：`multi-worker-regression.md:106` 仍用 `test_pagination.py::test_p01–p20` 区间简写（非 node id），不影响理解。

### 6.3 安全

| 检查项 | 结论 | 证据 |
| --- | --- | --- |
| 命令永不 `shell=True` | **PASS** | `apps/agent-worker`、`apps/control-plane` 全量 grep 无 `shell=True`；`command_parse.py` 只产出 argv，`executor` 用 `Popen(list)`；无 `eval/exec/os.system`。 |
| 日志不含 token/secret | **PASS** | 无打印 Authorization/GH_TOKEN/secret；`RestRefStore`/`RestGitHubTransport` 只在请求头注入；worker 日志中的 `token=` 是 fencing_token（并发代次，非凭据）；本次真实 API 日志已做 token 脱敏。 |
| 旧实例不能提交结果 | **PASS（真实 API 实证）** | 旧 token 的 `complete`/`transition`/`release_scope` 在真实 GitHub 上全部 StaleFencing（B4/B5/B6、C7）；HWM 单调不可回退；旧实例结果无法覆盖新执行者。 |
| 无越权部署 | **PASS** | 9 个 workflow 无 deploy/ssh/scp/生产操作；质量作业只读；真实 API 只打隔离私库，清理后无残留。 |
| 部署门槛仍生效 | **PASS** | `multi-worker-scheduler-design.md` §13 仍为「多 VM 部署 BLOCKED」，解除需回归 + Muse 2/3 独立验收 + owner 批准；仓库无解除动作。 |

## 七、逐 T01–T30 结论（最终 main `4586d8b`）

> 本地（Windows）全量 330 passed / 6 skipped；Linux 由 CI ubuntu job 真实执行（102 passed / 4 skipped）。以下 PASS 均基于最终 main 实际执行；标注「真实 API」的项另有我的独立真实 GitHub 交叉验证。

| T | 验收项 | 结论 | 证据 |
| --- | --- | --- | --- |
| T01 | Issue 150 条分页 | PASS | `test_pagination.py::test_p01_p02_issues_full_pagination`（本地） |
| T02 | Comments 150 条分页 | PASS | `test_pagination.py::test_p04_comments_full_pagination_150`（本地） |
| T03 | 完成事件在第 2 页被识别 | PASS | `test_pagination.py::test_p05_*` |
| T04 | 同 Agent 两实例同时认领只一个成功 | PASS（本地 + 真实 API） | `test_ref_concurrency.py`（线程 + 双进程）；真实 API A1–A4 |
| T05 | 不同 Agent 同时认领遵守唯一性 | PASS（本地 + 真实 API） | `test_claim_other_instance_is_lease_held`；真实 API A1（muse-2 vs muse-3） |
| T06 | 旧实例提交被 fencing 拒绝 | PASS（本地 + 真实 API） | `test_renew_then_old_token_cannot_do_anything` 等；真实 API B4/B5/B6/C7 |
| T07 | 执行 200s 心跳持续刷新 | PASS | `test_heartbeat.py`（200s/执行期持续）；变异 R6 等不相关 |
| T08 | 执行 30 分钟不被误杀 | PASS | `test_heartbeat.py`（30min FakeClock） |
| T09 | 旧 Worker 忽略 SIGTERM 后最终确认退出 | PASS | POSIX 用例 Linux CI PASS；Windows 真跑 `test_windows_timeout_kills_grandchild_*`（上一轮 F1 修复已复验） |
| T10 | 孙进程超时后进程树全部清理 | PASS | POSIX `test_grandchild_process_is_reaped`（Linux CI）+ Windows 真跑孙进程用例；F1 修复 |
| T11 | 租约过期后范围锁可回收 | PASS（本地 + 真实 API） | `test_ref_reap.py`/`test_ref_scope.py`；真实 API C2–C4 |
| T12 | 两 Worker 抢重叠范围只一个成功 | PASS（本地 + 真实 API） | `test_overlapping_scope_conflict_rolls_back_all_refs`；真实 API D4 |
| T13 | 两 Worker 不重叠范围可并发 | PASS（本地 + 真实 API） | `test_non_overlapping_scopes_both_acquired`；真实 API D5 |
| T14 | 完成评论成功、标签失败后重试修复 | PASS | `test_ref_completion.py::test_label_write_failure_is_retried_bounded`、`test_persistent_label_failure_does_not_fail_completion` |
| T15 | 完成请求重复执行幂等 | PASS（本地 + 真实 API） | `test_complete_idempotent_posts_one_comment` 等；真实 API E1–E12 |
| T16 | 执行命令失败状态正确、资源清理 | PASS | `test_worker_cleanup.py` |
| T17 | Hook 校验失败状态正确、资源清理 | PASS | `test_blocked_hooks_releases_everything` |
| T18 | 缺少执行命令状态正确、资源清理 | PASS | `test_blocked_no_command_releases_lease_and_scope` |
| T19 | 范围锁等待超时无锁残留 | PASS | `test_blocked_scope_timeout_releases_lease` |
| T20 | 执行超时进程树退出、资源清理 | PASS | Windows 真跑含孙进程用例 + POSIX Linux CI；F1 修复已复验 |
| T21 | Worker 内部异常不失控且保留原始异常 | PASS | `test_internal_exception_cleans_then_reraises` 等 |
| T22 | 外部取消及时停止且不提交结果 | PASS | `test_cancel_terminates_process_and_never_completes` + Windows 真跑取消孙进程用例 |
| T23 | Worker 崩溃后租约/锁/任务可恢复 | PASS（本地 + 真实 API） | `test_github_ref_scheduler_expired_lease_reaped_then_reclaimed`；真实 API C1–C9 |
| T24 | 两个 auto-dispatch 同时启动不重复分派 | PASS | `test_register_concurrent_only_one_winner`、`test_two_concurrent_dispatch_runs_register_once`；真实 API F1–F3 |
| T25 | Workflow 重复触发幂等 | PASS | `test_repeated_event_delivery_is_idempotent`、`test_trigger_once_then_idempotent` |
| T26 | API 成功但客户端超时不产生重复副作用 | PASS | `test_comment_post_timeout_after_apply_does_not_duplicate` 等 |
| T27 | Linux Hook 权限 644 不得误报通过 | PASS | POSIX 用例 Linux CI PASS；Windows 跳过逻辑本地 PASS |
| T28 | Linux 主机信息与 capabilities 正确 | PASS | `test_hostinfo.py`（Linux CI + 本地模拟） |
| T29 | Windows 风格路径按规范处理 | PASS | `test_command_parse.py`（Windows 反斜杠/POSIX 引号分支） |
| T30 | 多项故障连续发生后状态与资源收敛 | PASS（本地 + 真实 API） | `test_github_ref_scheduler_end_to_end`、`test_disconnect_5min_*`；真实 API B/C/E 多故障链路 |

**统计**：T01–T30 **全部 PASS**（本地 Windows 实测；Linux POSIX 分支由 ubuntu CI 真实执行）。NOT TESTED/BLOCKED：真实多主机生产部署（部署门槛 N2/§13）、lease-reaper cron 真实运行（N2）、真实 Actions 并发（沿用原矩阵 NOT TESTED）。

## 八、问题清单

### 阻断（1）

**B1｜R2：`_require_current` 的 token 比较无测试覆盖（测试有效性缺口，实现本身正确）**
- 位置：`apps/control-plane/backend/scheduler/ref_fencing.py:62-78`（`_require_current` → `ref_api.require_current_token`，最终实现在 `ref_api.py:194-211`）。
- 证据：R2 变异后 `test_ref_fencing.py` + `test_ref_completion.py` **23 passed（全绿）**；真实 GitHub 确定性窗口探针证明「正确代码 StaleFencing / 变异代码接受」→ 该检查承重，不是等价变异。
- 影响：将来若有人重构/删除该比较，CI 不会报警；多 VM 下 fencing 依赖它拦截「HWM 已推进但旧 lease 未删」窗口的旧写。
- 修复：补 1 条确定性测试（§3.1 代码）即可让 R2 变异变红；无需改实现。

### 非阻断（3 + 1 minor）

- **N1｜文档引用的真实 API 证据文件不在仓库**（`multi-worker-regression.md:63/127` → `evidence/real-github-multivm-verify.log` 缺失；被 `.gitignore:157 *.log` 忽略）。建议改名 `.txt` 或内联关键摘要提交。
- **N2｜lease-reaper cron 未观察到真实运行**（workflow 创建 00:05:49Z，复验 00:52 仍 0 run，state=active）。建议手动 dispatch 验证；不影响权威安全性，但「自动回收」标注 NOT TESTED。
- **N3｜最终 main 合并提交无 quality push run**（auto-merge 用默认 `GITHUB_TOKEN`，push 事件被抑制）；建议改用 PAT 合并或在 main 上手动触发 quality，否则 main 分支级 CI 证据依赖 PR head + 独立复算。
- **N4（minor）｜矩阵 §五 仍用 `test_pagination.py::test_p01–p20` 区间简写**（非 node id），建议改成真实用例名。

## 九、是否建议解除多 VM 部署门槛（设计 §13）

**当前（`4586d8b`）不建议解除。**

- 已满足的部分：GitHub 原生 ref-CAS 权威层已合入（PR-1~6，SQLite 权威实现已删除、权威唯一）；设计 §13 列出的 T02/T04/T05/T11/T12/T15/T23/T24/T30 回归全部 PASS；两个独立 checkout 打真实 GitHub API 的跨 VM 语义（并发认领唯一赢家、fencing、Reaper 回收后重认领、重叠范围串行化、完成幂等、HWM 单调）**52/52 PASS**；旧实例写操作被真实 API 拒绝。
- 未满足 / 阻塞的部分：**B1（R2 测试缺口）**——按本任务「变异后测试仍绿 = 测试无效 = 阻断」的验收规则，当前 head 不能判为通过；而多 VM 部署的安全性依赖 fencing 拦截「HWM 已推进但旧 lease 未删」窗口。
- **建议路径**：① 补 R2 确定性测试（约 15 行，§3.1）+ 现有测试全绿；② CI 在新 head 7/7 绿；③ (可选) 手动跑一次 lease-reaper 验证 cron；④ owner 按 §13 批准解除。满足 ①②④ 后，**我建议批准解除多 VM 部署门槛**（实现与真实跨 checkout 行为已具备跨机权威层能力）。

## 十、方法局限与未验证项

1. 本机无 Linux/WSL/Docker：POSIX 进程组/SIGTERM/644 语义依赖 GitHub Actions ubuntu job（已核对 102 passed/4 skipped），本机未独立复现。
2. 真实 GitHub 验证是「同机两个独立 checkout + 真实 GitHub 服务端裁决」——对跨 VM 语义（服务端 ref 唯一性/原子 CAS）是有效证据，但不是两台真实 VM + 网络分区/时钟偏差的物理多机测试；多机网络分区场景仍属 NOT TESTED。
3. lease-reaper cron 真实运行未观察到（N2）；真实 GitHub Actions 的并发执行未在本机触发。
4. 变异测试覆盖 ref 层 10 个关键点，未做全量变异（如 ref_view 状态派生、worker 注册表/cancel 路径的变异）。
5. 按任务约束未修改仓库源码/测试、未 commit/push；报告只写在 `D:\tmp\final-acceptance`。

## 附录：复现命令与证据文件

```powershell
# 最终 main 干净副本
git clone --depth 1 --branch main https://github.com/Jouwala/noc-soc-intelligent-platform.git D:\tmp\muse3-final
cd D:\tmp\muse3-final; git rev-parse HEAD   # 4586d8b6607187b35032c832bf7bcc2593340269

# 全量回归
python -m pytest apps/agent-worker/tests apps/control-plane/backend/tests `
    apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider -rs
# => 330 passed, 6 skipped

# 变异测试（10 处，自动注入/还原/记录）
$env:MUSE3_REPO='D:\tmp\muse3-final'
$env:MUSE3_OUT='D:\tmp\muse3-tools\ref-mutation-results.json'
python D:\tmp\muse3-tools\mutate_ref.py
# => R1/R3-R10 RED；R2 GREEN（阻断 B1）

# 独立真实 GitHub（两个独立 checkout + 真实 API；令牌仅环境变量）
$env:GITHUB_TOKEN = (gh auth token)
python D:\tmp\muse3-tools\real_gh_probe.py `
    --checkout-a D:\tmp\muse3-gha --checkout-b D:\tmp\muse3-ghb `
    --sha 4586d8b6607187b35032c832bf7bcc2593340269 `
    --log D:\tmp\final-acceptance\muse3-real-github-evidence.log `
    --json D:\tmp\final-acceptance\muse3-real-github-evidence.json
# => 52/52 CHECK PASS

# R2 承重证明（正确代码 vs 变异副本，真实 API）
$env:CHECKOUT_CORRECT='D:\tmp\muse3-gha'; $env:CHECKOUT_MUTATED='D:\tmp\muse3-gha-mut'
python D:\tmp\muse3-tools\r2_window_probe.py      # => 6/6 CHECK PASS

# CI
gh run view 43658008019 --repo Jouwala/noc-soc-intelligent-platform   # PR#65 head quality 7/7 绿
gh api repos/Jouwala/noc-soc-intelligent-platform/actions/workflows/lease-reaper.yml/runs  # 0 runs（N2）
```

**证据文件**（均在 `D:\tmp\final-acceptance\`）：
- `muse3-real-github-evidence.log` / `.json`（52 CHECK）
- `muse3-r2-window-evidence.log` / `.json`（R2 承重 6 CHECK）
- `muse3-real-github-console.log`（完整控制台输出）
- 变异结果：`D:\tmp\muse3-tools\ref-mutation-results.json`；全量测试日志：`D:\tmp\muse3-tools\final-baseline-clean.log`
