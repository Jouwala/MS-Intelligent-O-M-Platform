# Muse 3 独立验收报告（对抗路径：变异测试 + 独立探针 + Workflow/安全复核）

> 验收方：Muse 3（第二独立验收方，验证路径刻意不同于「逐条跑测试」）
> 分支：`fix/multi-worker-scheduler`
> 初验 commit：`0a91985f3c4fd4d77773e5fc35cd2dd87feb80ab`
> **复验 commit：`64d2458dd828eb107e9ac8853f31d486228b3a7c`**（= `origin/fix/multi-worker-scheduler` 最新 head，2026-11-14 22:26Z 推送）
> 关联：`multi-worker-scheduler-design.md`（ADR-2026-11-14）、`multi-worker-regression.md`（T01–T30）
> 本报告独立于 Lead 团队与 Muse 2，不采信其 PASS 结论；所有结论均来自本机实测或可复核的 CI 记录。

## 〇、结论先行

| 判定 | 内容 |
| --- | --- |
| **初验（0a91985）** | **不建议通过**：发现 1 个阻断级缺陷 **F1（Windows 进程树清理失效 + `tree_exited` 假阳性）**，T09/T10/T20/T22 的 Windows 覆盖不成立；另有 3 个非阻断问题。已当场 send_message 报 Lead，Lead 确认阻断并修复。 |
| **复验（64d2458）** | **F1 已修复并复验通过**：B5/B5b 探针场景孙进程全部消失、`tree_exited` 不再假阳性（连续 2 轮 × 超时/取消两条路径）；新增 3 条 Windows 真跑测试；**独立变异（去掉 `/F /T`）使这 3 条全红**，证明新测试有效；原 6 个 skip 未变（仍全部 POSIX 专用），无新增假绿；CI 7/7 job 全绿。 |
| **最终建议** | **建议通过**（以 `64d2458` 为验收 commit；非阻断项见 §六，不阻塞）。 |
| 测试有效性 | 初验 9 处变异：8 处变红；1 处（executor 进程树）在 Windows 全绿 → 暴露真实缺陷 F1。复验新增 Windows 树变异 → **3 条新测试全红**。 |
| 独立探针 | 初验 6 条：4 PASS、2 FAIL（F1）。复验：同 6 条全部 PASS。 |
| 基线证据真实性 | 在干净 `7bbe5ec` clone 上独立复跑 `evidence/probe_baseline.py`：**9/9 缺陷全部复现**，与 `baseline-defects.txt` 一致（证据不是编造）。 |

## 一、环境与方法

- 机器：Windows（MSNOC1 / .20），Python 3.12.10，pytest 9.1.1，git；无 Linux / WSL / Docker。
- 验收对象：`D:\tmp\noc-soc-fix`，分支 `fix/multi-worker-scheduler`；初验 `0a91985`，复验 `64d2458`（远端 head 已核对一致）。
- 独立隔离（**不修改**验收仓库源码/测试）：
  - `D:\tmp\muse3-mutation`：干净 `0a91985` clone，用于初验变异/破坏性验证；
  - `D:\tmp\muse3-reverify`：干净 `64d2458` clone，用于复验；
  - `D:\tmp\muse3-baseline`：干净 `7bbe5ec` clone，用于复跑基线缺陷探针；
  - `D:\tmp\muse3-tools`：自写变异驱动与独立探针（`mutate.py` / `probes.py` / `taskkill_experiment*.py` / `tree_repeat.py`）。
- 验收期间 Lead 团队在验收仓库工作树里做过 task-7 修复（未提交），因此凡涉及 `0a91985` 的本地结论都在干净 clone 上复算，避免混入在途改动；复验一律在 `64d2458` 干净 clone 上执行。
- 仓库内唯一写入文件：本报告。

**为什么验证路径必须不同**：逐条跑绿只能证明「现有断言通过」，不能证明「断言会在缺陷出现时失败」。本次用三类不依赖被测团队信誉的证据：① 向实现注入缺陷看测试是否变红（变异测试）；② 自写探针直接观测真实 SQLite / 真实子进程 / 真实分页行为；③ 独立复跑基线缺陷探针，验证「修复前失败证据」不是编造。

## 二、实际执行的命令与结果

```text
# 全量（与 quality.yml agent-tests 作业同一口径；干净 clone 上执行）
python -m pytest apps/agent-worker/tests apps/control-plane/backend/tests \
    apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider

0a91985（初验）: 268 passed,  6 skipped in 35.77s
64d2458（复验）: 271 passed,  6 skipped in 30.39s   # +3 条新增 Windows 进程树测试
```

| 套件 | 0a91985（干净 clone） | 64d2458（干净 clone） |
| --- | --- | --- |
| `apps/agent-worker/tests` | 97 passed, 6 skipped | 100 passed, 6 skipped（+3 Windows 树测试） |
| `apps/control-plane/backend/tests` | 100 passed | 100 passed |
| `apps/control-plane/backend/dispatch/tests` | 71 passed | 71 passed |
| 收集总数（`--collect-only`） | 274（=268+6，无静默丢测） | 277（=271+6） |

- **6 个 skipped 逐条核对（两个 SHA 完全一致，全部为平台专用）**：
  1. `test_command_parse.py:107`：POSIX `posix=True` 默认行为（仅 Linux 有意义）；
  2. `test_executor_process_tree.py`（0a91985 行 99/117/136；64d2458 行 155/173/192）：POSIX 进程组/SIGTERM/SIGKILL 语义 3 条；
  3. `test_worktree_guard.py:49/62`：POSIX 可执行位语义 2 条。
