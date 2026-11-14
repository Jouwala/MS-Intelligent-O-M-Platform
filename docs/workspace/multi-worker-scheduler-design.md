# 多 Worker 调度系统统一修复设计（ADR-2026-11-14）

> 状态：**已实施**（分支 `fix/multi-worker-scheduler`）　|　基线：`7bbe5ec`（main，2026-11-14）
> 关联：`agent-protocol.md`（任务协议）、`multi-worker-regression.md`（T01–T30 回归矩阵）
> 本文是本次修复的**唯一架构决策记录**，后续维护者改调度语义前先读这里。

## 一、问题与范围

现有多 Worker 系统的权威状态是 GitHub Issue 评论 + 标签，认领是「先读评论判断有没有人认领、
再写认领评论」的检查时-执行时（check-then-act）流程。这在两个并发 Worker 下必然出现重复认领；
再叠加 GitHub 分页只读第一页、心跳只在任务开始时写一次、进程终止不确认退出、
完成时评论与标签分别成功/失败等问题，形成一条完整的故障链。

修复覆盖任务书 TASK-01～TASK-07 与 Linux 专项（§7），共 14 项缺陷（P0×6、P1×4、P2×4），
基线复现证据见 `multi-worker-regression/evidence/baseline-defects.txt`（9 组探针全部复现失败）。

## 二、架构决策（ADR）

### ADR-1：GitHub 不再是原子锁，中央调度器是唯一权威状态

**决定**：任务描述、验收标准、PR/评审记录继续放 GitHub；**认领、租约、文件范围锁、
fencing token、任务状态、完成幂等、分派登记由中央调度器（`apps/control-plane/backend/scheduler/`）
在事务存储中裁决**。GitHub 评论与标签降级为**展示与审计镜像**（outbox 异步回写）。

**理由**（任务书 §5.2 已明确）：GitHub 评论没有条件更新/唯一约束，无法线性化。
「先读评论再写评论」在并发下必然都通过检查；GitHub Actions `concurrency` 只能串行化
同一工作流，不能约束 Worker 进程；Issue 标签同理。任何仅靠「加 instance_id 再检查一次」
的方案仍然是 check-then-act。

**代价**：多一个本地状态存储与 outbox 回写通道。收益是可证明的原子性：
唯一约束 + `BEGIN IMMEDIATE` 写事务把认领变成一次线性化的插入。

### ADR-2：存储用 SQLite（WAL + BEGIN IMMEDIATE），并为多机预留迁移点

**决定**：本轮用标准库 `sqlite3`，库文件默认 `<repo>/TMP/scheduler/scheduler.db`
（`TMP/` 已被 `.gitignore` 忽略；可用 `AGENT_SCHEDULER_DB` 或构造参数覆盖）。
写事务一律 `BEGIN IMMEDIATE`，关键约束：

- `leases`：每 task 至多一行（主键 `task_id`）——「同一任务同时只有一个有效执行者」由主键保证；
- `tasks.fencing_token`：认领成功即 +1，单调递增；
- `scope_locks`：每 task 一行，且 `acquire_scope` 在事务内检查其他**未过期租约**的范围重叠；
- `completions`：`UNIQUE(idempotency_key)`、`UNIQUE(task_id, attempt_id)`——完成幂等；
- `dispatches`：主键 `dispatch_key`——分派幂等。

**为什么现在不用 PostgreSQL**：训练机是单主机多 Worker，SQLite 文件数据库在共享文件系统上
即可提供真正的事务与唯一约束；本轮交付环境无 PG 服务，强上 PG 会让测试变成「只测 Mock」。
Worker 只依赖 `Scheduler` 协议（`api.py`），换成 PG 时只替换 `store.py` 实现
（把 `sqlite3` 换成 psycopg + 同样的事务语义），`service.py` 与 Worker 不改。
**迁移点**：多主机部署前必须替换；SQLite 文件不能跨主机共享。

### ADR-3：实例身份 + fencing token，缺一不可

- `agent_id`：逻辑 Worker 身份（muse-2 / muse-3），标签与注册表用；
- `instance_id`：一次运行实例的唯一身份（进程启动生成），**认领与续租必须携带**；
- `attempt_id`：一次任务执行尝试；
- `fencing_token`：任务级执行代次，认领成功单调 +1，**所有状态修改（续租、迁移、
  完成、释放锁、取消确认）必须携带当前 attempt + token，不符一律 `StaleFencing`**。

旧实例恢复运行后：它的租约若已过期，`claim` 已把 token 推进给新实例；它再提交结果/释放锁
会因 token 过小而拒绝。仅在认领里加 instance_id、或只加 attempt_id 而不校验，
都不足以防住「旧实例迟到回写」。

### ADR-4：文件范围锁绑定租约，过期即失效