- 无 `xfail`；目标测试目录无 `pytest.ini`/`addopts`/`testpaths` 等隐藏过滤；分套件运行与合并运行的 node id 集合完全一致（0a91985：274/274；64d2458：277/277），无「合并运行静默丢测」。
- 基线证据复跑（`D:\tmp\muse3-baseline`，7bbe5ec）：`python TMP\probe_baseline.py` → **9/9 探针失败复现**，与 `evidence/baseline-defects.txt` 一致（150 Issue 只读 100、并发双认领、完成标签永久不一致、心跳 200s 不刷新、旧进程未退出即重启、异常路径资源残留、旧实例释放新锁、Windows 路径被 shlex 破坏）。基线证据可信。

## 三、变异测试（证明测试是否真的能发现问题）

方法：在干净 clone 上逐处注入变异 → 跑仓库自带相关测试 → 记录红/绿 → `git checkout --` 还原。判定标准：**变红 = 测试能发现该缺陷；全绿 = 测试对该缺陷无效**。

### 3.1 初验（0a91985，`D:\tmp\muse3-mutation`）

| # | 变异（文件:行） | 注入内容 | 跑的测试 | 结果 |
| --- | --- | --- | --- | --- |
| M1 | `scheduler/leasing.py:185` | `fencing_token + 1` → 不加 1 | `test_scheduler_claim.py`、`test_scheduler_complete.py` | **RED**：8 failed, 27 passed（t01/t02/t03/t06/t07/t11/c05/c14） |
| M2 | `scheduler/leasing.py:96` | 删除 `attempt_id`/`fencing_token` 比较 | claim + complete + scope 三套 | **RED**：8 failed, 39 passed（含 t07/c05/c14/s08） |
| M3 | `scheduler/locking.py:44` | `allow_expired=False` → `True` | `test_scheduler_scope.py` | **RED**：1 failed（`test_s12_acquire_scope_requires_unexpired_lease`） |
| M4a | `scheduler/completion.py:129` | 删除幂等键（idempotency_key）重放查询 | `test_scheduler_complete.py` | **RED**：1 failed（`test_c10_idempotency_key_cannot_cross_tasks`） |
| M4b | `scheduler/completion.py:155` | 删除 `(task_id, attempt_id)` 重放判定 | `test_scheduler_complete.py` | **RED**：1 failed（`test_c03_replay_same_attempt_with_different_key_is_replay`） |
| M5 | `agent-worker/executor.py:155` | 去掉 `killpg`/`taskkill /T`，只 `proc.kill()` 直接子进程 | `test_executor_process_tree.py` | **GREEN**：5 passed, 3 skipped（**测试未发现** → F1） |
| M6a | `adapters/github_issue.py:332` | 分页 `_get_json_pages` 首屏直接 `return`（去掉 Link 循环） | `test_pagination.py` | **RED**：14 failed, 18 passed（p01–p10、p19 等） |
| M6b | `dispatch/github_client.py:277` | `paginate()` 只取第一页 | `dispatch/tests/test_github_client.py`、`test_auto_dispatch.py` | **RED**：10 failed, 26 passed（Link 分页/去重/上限/第 2 页失败/搜索分页） |
| M7 | `scheduler/mirror.py:108` | 删除评论幂等标记扫描（重放会重复发完成评论） | `test_mirror.py` | **RED**：1 failed（`test_m03_comment_already_posted_is_not_duplicated`） |

**关键结论（初验）**

1. 调度器（认领/fencing/范围锁/幂等完成）与分页、分派幂等的测试**是有效的**：注入真实缺陷后全部变红，失败用例与缺陷语义一一对应，不是「数据库唯一约束顺手报错」的偶然红。
2. **M5 在 Windows 全绿是有效告警**：仓库自带的 Windows 用例只起「直接子进程」，孙进程用例被 `POSIX_ONLY` 跳过；而原代码在 Windows 上真实的进程树清理本身就是坏的（§四 B5/B5b、§六 F1）。T09/T10/T20 的 Windows PASS 是**用不覆盖目标的测试换来的**。
3. 补充观察：M4a 单独删除「幂等键重放查询」时 `test_c02` 仍绿——因为还有 `(task_id,attempt_id)` 判定 + SQLite `UNIQUE(idempotency_key)` 两层兜底；只有「跨任务复用幂等键」`test_c10` 变红。实现有冗余防护，属多层防御的正常现象（非阻断，见 N3）。

### 3.2 复验（64d2458，`D:\tmp\muse3-reverify`）

- 对 `0a91985..64d2458` 的 diff 核对：只改了 `executor.py`、新增 `windows_tree.py`、`test_executor_process_tree.py`、`conftest.py`、README、回归矩阵；调度器/分页/分派代码**未变**。因此对全部变异点在新 head 上重跑，结果与初验一致：

| # | 变异 | 64d2458 结果 |
| --- | --- | --- |
| M1 / M2 | fencing token 不递增 / 去掉 token 比较 | **RED 8 failed**（与初验一致） |
| M3 | `allow_expired=False`→`True` | **RED 1 failed** |
| M4a / M4b | 删除两种幂等重放判定 | **RED 1 failed / 1 failed**（c10 / c03） |
| M6a / M6b | 两种分页只读第一页 | **RED 14 failed / 10 failed** |
| M7 | mirror 评论幂等扫描去掉 | **RED 1 failed**（m03） |
| **M5win（新增）** | `windows_tree.kill_tree` 的 `taskkill /F /T` → 去掉 `/F /T`（退回无效的 `taskkill /PID`） | **RED：3 failed, 5 passed, 3 skipped** —— `test_windows_timeout_kills_grandchild_and_tree_exited_true`、`test_windows_cancel_kills_grandchild_and_tree_exited_true`、`test_windows_root_exit_leaving_orphans_is_cleaned` **三条全红** |