锁记录带 `attempt_id + fencing_token + expires_at`（与租约同寿命）。
`scope_holders()` / `acquire_scope()` 只认**未过期租约**的锁；`reap_expired()` 回收过期租约时
在同一事务删除其锁。释放锁必须校验 token——旧实例不能释放新实例已取得的锁。
路径规范化统一为 `/`、折叠 `.`/`..`（**拒绝越界穿越**）、大小写按仓库策略
（默认大小写敏感，Windows 盘符/分隔符差异在比较前统一）。

### ADR-5：完成 = 状态 + 事件 + outbox，三步一个事务

`complete()` 在一个事务里完成：终态迁移 + 写 `completions`（幂等键）+ 写 `outbox`
（评论 / 标签 / 审计）+ 删除租约。GitHub 回写由 `mirror.drain()` 异步重放：
评论带 `<!-- completion-id: KEY -->` 幂等标记（发之前先分页查重），标签操作按目标状态收敛。
**因此「评论成功、标签失败」后重试会从已存完成事件继续修复标签，而不是因为
「评论已存在 = 无租约」永久卡死**——这正是基线缺陷 7 的根因。

### ADR-6：分派幂等用「创建 git ref」作为原子 CAS

GitHub 没有事务，但**创建 git ref 是原子且唯一的**：`POST /git/refs` 创建
`refs/heads/dispatch/<dispatch_key>`，已存在时返回 422。工作流先登记 ref（唯一线性化点），
再发 `TASK_ASSIGNED` 评论；评论头带 `dispatch_id`，重试/并发时已存在 ref 且评论已在则不动，
ref 在而评论缺失则补发一次（对账）。`dispatch_key` 由 repo + issue/PR + merge_sha + 触发来源
稳定生成，保证同一逻辑事件重试命中同一个 key。Actions `concurrency` 只作为辅助降噪。

## 三、组件与数据流

```
GitHub Issue / PR（任务描述、验收标准、展示、审计镜像）
        │  分页全量读取（Link rel=next，失败显式报错）
        ▼
中央调度器 SchedulerService（apps/control-plane/backend/scheduler/）
  ├─ store.py    SQLite：tasks / leases / attempts / scope_locks / completions
  │                     / outbox / dispatches / workers / audit（BEGIN IMMEDIATE）
  ├─ service.py  原子认领、租约、fencing、范围锁、状态机、取消、回收、分派登记
  └─ mirror.py   outbox → GitHub 评论/标签回写（幂等、可重试、可收敛）
        ▲                     │
        │ claim/renew/complete │ outbox drain
        │ verify token         ▼
Worker（apps/agent-worker/）：心跳线程 → 认领 → worktree/钩子 → 范围锁 →
  执行（进程组、取消轮询）→ 完成/失败/取消 → 统一清理 → 回收重试
```

## 四、任务状态机（`scheduler/api.py::TRANSITIONS` 为唯一真相源）

| 当前状态 | 事件 | 下一状态 |
| --- | --- | --- |
| queued | 原子认领成功（fencing+1） | claimed |
| claimed | 开始执行 | running |
| running | 执行成功（幂等完成） | completed |
| running | 执行失败（命令退出码非 0） | failed |
| running | 依赖/校验/锁问题 | blocked |
| running / claimed | 收到取消请求 | cancel_requested |
| cancel_requested | 进程退出并清理（ack_cancel） | cancelled |
| claimed / running | 租约过期回收 | retryable / expired |
| retryable / expired | 新实例认领（fencing+1） | claimed |
| queued / claimed / running | 主动取消 | cancelled |

未列出的迁移一律 `InvalidTransition`；终态（completed/failed/cancelled）不再接受认领。
每次执行至少记录：task_id、issue_id、agent_id、instance_id、attempt_id、fencing_token、
lease_expire_at、heartbeat_at、started_at、completed_at、terminal_status、error_code、
error_message、idempotency_key（`tasks` + `attempts` + `audit` 三张表覆盖）。

## 五、认领时序（原子性证据点）

1. Worker 生成 `instance_id`、`attempt_id`，调用 `scheduler.claim(task_id, ...)`。
2. `service.claim` 开 `BEGIN IMMEDIATE`：
   - 读 `tasks`（不存在则报 TaskNotFound；终态报 AlreadyTerminal）；
   - 读 `leases`：存在且 `expires_at > now` 且非本实例 → `LeaseHeld`（回滚）；
   - 否则 `fencing_token += 1`、写新 lease（主键 task_id 冲突即并发失败）、
     写 attempt、置状态 claimed、写审计；
3. COMMIT 后返回 `Lease(attempt_id, instance_id, fencing_token, expires_at)`。
4. 任何状态写入都带 `(attempt_id, fencing_token)`；旧代次 → `StaleFencing`。
5. Worker 侧 GitHub 认领评论只是 outbox 镜像，不影响权威状态。