**M5win 是本次「测试有效性」的关键证据**：它证明新增的 3 条 Windows 测试不是摆设——去掉修复的核心动作（`/F /T`）它们就会失败；也反证 F1 的修复没有被「假绿」掩盖。初验的 M5 旧变异模式在重构后的 `executor.py` 中已不存在（`SKIP: pattern count=0`），由 M5win 接替。

## 四、独立探针（自写，不导入对方 tests）

实现见 `D:\tmp\muse3-tools\probes.py`；对 SQLite 喂真实文件库、对并发喂真实线程/子进程、对分页喂真实 `RestGitHubTransport` 代码路径（只替换 HTTP 字节）、对进程树喂真实 Windows 子进程并用 `tasklist` 核验。

### 4.1 初验（0a91985）

| 探针 | 场景 | 结果 | 观测 |
| --- | --- | --- | --- |
| B1 | 20 线程并发 claim 同一任务（各自独立 SQLite 连接、Barrier 同时起跑，不同 instance/attempt） | **PASS** | 1 成功（token=1）/ 19 LeaseHeld / 0 意外异常；DB 恰好 1 条 lease、1 条 attempt、1 条 claim 审计；state=claimed |
| B2 | 4 个子进程对同一 SQLite 文件并发 claim（墙钟对齐同时起跑） | **PASS** | 1 成功 / 3 LeaseHeld；DB 1 lease、1 attempt、token=1 |
| B3 | 租约过期并被新实例接管后，旧 token 调 `renew` / `complete` / `release_scope` / `acquire_scope` / `transition` | **PASS** | 5 个操作**全部 StaleFencing**；任务仍属新实例（claimed / attempt=a-b）；新实例 renew→running→complete 成功，终态 completed |
| B4 | 连续故障：完成评论写入后「分页第 2 页 503」+「标签写失败」→ 恢复网络/标签后重新 `mirror.drain` | **PASS** | 首轮 applied=1，pending=[comment:error, labels:error]；恢复后 applied=2、pending=0、完成评论**恰好 1 条**、标签收敛 `state:in-review`；第 3 轮 drain=0（零副作用）；任务 completed |
| B5 | 真实子进程超时（根 python 起孙进程后 sleep，timeout 1s）→ `tasklist` 核验孙进程 | **FAIL（F1）** | 孙进程 3s 后仍 ALIVE；`tree_exited=True`（假阳性）、`exit_code=1`；干净 clone 连续 2 轮复现 |
| B5b | 取消路径（`cancel_event`）同样场景 | **FAIL（F1）** | 孙进程 3s 后仍 ALIVE；`tree_exited=True` |

B5/B5b 的根因经 taskkill 语义实验定位（`taskkill_experiment2.py`，干净进程对）：

| 实验 | 观测 |
| --- | --- |
| `taskkill /T /PID <root>`（无 /F，root 存活） | rc=128，root 与孙进程**都还活着**；stderr：「child process … could only be terminated forcefully (with /F option)」 |
| `taskkill /F /T /PID <root>`（root 存活） | rc=0，root + 孙进程都被终止（强制路径本身有效） |
| root 已被 `proc.terminate()` 杀掉后再 `taskkill /F /T` | rc=128「process not found」，**孙进程存活**（树已断，/T 无法枚举） |

即：`0a91985` 的 `executor.py` 宽限阶段先 `taskkill /T`（对控制台进程无效）→ 兜底 `proc.terminate()` 只杀根 → Windows 的 `_group_alive` 仅看 `proc.poll()`，根一死即判「树已退出」，`terminate_tree()` 提前 `return True`，**强制 `/F /T` 永远执行不到**；即使执行，根已死也无效。这是 TASK-10「超时/取消后进程树无残留」在 Windows 上的直接失败，并导致 `tree_exited=True` 假阳性（`worker.py` 会据此删除 worktree）。

### 4.2 复验（64d2458）

| 探针 | 复验结果（连续 2 轮） | 观测 |
| --- | --- | --- |
| B5 超时路径 | **PASS / PASS** | `killed_reason=timeout`、`tree_exited=True`、孙进程 3s 后 `tasklist` **不可见**（两轮 pid 均消失） |
| B5b 取消路径 | **PASS / PASS** | `killed_reason=cancelled`、`tree_exited=True`、孙进程 3s 后不可见 |
| B1–B4 | **PASS**（复验重跑） | 并发认领、fencing、连续故障收敛结果与初验一致 |

## 五、测试有效性审查（是否「只测 Mock / 跳过失败用例 / 断言过弱」）