## 六、完成 / 失败 / 取消的收敛

- **完成**：`complete(idempotency_key=attempt_id)`；重放同键返回 `created=False`，
  不再发第二条完成评论；镜像失败不阻塞权威状态，`mirror.drain()` 重试直到标签/评论到位。
- **失败**：`transition(state=failed, error_code=...)` + 释放锁与租约（保留诊断日志/产物）。
- **取消**：`request_cancel()` 把状态置 `cancel_requested`；Worker 执行期轮询发现后
  终止进程组，进程退出后 `ack_cancel()` → `cancelled`；被取消任务不得再 complete。
- **崩溃**：租约到期由 `reap_expired()` 回收（租约 + 锁 + attempt），任务回到 retryable；
  Worker 启动时清理本实例遗留的 worktree（`worktree.reap_orphans(active_slugs)`）。

## 七、失败清理矩阵（TASK-06 验收依据）

| 退出路径 | 任务状态 | 租约 | 范围锁 | `_active` | 子进程/Worktree |
| --- | --- | --- | --- | --- | --- |
| 成功 | completed | 释放 | 释放 | 清除 | 子进程已退出后按策略回收 |
| 命令失败 | failed | 释放 | 释放 | 清除 | 保留诊断日志，进程已退出 |
| Hook 校验失败 | blocked | 释放（不永久占用） | 不取得/释放 | 清除 | 不遗留无主资源 |
| 无执行命令 | blocked | 释放 | 不取得/释放 | 清除 | 不遗留无主资源 |
| 锁等待超时 | blocked | 释放 | 不取得/释放 | 清除 | 按策略回收 |
| 执行超时 | failed/retryable（按配置） | 释放或可回收 | 释放 | 清除 | SIGTERM→SIGKILL 进程组后回收 |
| 内部异常 | failed/retryable | 释放或可回收 | 释放 | 清除 | 按策略回收 |
| 外部取消 | cancelled | 释放 | 释放 | 清除 | 终止进程组后回收 |
| Worker 崩溃 | 可恢复（过期→retryable） | TTL 回收 | TTL 回收 | 重启重建 | Reaper 回收 |

清理异常不得覆盖原始异常（两段错误都记录）。

## 八、Linux 专项

- **Executor**：独立进程组/Session（`start_new_session=True`），超时/取消先 `SIGTERM`
  进程组，宽限期后 `SIGKILL` 进程组，`wait()` 回收并确认进程树退出；Windows 用
  `CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T /PID`。
- **Watchdog**：重启前 SIGTERM → 宽限 → 轮询确认退出 → SIGKILL（进程组）→ wait()；
  未确认旧进程退出绝不启动新实例；处理 SIGTERM/SIGINT。
- **心跳**：后台线程持续写（默认 30s），临时文件 + `fsync` + `os.replace` 原子替换；
  看门狗阈值（默认 180s）显著大于间隔；解析失败不得作为死亡证明（结合文件 mtime 判定）。
- **主机信息**：`HOSTNAME` → `socket.gethostname()`；capabilities 按 `sys.platform` 生成
  `linux-worker` / `windows-worker`，不得默认声明 Windows。
- **Hook 权限**：POSIX 检查可执行位（644 必须判失败并说明）；Windows 明确跳过并记录。
- **命令解析**：优先结构化 argv（Issue「执行命令」节里的 JSON 数组），否则平台感知
  `shlex`（POSIX `posix=True`，Windows `posix=False` 保留反斜杠），永不 `shell=True`。
  本项目以 Linux 为第一运行目标；Windows Worker 的命令格式另行测试。

## 九、安全边界（任务书 §9 落地）

- 任务来自 Issue 不等于可信：执行命令只接受结构化 argv，禁用 shell 解释；
- 任务范围从 Issue「改动文件」解析后进入范围锁，执行器提交前做范围守卫，超范围改动不入提交；
- Worker 身份使用各自 token，环境变量/Secret 不进日志（执行日志脱敏交由部署侧）；
- 旧实例的失效授权无法提交结果（fencing）；
- 第三方本轮不部署生产、不操作真实任务队列（见交付报告约束）。

## 十、文件修改清单