| 审查项 | 结论 | 证据 |
| --- | --- | --- |
| 6 个 skipped 是否合法 | **合法，全部平台专用，两 SHA 一致** | 逐条见 §二；Linux CI 上 POSIX 用例真实执行（初验 102 passed/1 skipped；复验 102 passed/4 skipped，4 skip = 1 条 Windows 专用解析用例 + 3 条新增 Windows 树用例） |
| 有无被跳过的失败用例 | **未发现** | 无 xfail；目标目录无 pytest 配置过滤；`--collect-only` 与执行数一致；分套件与合并运行 node id 集合一致（274/274、277/277） |
| `test_worker_integration` 是否真 SQLite | **是** | `SchedulerService(SQLiteStore(db_path))`（真实文件库/事务/BEGIN IMMEDIATE），仅 GitHub 侧用内存 Transport；断言覆盖 claim 写入、终态、outbox 回写、幂等重放 |
| 分页测试是否走真实分页代码 | **是** | `FakeGitHub` 只替换 `http_open`（HTTP 字节），`RestGitHubTransport._get_json_pages` 的 Link 跟随/去重/空页/上限/重试全部是生产代码；M6a 变异可使其变红佐证 |
| 断言是否过弱 | **未发现空断言/仅「不抛异常」** | AST 扫描：0a91985 有 264 个测试函数/823 条断言，64d2458 有 267 个/830 条断言，**0 个无断言测试**；44 个「单断言」测试逐条人工核对均为具体等值断言（命令解析结果、状态、计数），无只 `assert no exception`。worker 层清理矩阵使用语义替身 `FakeScheduler`（合理，测的是清理顺序/策略），真实调度器原子性由 integration + 本报告 B1–B4 覆盖 |
| 新增 3 条 Windows 测试是否「假绿」 | **否** | 它们在本机 Windows 真跑并计入 271 passed（不是 skip）；独立变异 M5win 去掉 `/F /T` → 3 条全红，证明断言落在真实行为上 |
| 是否存在「只测 Mock」的系统性风险 | **低** | 调度器测试全部真实 SQLite（tmp_path 文件库）；并发用真实线程/真实子进程；分派并发用真实生产代码 + 强制 ref 唯一性的内存 GitHub；外部 GitHub 网络是唯一被替身的部分（离线要求所致，且已用故障注入覆盖） |

## 六、问题清单

### 阻断（初验发现，复验已关闭）

**F1｜Windows 进程树清理失效 + `tree_exited` 假阳性（T09/T10/T20/T22）** —— **复验已关闭**
- 初验位置（0a91985）：`apps/agent-worker/executor.py:141-150`（`_group_alive` Windows 只看根）、`:153-178`（`_signal_group` 宽限阶段 `taskkill /T` 无效后 `proc.terminate()` 杀根）、`:191-211`（`terminate_tree` 宽限循环提前 `return True`，强制 `/F /T` 不可达）。
- 初验现象：真实子进程超时/取消后孙进程残留 ≥3s，`ExecutionResult.tree_exited=True`（假阳性）；`worker.py` 会据该标志删除 worktree。B5/B5b 在干净 0a91985 上连续复现。
- 影响：Windows Worker 可能遗留孙进程（构建/守护类子进程）；矩阵 T09/T20 的 Windows PASS 曾夸大覆盖。
- 处置：已报 Lead（2026-11-14），Lead 确认阻断并修复（`64d2458`）。
- 复验：见 §十，**关闭**。

### 非阻断（记录在案，不阻塞通过）

**N1｜矩阵 T09/T10/T20 的 Windows 覆盖表述** —— **复验已更正**
- `64d2458` 已更新 `multi-worker-regression.md` T09/T10/T20：Windows 行改为引用真实 Windows 测试并注明「0a91985 上为 FAIL，Muse 2/3 独立发现」，与实际证据一致。

**N1b｜T22 行未同步新增 Windows 取消测试（文档小瑕疵，非阻断）**
- `multi-worker-regression.md:81` 的 T22 仍只引用 `test_cancel_event_terminates_process_group`（POSIX），未提到新增的 `test_windows_cancel_kills_grandchild_and_tree_exited_true`。建议补一句引用。

**N2｜矩阵 §五（缺陷→测试映射）有失效引用（非阻断）**
- `multi-worker-regression.md:104` 引用 `test_scheduler_mirror.py::test_m01–m10`，实际文件是 `test_mirror.py`（不存在 `test_scheduler_mirror.py`）；同表还有 4 处区间简写（`test_p01–p20` 等），不是可点击 node id。建议改为 `test_mirror.py::test_m01–m11` 或加说明。

**N3｜M4a 变异下 `test_c02` 仍绿（多层防护的测试敏感度，非缺陷）**
- 删除「幂等键重放查询」后，`(task_id, attempt_id)` 判定 + `UNIQUE(idempotency_key)` 仍兜住同任务重放；测试只对「跨任务复用键」（c10）变红。多层防御是有意设计；仅记录为测试敏感度边界。

**N4（观察，超出本轮声明范围）｜执行命令继承 Worker 进程环境**
- `executor._spawn()` 未显式清理环境变量，Issue 指定的命令会继承 Worker 进程环境（若部署时把 `GH_TOKEN` 放在 Worker 环境里，任务命令可读取）。设计 §9 只承诺「日志脱敏交由部署侧」，本轮未要求容器化隔离；记录为部署侧残余风险，建议后续用最小环境（`env=` 白名单）执行任务命令。

## 七、Workflow / CI 复核

### 7.1 YAML 与权限

- 8 个 workflow 文件全部 `yaml.safe_load` 成功（PyYAML；`on:` 在 PyYAML 1.1 下解析为 `True`，属已知行为，不影响 GitHub 解析）。
- 四个本轮改动的 workflow 权限（最小必要核对）：

| 文件 | permissions | 是否最小必要 |
| --- | --- | --- |
| `auto-dispatch.yml` | `contents: write`（创建 git ref CAS）、`issues: write`（TASK_ASSIGNED 评论/标签）、`pull-requests: read`（读 PR 合并态） | 是 |
| `agent-orchestrator.yml` | `contents: write`（ref CAS）、`pull-requests: read` | 是（webhook 走 secret，不需 GitHub 额外权限） |
| `dispatch-check.yml` | `contents: read`、`issues: write` | 是 |
| `quality.yml` | `contents: read`（7 个 job 全是只读测试/检查） | 是 |

### 7.2 ref CAS 是否真能保证一次分派

- 生产实现：`dispatch_registry.register()` 以 `POST /git/refs` 创建 `refs/heads/dispatch/<source>/<dispatch_id>` 为唯一线性化点；422 时再用一次读确认「ref 确实存在（已登记）」还是「锚点 SHA 无效（真错误）」，不会把真错误静默当幂等（`dispatch_registry.py:136-155`，`test_422_without_ref_is_a_real_error` 覆盖）。
- 本地并发测试用「强制 ref 唯一 + 线程安全」的假 GitHub 走真实 `_request_full`/`paginate`/业务代码：2 线程 Barrier 同时 `dispatch_next` → 恰好 1 `dispatched` + 1 `reconciled`、1 条 TASK_ASSIGNED、1 个 ref（`test_two_concurrent_dispatch_runs_register_once`）；重复投递/两条触发路径同键幂等、评论超时后对账不重复、ref 在评论缺时补发一次均有测试，M6b 变异可使其变红。
- **check-then-act 残留（已识别并记录，非阻断）**：
  1. `auto_dispatch.collect_ready()/is_ready()` 先读评论判断「无人认领/未分派」，再 CAS 登记——可接受的「读后 CAS」，CAS 保证同一 `dispatch_key` 只登记一次；但两次并发运行若在「选择目标任务」阶段选中**不同** Issue，会各自登记不同 key、各派一个任务。`dispatch_registry.py:24-27` 已把该残余风险写明，属已披露的已知边界。
  2. `ensure_comment_once` 仍是「先查再发」：并发赢家发评论超过 `reconcile_timeout`（默认 2s）时，输家可能补发第二条**同 dispatch_id** 评论。影响是重复评论（展示层），不会重复分派；有对账测试覆盖常见路径。建议后续收紧补发条件或接受该边界。
  3. `qa_dispatch.trigger_qa` 先 `exists()` 再 `register()`，但 `register` 的 CAS 才是权威（`created=False` 即退出），失败回滚只在明确非 2xx 时发生——逻辑正确。

### 7.3 「内嵌 Python 是否留成两套真相」

- 四个改动 workflow 已无内嵌 Python/curl/jq 业务逻辑，只保留 `python apps/control-plane/backend/dispatch/<module>.py` 一行入口（`auto-dispatch.yml:82-83`、`agent-orchestrator.yml:81-82`、`dispatch-check.yml:59-60`）；质量作业也只跑命令。**未发现同一逻辑的 YAML 副本与模块双实现。**
- 未迁移的 `auto-merge.yml` / `ci-failure-notify.yml` / `issue-to-pr.yml` / `pr-merged-writeback.yml` 仍含内嵌 bash+jq，但不在本次修复范围（commit stat 未改），且本次新增逻辑没有在 YAML 里留副本，不构成「两套真相」。

### 7.4 NOT TESTED 标注是否合理

| 项 | 报告标注 | 复核结论 |
| --- | --- | --- |
| 真实 GitHub Actions 上两条 workflow 真并发 | NOT TESTED / BLOCKED | **合理**：本机无 runner/act/凭据；已用强制 ref 唯一性的假 API 做真实线程 CAS + 故障注入 |
| 真实 GitHub API 的 Link/422/限流在线行为 | NOT TESTED | **合理**：无 token 写权限；实现按官方文档 + 假 API 覆盖 |
| 真实多主机（跨机器）调度 | NOT TESTED | **合理**：存储为单主机 SQLite（ADR-2 已声明迁移点） |
| 生产训练机部署 / 真实任务队列破坏性测试 | BLOCKED | **合理**：需项目所有者另行授权，第三方不得部署 |

### 7.5 CI 实际结论（`gh` 查询，repo `Jouwala/noc-soc-intelligent-platform`）

| run | commit | 结论 | 关键 job |
| --- | --- | --- | --- |
| `43646693148` | `0a91985`（初验） | **success**，7/7 job 绿，5m27s | `agent-tests`（windows）268 passed/6 skipped；`agent-linux-process-tests`（ubuntu）102 passed/1 skipped |
| `43648075661` | `64d2458`（复验） | **success**，7/7 job 绿，约 5m | `agent-tests`（windows）**271 passed/6 skipped in 33.09s**；`agent-linux-process-tests`（ubuntu）**102 passed/4 skipped in 4.76s**（4 skip = 1 Windows 专用解析 + 3 新增 Windows 树用例；POSIX 用例真实执行） |

- 远端 head 与验收 commit 一致：`git ls-remote origin refs/heads/fix/multi-worker-scheduler` = `64d2458`（复验时）。
- 更早的 run `43646681130` 是 cancelled（重复 push 触发，非失败）。
- 附注：`agent-linux-process-tests` 的 4 个 skip 全部是 Windows 专用用例在 Linux 上的合法跳过，反向证明新增的 3 条 Windows 测试确实按平台标注，不是「Windows 上也 skip 的假绿」。

## 八、逐 T01–T30 结论（最终以复验 commit 64d2458 为准）

> 图例：PASS = 本次实际执行通过（含变异/独立探针佐证时注明）；FAIL = 实测失败；SKIP = 平台跳过；NT = NOT TESTED（附原因）。
> 「0a91985 初验」一列记录当时结论；「64d2458 复验」为最终判定。