| 文件 | 改动 |
| --- | --- |
| `apps/control-plane/backend/scheduler/api.py` | **新增** 冻结契约：类型/错误/状态机/Scheduler 协议 |
| `apps/control-plane/backend/scheduler/store.py` | **新增** SQLite schema 与事务原语 |
| `apps/control-plane/backend/scheduler/service.py` | **新增** 原子认领/租约/fencing/锁/幂等完成/回收/分派 |
| `apps/control-plane/backend/scheduler/mirror.py` | **新增** outbox → GitHub 幂等回写 |
| `apps/control-plane/backend/adapters/github_issue.py` | 全量分页 + 重试 + 实例身份注册/心跳 |
| `apps/control-plane/backend/dispatch/*` | **新增** 可测的分派/QA 触发逻辑（ref CAS 幂等） |
| `apps/agent-worker/worker.py` | 接入调度器、持续心跳、取消、统一清理 |
| `apps/agent-worker/executor.py` | 进程组、SIGTERM→SIGKILL、取消、回收 |
| `apps/agent-worker/watchdog.py` | 退出确认、原子读心跳、信号处理 |
| `apps/agent-worker/worktree.py` | 孤儿回收、Hook 可执行位 |
| `.github/workflows/auto-dispatch.yml` | ref CAS 幂等分派 + 分页 |
| `.github/workflows/agent-orchestrator.yml` | QA 触发去重（ref CAS）+ 分页 |
| `.github/workflows/dispatch-check.yml` | 分页 + 幂等写 |
| `.github/workflows/quality.yml` | 新增 Windows/Linux 测试作业 |
| `apps/agent-worker/tests/`、`apps/control-plane/backend/tests/`、`.../dispatch/tests/` | 新增回归测试 |
| `docs/workspace/agent-protocol.md` | 更新身份/租约/状态定义 |
| `docs/workspace/multi-worker-regression.md` | **新增** T01–T30 矩阵与证据 |
| `docs/workspace/multi-worker-regression/evidence/` | 基线复现日志与探针 |

## 十一、迁移、兼容与回滚

**兼容**：`GitHubIssueAdapter` 既有方法（注册/心跳/认领/进展/完成/scope）保留为
GitHub 镜像与审计语义，既有测试不删；新 Worker 走 `SchedulerService` 裁决。
滚动升级期间，旧 Worker（只写评论）与新 Worker（以调度器为准）并存时，
由调度器侧 `register_task` 以 Issue 号为键把 GitHub 状态同步为初始任务，
旧 Worker 的评论不会获得调度器里的租约（不会产生第二个有效执行者）。

**回滚**：本次改动集中在新增目录与四个 Worker 文件、四个 workflow；回滚即
`git revert` 对应提交后重启 Worker 进程即可。SQLite 文件是运行时状态，
删掉即视为无任务（任务描述仍在 GitHub，可重新登记），不涉及数据迁移脚本。
生产部署前需重新授权（任务书 §15）。

## 十二、已知风险与后续项

1. **多主机**：SQLite 不能跨主机共享；多训练机部署前必须把 `store.py` 换成 PG
   （契约已隔离，替换点单一）。
2. **Linux 真实执行**：本机为 Windows，POSIX 信号/进程组测试在 GitHub Actions
   `ubuntu-latest` 作业里真实执行；本机结果标注为「跳过」而非通过。
3. **真实 GitHub Actions 并发**：本地用假 GitHub API 做并发/故障测试；
   真实多工作流并发的验证标注 NOT TESTED（需要隔离仓库+真实 Actions）。
4. **GitHub 回写为异步 outbox**：极端情况下 GitHub 展示会短暂落后于权威状态，
   由 `mirror.drain()` 与对账任务收敛，这是有意的可用性权衡。

### 独立验收（Muse 2 / Muse 3）后保留的后续项（2026-11-14）

5. **W7 孤儿进程（非阻断，Muse 2 实测）**：Worker 根进程「自行退出」后遗留的子进程，
   看门狗重启路径不会清理（watchdog 只管理自己拉起的 Worker 进程树）。执行中的
   子进程树由 executor 负责（`64d2458` 后 Windows 也已覆盖），但若 Worker 在
   执行器之外派生子进程并异常退出，仍可能泄漏。后续建议：Worker 启动时按
   `workers` 表/心跳文件里的 pid 做孤儿回收，或由独立 Reaper 定时清理。
6. **任务命令继承 Worker 环境（Muse 3 N4）**：执行器以继承环境启动子进程，Secret
   与生产凭据对任务命令可见。建议后续提供最小环境白名单（部署侧先按运行用户隔离）。
7. **`dispatch` ref 累积**：`refs/heads/dispatch/**` 随分派与 QA 触发增长；本轮不做
   自动清理（删 ref 会让同一事件的重试被重复分派），人工清理策略见
   `github-automation.md`。
8. **`adapters/github_issue.py` 存量超 500 行（608→743）**：受 `_bootstrap.load_adapter`
   单文件加载方式限制本轮未拆；后续单独任务拆分并同步改加载方式。
9. **Worker 层「执行超时」分支缺仓库回归（Muse 2 N3）**：executor 超时已覆盖，
   Worker 上层把 `killed_reason=timeout` 映射到 FAILED 的路径目前只有集成测试间接覆盖，
   建议补一条显式用例。