| T | 验收项 | 0a91985 初验 | **64d2458 复验（最终）** | 依据 |
| --- | --- | --- | --- | --- |
| T01 | Issue 150 条分页 | PASS | **PASS** | 参数化 50/100/101/150/250 全绿；M6a 变红 14 项 |
| T02 | Comments 150 条分页 | PASS | **PASS** | `test_p04`；M6a 变红 |
| T03 | 完成事件在第 2 页被识别 | PASS | **PASS** | `test_p05`；M6a 变红 |
| T04 | 同 Agent 两实例同时认领只一个成功 | PASS | **PASS** | `test_t02`（8 线程）/`test_t03`（2 进程）；独立 B1（20 线程）/B2（4 进程）；M1/M2 变红 |
| T05 | 不同 Agent 同时认领遵守唯一性 | PASS | **PASS** | `test_t05`；M1/M2 变红 |
| T06 | 旧实例提交被 fencing 拒绝 | PASS | **PASS** | `test_c05`/`test_t07`；独立 B3（5 个旧 token 操作全拒）；M1/M2 变红 |
| T07 | 执行 200s 心跳持续刷新 | PASS | **PASS** | `test_200s_*`、`test_heartbeat_continues_while_task_is_running`（执行期后台心跳 ≥3 次） |
| T08 | 执行 30 分钟不被误杀 | PASS | **PASS** | `test_background_heartbeat_keeps_30min_task_not_stale` 等 |
| T09 | 旧 Worker 忽略 SIGTERM 后最终确认退出 | **FAIL（Windows 等价路径）** | **PASS** | Windows 真跑 `test_windows_timeout_kills_grandchild_and_tree_exited_true` + 独立 B5；POSIX 分支 Linux CI PASS；M5win 变异使新测试红 |
| T10 | 孙进程超时后进程树全部清理 | **FAIL（Windows 等价场景）** | **PASS** | `test_windows_timeout_kills_grandchild_*`、`test_windows_root_exit_leaving_orphans_is_cleaned` + 独立 B5；Linux CI POSIX 用例 PASS |
| T11 | 租约过期后范围锁可回收 | PASS | **PASS** | `test_s06`/`test_s07` |
| T12 | 两 Worker 抢重叠范围只一个成功 | PASS | **PASS** | `test_s02`/`test_s09` |
| T13 | 两 Worker 不重叠范围可并发 | PASS | **PASS** | `test_s01`/`test_s10` |
| T14 | 完成评论成功、标签失败后重试修复 | PASS | **PASS** | `test_m04`；M7 变红；独立 B4 复现同场景并收敛 |
| T15 | 完成请求重复执行幂等 | PASS | **PASS** | `test_c02`/`test_c03`；M4a/M4b 变红；集成测试重放 created=False |
| T16 | 执行失败状态正确、资源清理 | PASS | **PASS** | `test_execution_failure_keeps_diagnostics_but_releases_locks`（真实 scheduler 集成） |
| T17 | Hook 校验失败状态正确、资源清理 | PASS | **PASS** | `test_blocked_hooks_releases_everything` |
| T18 | 缺少执行命令状态正确、资源清理 | PASS | **PASS** | `test_blocked_no_command_releases_lease_and_scope` |
| T19 | 范围锁等待超时无锁残留 | PASS | **PASS** | `test_blocked_scope_timeout_releases_lease` |
| T20 | 执行超时进程树退出、资源清理 | **FAIL（Windows 进程树）** | **PASS** | Windows 真跑含孙进程的 `test_windows_timeout_*` + 独立 B5；POSIX 用例 Linux CI PASS；M5win 变红 |
| T21 | Worker 内部异常不失控且保留原始异常 | PASS | **PASS** | `test_internal_exception_cleans_then_reraises`、`test_failure_inside_runner_keeps_original_exception_after_cleanup` |
| T22 | 外部取消及时停止且不提交结果 | **FAIL（Windows 孙进程）** | **PASS** | `test_windows_cancel_kills_grandchild_and_tree_exited_true` + 独立 B5b；`test_cancel_terminates_process_and_never_completes` |
| T23 | Worker 崩溃后租约/锁/任务可恢复 | PASS | **PASS** | `test_t07`/`test_c07`/`test_real_scheduler_blocks_then_retry_recovers`；独立 B3 |
| T24 | 两个 auto-dispatch 同时启动不重复分派 | PASS | **PASS** | `test_register_concurrent_only_one_winner`（8 线程）、`test_two_concurrent_dispatch_runs_register_once`；M6b 变红 |
| T25 | Workflow 重复触发幂等 | PASS | **PASS** | `test_repeated_event_delivery_is_idempotent`、`test_trigger_once_then_idempotent` |
| T26 | API 成功但客户端超时不产生重复副作用 | PASS | **PASS** | `test_comment_post_timeout_after_apply_does_not_duplicate`、`test_registered_ref_without_comment_reconciles_once`、`test_webhook_timeout_keeps_claim` |
| T27 | Linux Hook 权限 644 不得误报通过 | PASS（Windows skip 逻辑） | **PASS** | `test_validate_hooks_*`、`test_hook_exec_check_rejects_644_on_posix`；Linux CI PASS |
| T28 | Linux 主机信息与 capabilities 正确 | PASS | **PASS** | `test_linux_capabilities` 等 |
| T29 | Windows 风格路径按规范处理 | PASS | **PASS** | `test_windows_backslash_path_preserved_with_posix_false` 等 |
| T30 | 多项故障连续发生后状态与资源收敛 | PASS | **PASS** | `test_c11`、`test_disconnect_5min_*`、`test_real_scheduler_end_to_end`；独立 B4 连续故障收敛 |

**统计**：复验 `64d2458` — **T01–T30 全部 PASS**（本地 Windows 实测；Linux POSIX 分支由 ubuntu CI 真实执行通过）。NT/BLOCKED 项见 §7.4（真实 Actions 并发、真实 GitHub 在线、多主机、生产部署），均非本轮可实现范围。
**初验 `0a91985`**：PASS 26、**FAIL 4**（T09/T10/T20/T22 的 Windows 进程树/取消路径，均系 F1）。

## 九、安全检查

| 检查项 | 结论 | 证据/说明 |
| --- | --- | --- |
| 命令永不 `shell=True` | **PASS** | `apps/agent-worker`、`apps/control-plane` 全量 grep 无 `shell=True`；`command_parse.py` 只产出 argv 列表，`executor._spawn` 用 `Popen(list)`；Issue 正文只作为 argv 来源，不做字符串 shell 解释 |
| 日志不含 token/secret | **PASS（本仓库代码）** | 无打印 Authorization/GH_TOKEN/secret；`github_issue.py:208`、`github_client.py:413`、`qa_dispatch.py:88` 只在请求头注入；worker 日志里的 `token=` 是 **fencing_token**（并发代次，不是凭据）。注意：任务命令继承 Worker 环境（N4，部署侧风险） |
| 旧实例不能提交结果 | **PASS** | fencing 覆盖 `renew/transition/complete/release/release_scope/ack_cancel`（M2 变异可令测试变红）；独立 B3 五个旧 token 操作全 StaleFencing；完成前 commit 只发生在本地 worktree，不 push |
| 无未授权部署 | **PASS** | 8 个 workflow 无 deploy/ssh/scp/生产操作；质量作业只读；设计 §9 明示第三方不部署 |
| Issue 内容不被当可信命令 | **部分 PASS / 边界已披露** | 命令只按结构化 argv 执行（无 shell 解释），`shell=True` 禁用；但 Issue 命令本身仍是任意可执行程序（需仓库写权限才能提 Issue），属设计声明的信任边界；未发现把 Issue 正文当脚本 `eval/exec` 的路径 |
| 写入范围守卫 | **PASS** | `executor.commit_in_scope` 只提交范围内文件；`out_of_scope` 单独标出 |
| 范围锁路径穿越/大小写/隔离 | **PASS** | `scopes.normalize_scope` 拒绝 `..`/绝对路径；`test_s05`；默认大小写敏感、可配置 |
| 分派 ref 锚点无效不被误当已登记 | **PASS** | 422 二次确认（`test_422_without_ref_is_a_real_error`） |

## 十、阻断问题与复验（F1）

### 10.1 初验缺陷证据（0a91985，干净 clone）

- 独立探针 B5/B5b（`D:\tmp\muse3-mutation`，executor `0a91985`）：
  - 超时：`killed_reason=timeout`、`tree_exited=True`、`exit_code=1`、孙进程 pid 在 3s 后仍 `tasklist` 可见；连续 2 轮复现；
  - 取消：`killed_reason=cancelled`、`tree_exited=True`、孙进程仍存活。
- 变异 M5（只杀直接子进程）在仓库测试中全绿，佐证 Windows 覆盖率缺口；taskkill 语义实验见 §四。

### 10.2 修复内容（64d2458）

`64d2458 fix(agent-worker): 修 Windows 进程树清理假阳性（Muse 2/3 独立验收阻断项）` 改动 6 文件（+609/−64）：
- 新增 `apps/agent-worker/windows_tree.py`（353 行）：终止前用原生 Toolhelp32（失败退 wmic → PowerShell CIM）快照整棵树；宽限阶段只发 `CTRL_BREAK_EVENT`/`taskkill /T`，**不先杀根**；根存活时执行 `taskkill /F /T /PID root`；wait 后逐个确认快照子孙消失（原生 OpenProcess → `tasklist` 兜底）；查询失败按「可能存活」处理，`tree_exited = 根退出 && 快照子孙全消失`。
- `executor.py`：Windows 路径委托 `windows_tree.terminate()`，执行期周期快照子孙（防父进程中途退出后孙进程失联）；POSIX 路径保持 killpg 语义不变。
- 新增 3 条 Windows 真跑测试（`test_executor_process_tree.py`）：超时、取消、根正常退出留孤儿三条路径。

### 10.3 复验结果（复验 SHA `64d2458dd828eb107e9ac8853f31d486228b3a7c`）

| # | 复验项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | B5/B5b 探针场景：孙进程必须消失、`tree_exited` 不再假阳性 | **PASS（连续 2 轮）** | 超时/取消两条路径 `tree_exited=True` 且孙进程 3s 后 `tasklist` 不可见；见 §4.2 |
| 2 | 独立变异（去掉 `windows_tree.kill_tree` 的 `/F /T`）是否让新增 Windows 测试变红 | **PASS（变异被测试抓住）** | **3 failed, 5 passed, 3 skipped**：`test_windows_timeout_kills_grandchild_and_tree_exited_true`、`test_windows_cancel_kills_grandchild_and_tree_exited_true`、`test_windows_root_exit_leaving_orphans_is_cleaned` 三条全红 |
| 3 | 原 6 个 skip 是否仍全部 POSIX 专用、有无新增假绿 | **PASS** | 6 个 skip 与初验逐条一致（全部 POSIX 专用）；新增 3 条 Windows 测试在 Windows 真跑（计入 271 passed，不 skip），Linux CI 上按其 `WINDOWS_ONLY` 合法跳过（4 skip = 1 + 3） |
| 4 | 全量回归 / CI | **PASS** | 干净 clone `271 passed, 6 skipped`；`quality` run `43648075661` **success 7/7**（windows 271+6、ubuntu 102+4） |
| 5 | 既有变异/探针结论是否仍成立 | **PASS** | M1/M2/M3/M4a/M4b/M6a/M6b/M7 在 64d2458 上仍全部变红；B1–B4 仍 PASS |
| 6 | 文档矩阵是否按实际证据更正 | **PASS（有 1 处小瑕疵 N1b）** | T09/T10/T20 Windows 行已更正并引用真实 Windows 测试；T22 行未同步引用新取消测试（N1b，非阻断） |

**复验结论：F1 关闭。** 修复有效、新增测试有效（变异可证）、无新增假绿、无回归。

## 十一、验收建议

**建议通过**（验收 commit：`64d2458`；初验 `0a91985` 上的阻断项 F1 已修复并由本报告独立复验关闭）。

- 支撑通过的关键独立证据：① F1 探针从 FAIL→PASS 且连续两轮；② 去掉修复动作的变异使 3 条新 Windows 测试全红（测试有效）；③ 全量 271 passed/6 skipped + CI 7/7 绿；④ 基线 9/9 缺陷复现证明证据链真实；⑤ 安全项无阻断发现。
- 通过前建议顺带处理（不阻塞）：N1b（T22 行补引用）、N2（矩阵 §五 `test_scheduler_mirror.py` → `test_mirror.py`）；N4（部署侧用最小环境执行任务命令）列入后续项。
- 未验证项见 §7.4 与 §十二，均为环境/授权限制，非实现缺陷。

## 十二、方法局限与未验证项

1. **无 Linux/WSL/Docker**：POSIX 进程组/SIGTERM→SIGKILL/644 可执行位语义只能依赖 GitHub Actions ubuntu 作业（已核对 run `43648075661` 的 `agent-linux-process-tests`：102 passed / 4 skipped），本机无法独立复现，标注 NOT TESTED（本地）。
2. 真实 GitHub Actions 并发、真实 GitHub API Link/422/限流在线行为：无 runner/写权限，标注 NOT TESTED；本地用「强制 ref 唯一」的假 API 走真实业务/分页代码覆盖。
3. 真实多主机调度：SQLite 单机为设计边界（ADR-2），未验证。
4. 变异测试针对 9 个关键点（初验）+ 1 个复验专项；未做全量变异覆盖（如 scopes 规范化、心跳原子写、watchdog 信号处理的变异）。
5. 验收期间 Lead 团队在验收仓库工作树内做过修复改动（未提交）；本报告所有 0a91985 结论基于干净 clone 复算，复验基于 `64d2458` 干净 clone，不受在途改动影响。

## 附录：复现命令

```powershell
# ---- 初验：干净 0a91985 副本 ----
git clone D:\tmp\noc-soc-fix D:\tmp\muse3-mutation
cd D:\tmp\muse3-mutation; git checkout 0a91985
python -m pytest apps/agent-worker/tests apps/control-plane/backend/tests `
    apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider   # 268 passed, 6 skipped

# ---- 复验：干净 64d2458 副本 ----
git clone D:\tmp\noc-soc-fix D:\tmp\muse3-reverify
cd D:\tmp\muse3-reverify; git checkout 64d2458
python -m pytest apps/agent-worker/tests apps/control-plane/backend/tests `
    apps/control-plane/backend/dispatch/tests -q -p no:cacheprovider   # 271 passed, 6 skipped

# ---- 变异测试（自动注入/还原/记录）----
$env:MUSE3_REPO='D:\tmp\muse3-mutation'; $env:MUSE3_OUT='D:\tmp\muse3-tools\mutation-results.json'
python D:\tmp\muse3-tools\mutate.py
$env:MUSE3_REPO='D:\tmp\muse3-reverify'; $env:MUSE3_OUT='D:\tmp\muse3-tools\mutation-results-64d2458.json'
python D:\tmp\muse3-tools\mutate.py          # 含 M5win：windows_tree.kill_tree 去掉 /F /T -> 3 failed

# ---- 独立探针 ----
$env:MUSE3_REPO='D:\tmp\muse3-mutation'
$env:MUSE3_EXEC_PATH='D:\tmp\muse3-mutation\apps\agent-worker\executor.py'
python D:\tmp\muse3-tools\probes.py threads processes fencing faults tree tree_cancel
# 复验：把 MUSE3_REPO / MUSE3_EXEC_PATH 指向 D:\tmp\muse3-reverify，tree/tree_cancel 应 PASS

# ---- 基线证据复跑 ----
git clone D:\tmp\noc-soc-fix D:\tmp\muse3-baseline
cd D:\tmp\muse3-baseline; git checkout 7bbe5ec
copy D:\tmp\noc-soc-fix\docs\workspace\multi-worker-regression\evidence\probe_baseline.py TMP\
python TMP\probe_baseline.py   # => 9/9 失败复现

# ---- CI ----
gh run list --repo Jouwala/noc-soc-intelligent-platform --branch fix/multi-worker-scheduler
gh run view 43648075661 --repo Jouwala/noc-soc-intelligent-platform      # 复验 head，7/7 绿
gh run view 43646693148 --repo Jouwala/noc-soc-intelligent-platform      # 初验 head，7/7 绿
```
