# ZIP 交付 Phase 0 只读审查报告（草稿）

> **Issue：** [#100](https://github.com/Jouwala/noc-soc-intelligent-platform/issues/100)《ZIP 交付 Phase 0：生产部署与发布 QA 只读审查》
> **负责人：** Muse 5（Production Deployment & Release QA Engineer）
> **审查基线：** main `2014a272`（2026-11-16 09:29 UTC，Merge PR #102）
> **审查时间：** 2026-11-16 10:16–10:21 UTC（只读，未修改任何业务代码，未创建分支/PR/评论/标签）
> **审查方式：** 本地只读克隆 `~/workspace/noc-soc-intelligent-platform` 的代码、脚本、文档与 CI 配置逐项核对；未执行安装、启动、冒烟或任何写操作
> **状态：** 草稿，待 Muse 1 审核；本报告不构成生产验收通过

---

## 一、测试环境要求

### 1.1 已核实的版本与依赖

| 项 | 要求 | 依据 |
| --- | --- | --- |
| Python | **3.12.10**（捆绑 `runtime/python312/`） | `README.md:24,49`、`runtime/README.md:9`、`docs/workspace/development.md:13`（开发态 Python 3.12，虚拟环境 `apps\my_app\.venv`） |
| Node.js | **v24.16.0**（捆绑 `runtime/node/`） | `README.md:24,50`、`runtime/README.md:10`；CI 用 `setup-node@v4` 的 Node 24（`.github/workflows/quality.yml` portal job） |
| PostgreSQL | **16**（捆绑 `runtime/postgresql16/`，便携实例） | `README.md:24,51`、`runtime/README.md:11` |
| nginx | **1.28.0**（Windows 发行包，`nginx-1.28.0/`） | `README.md:24`、`docs/workspace/部署说明.md` §一 |
| PowerShell | **5.1** 兼容（脚本显式声明不用 `::new()`、三元、`-Parallel`） | `ops/deploy/start_all.ps1:21` |
| 操作系统 | **Windows x64**（全部部署脚本为 `.ps1`/`.bat`，调用 `.exe`） | `ops/deploy/start_all.ps1`、`stop_all.ps1`、`start.bat`、`stop.bat` |
| Python 后端依赖 | fastapi、uvicorn、sqlalchemy、alembic、psycopg2-binary==2.9.12、apscheduler 等（见清单） | `apps/my_app/requirements.txt`（另有 `requirements.lock.txt` 精确锁版，`docs/workspace/development.md:40-41`） |
| 前端依赖 | 各子项目独立 `package.json`，无根 `package.json`；`npm ci` 安装 | `README.md` §二-3、`AGENTS.md` 三-7 |
| 磁盘/数据 | `runtime/` 约 6.5 GB（不入库）；`apps/_fe_dist` 约 142–277 MB 构建产物必须随包；`app.db` 约 2.5 GB（PG 模式下不读） | `README.md` §三、`docs/workspace/部署说明.md` §七 |

### 1.2 已核实的外部服务与网络需求

- **部署本身不要求外网**：运行时、前端构建产物、离线地图瓦片（`nginx-1.28.0/html/tiles/`）均随包（`docs/workspace/部署说明.md` §七）。
- **但“装依赖”步骤要求外网**：`npm ci`（三个前端）与 `pip install -r requirements.txt` 均需联网下载；仓库内无已安装的 `node_modules`、无离线 wheel/npm 缓存、无 vendor 目录。**无外网机器上无法从源码重新装依赖，只能用已打包好的副本**——这一点没有任何脚本或文档做离线装依赖的兜底。
- **上游服务（运行期，非部署必需）**：UME / eFlow / FineBI 内网上游刻意不带（`docs/workspace/部署说明.md` §一）；问答模型服务默认回环占位（`QA_OWU_BASE=http://127.0.0.1:8443/api/v1`、`OLLAMA_URL=http://127.0.0.1:11434`，无服务时快速失败而非白等，`ops/deploy/env_template.txt`）；WhatsApp bot 默认关闭。
- **开发态与生产态版本口径不一致**：`docs/workspace/development.md:13` 要求开发 Node.js **18+**，而捆绑运行时与 CI 均为 Node 24；门户用 React 19 / Vite 6 / Tailwind v4。ZIP 包应以捆绑的 Node 24 为准，文档存在两个口径，需在 Phase 1 统一。

### 1.3 缺口

- **无“干净机器首次安装所需清单”的单一真相源**：版本信息散落在 `README.md`、`runtime/README.md`、`docs/workspace/development.md`、`ops/deploy/env_template.txt` 四处，且开发态（Node 18+ / Python 3.12 venv）与便携态（Node 24 / Python 3.12.10 捆绑）混写。
- **无离线依赖安装方案**：`npm ci` / `pip install` 的离线替代（预装 `node_modules`、wheelhouse、lockfile 校验）均未核实到任何依据。
- **运行时来源不可复现**：`runtime/`（6.5 GB）与 `nginx-1.28.0/` 均不入库，补齐方式是“跑主仓库的打包脚本 `-Stage runtimes` 或按报错逐个装”（`runtime/README.md`），无校验和、无版本锁定清单。

---

## 二、双平台测试矩阵（Windows x64 / Linux x64）

### 2.1 现状总览

| 能力 | Windows x64 | Linux x64 |
| --- | --- | --- |
| 打包脚本 | 有（但**不在本仓库**，见 §2.2） | **无** |
| 安装脚本 | 有（`start_all.ps1` 首次启动时 initdb + 生成 `.env`，属“启动时安装”而非独立安装器） | **无** |
| 启动 / 停止 | 有，`start_all.ps1`（498 行）/ `stop_all.ps1`（126 行），pid 文件托管 | **无**（主栈无任何 `.sh` 启动脚本） |
| 健康检查 | 有，`/api/health` + `smoke.ps1` §8.3 六项检查 | **无**（主栈） |
| 数据库初始化与迁移 | 有（Windows，`initdb`/`createdb`/`pg_restore`/`alembic`） | **无**（主栈） |
| 冒烟测试 | 有，`ops/smoke/smoke.ps1`（1166 行，硬编码 `D:\02_olbr_portal`） | **无** |
| CI 覆盖 | `quality.yml` 全部跑 `windows-latest`（后端有 Windows 专用代码，Linux runner 会误报，见文件头注释） | 仅 1 个 job 跑 `ubuntu-latest`，且只测 agent-worker 进程/信号语义，不测主栈交付 |
| Agent Worker（沙箱） | 有（`apps/agent-worker/`） | **有**，`apps/agent-worker-linux/`：`install.sh` + systemd 单元，Python 3.10+ **仅标准库** |

**结论：双平台矩阵目前只有 Windows 一半。Linux x64 对 ZIP 交付物（门户 + my_app + nginx + PG）是空白，仅 agent-worker 沙箱 worker 有 Linux 支持。**

### 2.2 Windows 侧已核实的覆盖

- **打包**：`docs/workspace/部署说明.md` §二描述 `scripts/build_olbr_portable.ps1 -Stage all`（quarantine / source / prune / nodemodules / runtimes / nginx / config 七阶段，支持 `-DryRun`）。**但该脚本不在本仓库内**：仓库内无 `scripts/build_olbr_portable.ps1`、无 `scripts/portable/manifest.json`（文档称其为“保留清单真相源”），`README.md:42` 引用的是主仓库路径 `D:\01_RPA\scripts\build_olbr_portable.ps1`。即：**ZIP 打包能力本身不在本仓库版本控制内**。
- **启动**：`start_all.ps1` 六步——便携 PG（initdb → pg_ctl start → `SELECT 1` 轮询 → `SHOW data_directory` 校验 → createdb）→ nginx 配置生成（含 `nginx -t`）→ my_app 后端（uvicorn）→ 前端（vite preview）→ 门户（tsx）→ nginx；每步端口等待（30–45 s 超时），端口被外部进程占用时抛错拒绝启动（`Test-OwnPort`）。
- **停止**：`stop_all.ps1` 七步——nginx `-s quit` 优雅退出 → 按 pid 文件逆序停 wa_bot / wa_analysis / portal / frontend / backend（pid + 进程名 + 启动时间三重校验防 pid 复用误杀）→ `pg_ctl -m fast stop`；绝不按端口盲杀、绝不 tree kill。
- **干跑**：启动/停止均支持 `-DryRun`；另有 `ops/shortcuts/3-start-dryrun.bat` / `4-stop-dryrun.bat`。
  （2026-11-19：这两个入口已随 `ops/shortcuts` 改生产转发而删除，本节为当时评审快照，见该目录 `README.txt`。）
- **健康检查**：`ops/smoke/smoke.ps1` §8.3（见 §三）。

### 2.3 Linux 侧已核实的覆盖（仅 agent-worker）

- `apps/agent-worker-linux/install.sh`：幂等安装，校验 Python ≥ 3.10、仓库完整，首次生成 `/etc/agent-worker-linux/config.json`（`chmod 600`），渲染 systemd 单元、`daemon-reload` + `enable` + `restart`，备份旧单元为 `*.bak.<时间戳>`，打印回滚步骤（`systemctl disable --now` + 删单元）。
- `apps/agent-worker-linux/README.md`：目标机 Linux + Python 3.10+ 仅标准库；三台 VM（`muse-1-linux` / `muse-2-linux` / `muse-3-linux`）各需唯一 `instance`；GitHub PAT（Contents + Issues 读写）或 surrogate 网关。
- CI：`quality.yml` 的 `agent-linux-process-tests` job 跑 `ubuntu-latest`，测 POSIX 进程组 / SIGTERM / SIGKILL 真实语义；`agent-tests` job 也跑 `apps/agent-worker-linux/tests`（fake GitHub，不访问真实 GitHub）。

### 2.4 缺口

- 主栈 Linux 打包、安装、启停、健康检查脚本**全部缺失**；`start_all.ps1` / `stop_all.ps1` / `smoke.ps1` 均含 Windows 专属调用（`pg_ctl.exe`、`nginx.exe`、`vite` 的 `.exe` 路径、`D:\` 盘符、`Get-Process`、`Start-Process`）。
- `smoke.ps1` 第 1 行起即硬编码 `$Root = 'D:\02_olbr_portal'`，且 §8.4 断言 80/443 仍由 `D:\01_olbr_portal\nginx-1.28.0\nginx.exe` 监听——**冒烟脚本本身不可移植**。
- 无 Linux 版 nginx / PostgreSQL 捆绑运行时方案（`runtime/` 只有 Windows 发行包）；无 Linux 服务注册方案（systemd 单元仅存在于 agent-worker）。
- CI 的 `quality.yml` 明确说明后端有 Windows 专用代码、Linux runner 会误报，因此**主栈质量门禁本身就是 Windows-only**；Linux x64 的交付质量目前无 CI 兜底。

---

## 三、安装验收标准

### 3.1 现有依据（已核实）

| 验收点 | 现有依据 | 文件 |
| --- | --- | --- |
| 隔离标志 | `start_all.ps1` 拒绝无 `ISOLATED_COPY.flag` 的目录启动；`bundle_data.ps1` 灌库前同样校验；`check_all.ps1` 第 1 步校验 | `ops/deploy/start_all.ps1:302`、`ops/deploy/bundle_data.ps1`、`ops/quality/check_all.ps1` |
| 启动预检 | 预检 Python / Node / `backend/main.py` / `node_modules/vite` / `pg_ctl.exe` / 门户 `tsx`，缺失即抛错（干跑时仅告警） | `ops/deploy/start_all.ps1:300-320` |
| 首次 `.env` 生成 | 无 `.env` 时从 `env_template.txt` 复制生成；模板明确“不得写入任何真实口令/Token” | `ops/deploy/start_all.ps1`（`.env` 载入段）、`ops/deploy/env_template.txt:1-13` |
| 端口隔离熔断 | 副本端口不得落在生产端口（80/443/7038/7090/9267/9268/9269/5432/8787/8790）上、不得重复，否则抛错 | `ops/deploy/start_all.ps1:130-140` |
| PG 首次初始化 | 无 `PG_VERSION` 时 `initdb -D pgdata -U myapp -A trust --encoding=UTF8`；随后 `SELECT 1` 轮询至可连接、`SHOW data_directory` 必须等于副本 `pgdata`（防连到生产库）、不存在则 `createdb` | `ops/deploy/start_all.ps1`（§1 便携 PG，约 330–380 行） |
| 数据快照灌入 | `bundle_data.ps1`：`dump`（`pg_dump -Fc`，口令只经 `PGPASSWORD` 进程环境变量传递、用完清除、不进日志）→ `restore`（initdb → pg_ctl start → createdb → `pg_restore`，并补回 role 级 `search_path`，因 `pg_dump` 不含 cluster 级配置） | `ops/deploy/bundle_data.ps1:1-247` |
| 本地权威表校验 | `local_authority_tools.py check` 核对三表行数，冒烟断言 `credentials/task_schedule_config/email_recipients = 20/74/10`，缺失即失败 | `ops/smoke/smoke.ps1`（§8.2）、`docs/workspace/部署说明.md` §八 |
| 健康检查 | 冒烟 §8.3 六项：`(1) /api/health 返回 db_mode=pg`、`(2) /api/wo-dashboard/overview 非空`、`(3) 门户 9277/uniplam/ 返回 200`、`(4a/4b) nginx 8080/8443 /myapp/ 返回 200`、`(5) 8080 提供的 index.html 与 `apps\_fe_dist\index.html` 同 sha256`、`(6) nginx -t 通过`；另有 `SHOW data_directory` 在副本下、02 端口均由副本进程监听 | `ops/smoke/smoke.ps1:990-1060` |
| 健康端点本身 | `GET /api/health`：PG 模式返回 `{"status":"ok","db_mode":"pg","database":"postgresql","db_exists":true}`，不做真实查询（仅报告模式）；sqlite 模式报告文件存在性 | `apps/my_app/backend/routers/system.py:23-28` |
| 停止后确认 | §8.4：02 端口 15 秒内释放、80/443 仍由生产 nginx 监听 | `ops/smoke/smoke.ps1`（§8.4） |
| 质量门禁 | `agent-done-check.mjs --full`：六道快守卫 + 前端构建 + pytest（相对基线）+ 门户 lint；CI `quality.yml`（compileall / ruff E9,F821 / 门户 lint+test+check:docs / my_app 文档守卫 / pytest `-m "not live"`，2 条已知失败显式 deselect）；`auto-merge.yml` 要求 CI 全绿 + `smoke-passed` 标签绑定当前 head SHA，`do-not-merge` 一票否决 | `ops/deploy/agent-done-check.mjs`、`ops/deploy/lib/agent-checks.mjs`、`.github/workflows/quality.yml`、`.github/workflows/auto-merge.yml:152-163` |

### 3.2 缺口

- **无 ZIP 安装器**：没有解压、校验、安装向导或安装脚本；“安装”实际等于“把目录拷过去 + 双击 `start.bat`”，且打包脚本本身不在仓库内（见 §2.2）。
- **无安装验收清单文档**：没有一份“首次安装后逐项打勾”的验收标准；现有检查散落在冒烟脚本（针对 PR）与部署说明（针对人）中，且冒烟只在 `D:\02_olbr_portal` 本机跑。
- **`/api/health` 不做真实 DB 查询**：PG 模式只返回固定 `db_mode=pg`（`system.py:26-27`），不验证能否查表；冒烟的真实 DB 探测是 `psql SELECT 1` + `SHOW data_directory`，不在健康端点内。生产健康检查应要求端点本身能证明 DB 可读。
- **无 seed / 迁移自动执行**：`start_all.ps1` 全文无 `alembic` 调用；`alembic/versions/` 7 个迁移里“只有 CREATE INDEX、零 INSERT”（`docs/workspace/development.md:419`），即**无任何 seed 可重建**；全新安装的库是空库，必须另跑 `bundle_data.ps1` 灌快照，否则“数据库是空的”（`docs/workspace/部署说明.md` §六）。空库安装的验收标准（哪些表/行必须存在）未定义。
- **前端构建产物必须随包，但无校验**：`apps/_fe_dist` 与门户 `dist/` 必须随包（`docs/workspace/部署说明.md` §七），冒烟只校验 `index.html` 的 sha256 一致，未校验整包完整性（无 manifest / 校验和清单——`manifest.json` 在主仓库，不在本仓库）。
- **无版本标识**：ZIP 包内无版本号文件、无 `version.json`、无“当前包对应哪个 commit”的机读标记；冒烟靠 `git rev-parse` 与 head SHA，解压后的 ZIP 包脱离 git 后无法自证版本。

---

## 四、升级和回滚验收标准

### 4.1 现有依据（已核实）

| 验收点 | 现有依据 | 文件 |
| --- | --- | --- |
| 表结构升级 | Alembic：`apps/my_app/alembic/versions/` 共 7 个版本（`0001_baseline`…`0006_manual_stop_flag`、`5415704359dc_phase4_coverage`）；约定 `alembic upgrade head`，回退 `alembic downgrade base`；`alembic/env.py:38-64` 按 `MY_APP_DB_MODE` 解析真实 URL（`alembic.ini` 的 sqlite URL 只是占位） | `apps/my_app/alembic/versions/`、`apps/my_app/docs/manual/03-data-layer.md:175-184`、`AGENTS.md` 三-9 / 四 |
| 回滚手段（表结构） | 每个迁移均有 `downgrade()`；`AGENTS.md` 明确“表结构走 `alembic downgrade`，进程有 watchdog 守护，改大文件前先建 `_bak_<日期>_<说明>` 快照” | `apps/my_app/alembic/versions/*.py`、`AGENTS.md` 三-9 |
| 快照恢复（数据） | `bundle_data.ps1` restore 前若目标库已存在，先 `pg_dump -Fc` 备份到 `TMP\_pre_restore_<时间戳>.dump`；`pg_restore` 失败时抛错称“事务已回滚；副本原有数据仍在导入前备份中” | `ops/deploy/bundle_data.ps1:208-220` |
| 同步事务回滚 | `sync-engine.mjs` 事务式应用：逐文件备份到 `TMP\_pre_sync_<时间戳>`、原子改名、任一步失败整体回滚；源已删除文件隔离到 `TMP\_pre_sync_removed_<时间戳>` | `README.md` §四-5 |
| 本地权威表迁移 | `local_authority_tools.py check/migrate`：先核对（目标为空而源非空时退出码 2 并列出缺表），迁移时先把目标表现状备份到 `TMP\`、事务内替换、逐行哈希校验，复跑 check 须退出码 0 | `docs/workspace/部署说明.md` §八、`ops/deploy/local_authority_tools.py` |
| 服务单元备份 | `install.sh`（agent-worker-linux）重写 systemd 单元前备份旧单元为 `*.bak.<时间戳>`，并打印回滚步骤 | `apps/agent-worker-linux/install.sh` |
| 用户数据保护（同步策略） | 同步白名单绝不同步 `.env`、session、凭据、`node_modules`、`data/db`、构建产物；`prune` 跳过 `node_modules`/`runtime` 并保护 `.env`；`sync_pg_to_sqlite.py` 的 `SKIP_TABLES` 含 `credentials` / `task_schedule_config` / `email_recipients` / `alembic_version`（避免 DROP 本地数据） | `README.md` §四-2、`apps/my_app/backend/scripts/sync_pg_to_sqlite.py:82-87` |
| 冒烟防回归 | `smoke-watcher.ps1`：冒烟前 `git fetch` 并 `checkout --detach` 到 PR head，finally 切回原分支；工作树有未提交改动时跳过；贴标签前重读 PR，head 变了作废 | `ops/deploy/README.md`（smoke-watcher 段） |

### 4.2 缺口

- **无 ZIP 级升级流程**：没有任何脚本或文档描述“已安装的旧 ZIP 如何升级到新 ZIP”（原地覆盖？并行目录 + 切换？数据目录如何保留？）。`start_all.ps1` 不做版本比较、不跑迁移、不校验数据兼容性。
- **启动路径不执行迁移**：`start_all.ps1` 全文无 `alembic` 调用。升级后若只换代码不手动跑 `alembic upgrade head`，后端将以旧表结构跑新代码；没有任何启动时检查拦截这种状态。
- **无“升级前强制备份”门禁**：`bundle_data.ps1` 的 restore 前备份只覆盖“灌快照”这一条路；`alembic upgrade` 本身无前置备份检查，破坏性迁移（见下）可在无备份时执行。Issue 约束“不允许未备份数据库就执行破坏性迁移”目前**没有任何代码或脚本强制执行**，只靠文档约定。
- **破坏性迁移真实存在**：`downgrade()` 中大量 `op.drop_table(...)`（`0001_baseline.py:100-105`、`5415704359dc_phase4_coverage.py:857-881` 等）；`upgrade()` 是否含破坏性操作需逐版本审计，本次只读审查未逐行审计全部 7 个迁移的 `upgrade()`（见 §七）。
- **同步链已停用，升级路径断裂**：`sync-engine.mjs` / `sync-policy.mjs` 已随第一轮重构批 C 停用，只保留备查；`sync_from_main.ps1` 禁止对新目录跑；根 `sync-now.bat` 已删除（`ops/deploy/README.md`“已停用”一节、`AGENTS.md` 二）。即：曾经的“源码同步 + 事务回滚”升级路径已不存在，新的 ZIP 升级路径尚未建立。
- **本地权威表仍会静默丢失**：2026-11-11 实测事故——漏搬三表导致 16 个任务同时报空密码、4 个手动停用任务变回启用（`AGENTS.md` 二、`docs/workspace/部署说明.md` §八）。`local_authority_tools.py check` 只在 `deploy_to_copy.ps1` Step 1.5（已停用的同步链）与冒烟 §8.2 中跑；**ZIP 升级路径上没有任何等价检查**。
- **无生产回滚手册**：无“升级失败后如何回退到上一 ZIP（含数据恢复步骤、回退后健康检查、回退演练）”的文档；`runtime/README.md` 明确“不要把 `pgdata` 当备份：PG 数据文件跨版本/跨机器不保证可移植”，即文件级回退不可行，必须走 `pg_dump` 逻辑备份——但定期备份任务本身不存在（无备份计划任务、无保留策略）。
- **动态表不在迁移内**：动态 eFlow 表不进 migration，由采集任务运行时建表（`03-data-layer.md:184`），因此 `alembic upgrade head` 后表数量少于页面表数是“正常现象”——升级验收时无法仅凭迁移版本判断库结构完整。

---

## 五、生产安全检查标准

### 5.1 现有依据（已核实）

| 检查点 | 现有依据 | 文件 |
| --- | --- | --- |
| 密码哈希 | 门户密码 `scrypt$<salt>$<hash>`（N=16384, r=8, p=1），永不存明文；校验用 `timingSafeEqual`；重置 token 只存 SHA-256 哈希，原始 token 只走 URL fragment，不进日志 | `apps/portal/server/auth/password.ts:1-60` |
| 密钥/配置管理 | `.env` 不入库、不随包分发；模板 `env_template.txt` 明确不得写真实口令/Token，密钥留空由使用者在目标机填写；`bundle_data.ps1` 口令只经 `PGPASSWORD` 进程环境变量、用完清除、不进日志；`check_all.ps1` 第 3 步用 `git ls-files` 检查被跟踪文件里有无 `.env`、数据库、私钥、session | `ops/deploy/env_template.txt:9-13`、`ops/deploy/bundle_data.ps1:13-16`、`ops/quality/check_all.ps1`（步骤 3） |
| 服务权限/隔离 | 启动脚本要求 `ISOLATED_COPY.flag`；副本端口不得落在生产端口上；端口被外部进程占用时拒绝启动、绝不抢端口、绝不杀进程；停止只按 pid 文件（pid + 进程名 + 启动时间三重校验），绝不按端口盲杀；`PATH` 剔除指向源仓库（`\01_RPA\`）的条目，防误用源 venv | `ops/deploy/start_all.ps1`、`ops/deploy/stop_all.ps1` |
| 调度器/外发熔断 | `MY_APP_DISABLE_SCHEDULER=1` 由 `start_all.ps1` **强制**设置，不允许 `.env` 改回 0；不设 `MY_APP_TASK_QUEUE`，不起 watchdog / worker 组 / 计划任务；`START_WA_BOT` 即使 `.env` 误设 1 也强制改回 0（防接管他人 WhatsApp 会话） | `ops/deploy/start_all.ps1`（安全熔断段）、`docs/workspace/部署说明.md` §五 |
| 端口暴露 | 应用服务只绑回环：my_app 后端/前端 `127.0.0.1`（7039/7091）、门户 `127.0.0.1`（9277，`server/index.ts:24,41,104`）、PG `127.0.0.1:55432`、QA/WA 服务 `127.0.0.1`；对外只经本机 nginx | `ops/deploy/start_all.ps1`、`apps/portal/server/index.ts`、`docs/workspace/部署说明.md` §四 |
| PG 认证 | 便携 PG 用 `trust` 认证且只绑回环，`DATABASE_URL` 里没有口令；`start_all.ps1` 校验 `MY_APP_DB_USER` / `MY_APP_DB_NAME` 只允许字母数字下划线（防拼进 psql 命令注入） | `ops/deploy/env_template.txt`、`ops/deploy/start_all.ps1:126-129` |
| TLS | nginx HTTPS：`TLSv1.2/1.3`、`HIGH:!aNULL:!MD5`，自签证书；`client_max_body_size 1024m`；nginx 侧禁止下载备份/归档/源码映射（B5 安全整改） | `nginx.conf.template:64-80` |
| 日志 | 后端 `logging.basicConfig`（级别由环境变量定，默认 INFO）；`TMP\portable-*.err/.out` 分服务落盘；冒烟日志落 `TMP\`（已 gitignore）；`smoke-watcher.ps1` 的 token 依次取环境变量、没有时回落本机 `gh auth token`（只在内存，不落盘）；`bundle_data.ps1` 与冒烟均声明不打印连接串/凭据表内容 | `apps/my_app/backend/main.py:48-49`、`ops/deploy/README.md`（smoke-watcher 段）、`ops/smoke/smoke.ps1`（文件头安全约束） |
| 审计/守卫 | 六道文档一致性守卫进 pre-commit + CI；`agent-done-check.mjs` 把“未验证”单列、禁止写成“通过”；`auto-merge.yml` 要求 `smoke-passed` 绑定当前 head SHA | `AGENTS.md` 四-9、五、`ops/deploy/agent-done-check.mjs` |

### 5.2 缺口

- **nginx 对外监听 0.0.0.0**：`nginx.conf.template` 的 `listen {{HTTP_PORT}}` / `listen {{HTTPS_PORT}} ssl` 未指定绑定地址，默认监听全部接口（含 `[::]`），与“只绑回环”的应用服务形成反差；部署说明 §四的端口表也写绑定 `0.0.0.0`。生产部署应明确要求仅监听内网/指定地址，或在验收标准中写明。
- **自签证书私钥随包分发**：`nginx-1.28.0/certs/portal.key` 是自签证书私钥，文档明确警告“随副本分发等于交出这把钥匙”，要求目标机重新生成并替换（`docs/workspace/部署说明.md` §一）。**但没有任何脚本或检查强制执行**：不重新生成也能正常启动并通过冒烟。
- **PG `trust` 认证无口令**：安全完全依赖“只绑回环”这一层；一旦回环假设被打破（如端口转发、容器网络命名空间共享），数据库无任何认证屏障。生产验收标准应写明该假设及其验证方法。
- **内部服务全是明文 HTTP**：后端、前门、QA 服务之间走 `http://127.0.0.1` 明文；安全依赖回环隔离，无 mTLS 或 token 校验的统一要求（本报告未逐一审计各内部接口的鉴权，见 §七）。
- **无审计日志标准**：未核实到审计日志（谁、何时、改了什么配置/数据）的格式、保留期与轮转要求；`TODO.md` 提到 `backend_runtime_<日期>.log` 单文件上限 100MB 自动切分，但未核实到对应的日志轮转代码与生产日志保留策略。
- **无服务账户/最小权限要求**：所有服务以启动用户的权限运行；无 Windows 服务注册、无专用服务账户、无文件 ACL 要求。`install-smoke-watcher.ps1` 注册计划任务 `OLBR_SmokeWatcher` 是仓库内唯一的服务化动作，且只针对冒烟 watcher。
- **无密钥轮换与吊销流程**：门户 `auth-data/`（加盐哈希）是否随包分发“由交付方自行决定”（`docs/workspace/部署说明.md` §五）；`TMP\_bak_local_authority_*.json` 含明文口令，只留在本机、不入库——但无“迁移完成后删除”的强制检查。
- **VMAX 数仓只读约束未进部署检查**：`AGENTS.md` 四-8 规定 GBase 8a 生产库只能 SELECT，客户端层有只读断言兜底；但部署/冒烟脚本不校验该约束，属流程外约定。

---

## 六、测试矩阵（逐场景现状）

| # | 场景 | Windows x64 现状 | Linux x64 现状 | 结论 |
| --- | --- | --- | --- | --- |
| 1 | 首次安装 | 有章可循：预检 + initdb + `.env` 生成 + 快照灌入（`start_all.ps1`、`bundle_data.ps1`、`部署说明.md` §三）；但无独立安装器、无 ZIP 包内版本标识、无安装验收清单 | 无脚本、无文档 | ⚠ 部分 / ❌ 缺失 |
| 2 | 启动 / 停止 | 完整：`start_all.ps1` 六步 + `stop_all.ps1` 七步 + `-DryRun` + pid 文件托管；冒烟 §8.3/§8.4 覆盖启停 | 仅 agent-worker 有 systemd 启停；主栈无 | ✅ Win / ❌ Linux 主栈 |
| 3 | 健康检查 | 有：`/api/health`（但 PG 模式不做真实查询）+ 冒烟六项 + `nginx -t` + 端口归属断言 | 无（主栈） | ⚠ 端点能力不足 |
| 4 | 数据库初始化与迁移 | 有：initdb / createdb / `pg_restore` / alembic（7 版本）；但启动不跑迁移、无 seed、动态表不在迁移内 | 无（主栈） | ⚠ 链路不完整 |
| 5 | 升级 | **无 ZIP 级升级流程**；仅有 alembic 手动升级、已停用的同步链事务回滚、快照恢复前备份 | 仅 agent-worker `install.sh` 幂等重装 + 单元备份 | ❌ 缺失 |
| 6 | 回滚 | 有片段：alembic `downgrade`、快照恢复前备份、`_bak_` 快照约定；无生产回滚手册、无演练 | 仅 agent-worker 打印回滚步骤 | ⚠ 只有片段 |
| 7 | 备份恢复 | 有：`bundle_data.ps1` dump/restore（`pg_dump -Fc` / `pg_restore` + search_path 补回）；明确 `pgdata` 不能当备份；**无定期备份任务、无保留策略**；本地权威表需手工 `migrate` | 无（主栈） | ⚠ 只有手动快照 |
| 8 | 无外网部署 | 运行期可离线（运行时/瓦片随包）；但**装依赖需外网**（`npm ci` / `pip install` 无离线方案）；打包脚本在主仓库不在本仓库，无法在无外网目标机重新打包 | 未核实 | ⚠ 未闭环 |
| 9 | 异常场景 | 有部分：端口占用拒绝启动、pid 复用防误杀、PG `data_directory` 校验防连错库、冒烟预检拒绝（缺标志/端口被占时退出码 2 且不启停任何进程）、`stop_all` 放 finally、已知失败 deselect（pytest 2 条、冒烟 NUR 2 条） | 未核实 | ⚠ 只有 Windows 部分场景 |

---

## 七、发现的问题与缺口清单（按严重程度排序）

### 🔴 严重（Phase 1 必须解决，否则 ZIP 交付不成立）

1. **ZIP 打包能力不在本仓库内**：`build_olbr_portable.ps1` 与 `scripts/portable/manifest.json`（保留清单真相源）只存在于主仓库 `D:\01_RPA\scripts`，本仓库无任何 ZIP 生成、校验、安装脚本；`*.zip` 在根 `.gitignore:3` 被忽略。ZIP 交付目前是“文档描述 + 主仓库脚本”，不是本仓库可复现、可 CI 验证的能力。
2. **Linux x64 交付能力空白**：主栈（门户/my_app/nginx/PG）全部 Windows-only，冒烟脚本硬编码 `D:\02_olbr_portal`，CI 质量门禁声明 Windows-only；Linux 仅 agent-worker 沙箱 worker 有支持。Issue 要求的“双平台测试矩阵”目前只有一半。
3. **无 ZIP 级升级/回滚流程**：无升级脚本、无版本比较、无升级前强制备份门禁、无生产回滚手册；`start_all.ps1` 不执行 `alembic upgrade head`，升级后极易出现“新代码 + 旧表结构”的静默不一致；同步链（曾提供事务回滚）已停用且无替代。
4. **破坏性迁移无备份门禁**：`downgrade()` 含大量 `drop_table`；Issue 约束“未备份不执行破坏性迁移”无任何代码/脚本强制执行，只靠约定。

### 🟠 重要（Phase 1 应解决）

5. **首次安装无验收标准与版本标识**：无安装清单、无包内版本文件、无整包完整性校验（manifest/校验和不在仓库内）；`/api/health` 在 PG 模式下不做真实查询，不能证明库可用。
6. **无 seed 数据**：alembic 零 INSERT，全新安装的库是空库，必须另灌快照；空库验收标准未定义；`app.db`（2.58 GB）不随包且部分路由硬编码 `d:\01_RPA\...` sqlite 路径，换机即失效（`env_template.txt`）。
7. **本地权威表在升级路径上仍会静默丢失**：三表（`credentials` 20 组 / `task_schedule_config` 74 行 / `email_recipients`）的 check 只存在于已停用的同步链与冒烟中；ZIP 升级无等价检查；2026-11-11 已有实测事故。
8. **自签证书私钥随包分发且无强制轮换**：`portal.key` 随副本分发，文档要求目标机重新生成，但无脚本、无检查强制执行。
9. **无外网部署未闭环**：运行期可离线，但 `npm ci` / `pip install` 需外网，仓库内无离线依赖方案；目标机无法从源码重建依赖。

### 🟡 一般（应记录并排期）

10. **版本口径不一致**：开发文档要求 Node 18+，捆绑运行时与 CI 为 Node 24；端口、目录等信息散落四处，无单一真相源。
11. **nginx 默认监听 0.0.0.0**：与应用服务的回环绑定不一致，生产验收应明确绑定要求。
12. **PG `trust` 无口令**：安全完全依赖回环绑定；该假设无验收验证。
13. **无定期备份任务与保留策略**：备份只有手动 `bundle_data.ps1`；`pgdata` 明确不能当备份。
14. **无审计日志标准**：日志格式、保留期、轮转、审计事件均未核实到统一要求。
15. **内部服务明文 HTTP、无统一鉴权审计**：本次只读审查未逐一审计各内部接口鉴权（见 §八）。

---

## 八、无法只读核实、需 Muse 1 决策的事项

### 无法只读核实（需实机验证，非本文结论）

- 在干净 Windows 机器上从 ZIP 解压到首次启动成功的全流程（含 `npm ci` / `pip install` 是否真需外网、缺失 `node_modules` 时的报错是否清晰）。
- 冒烟在基线 commit `2014a272` 上是否通过（`smoke.ps1` 需在 `D:\02_olbr_portal` 本机跑；本次未执行）。
- `node ops/deploy/agent-done-check.mjs --full` 在基线上的结果（提交前已在本地只读克隆上跑过，基线 `2014a272`，工作区干净、0 文件变更）：6/7 通过。唯一未通过“my_app 文档一致性”是因为本机 Linux 环境没有 `python` 命令（守卫 spawn `python` 立即失败）；同一脚本用 `python3` 直接跑 17/17 通过。pytest 一项显示“全绿”但实际未执行——同因 `python` 缺失导致输出为空、零失败被判通过，且本机无 pytest 模块；前端构建与门户 lint 按脚本设计因未改动相关路径而跳过。结论：自检无本次引入的失败；真实结果以本 PR 的 CI（`quality.yml`，windows-latest）为准。
- 7 个 alembic 迁移的 `upgrade()` 是否含破坏性操作（`DROP COLUMN` / `DROP TABLE` / 数据重写），需逐版本审计；本次只确认了 `downgrade()` 含大量 `drop_table`。
- 各内部接口（`/api/*`、门户 `/uniplam/api/*`、QA 服务）的鉴权覆盖是否完整；日志轮转代码与保留策略的实际实现。
- Linux x64 上能否跑通主栈（无脚本可供核查，需先有方案再验证）。
- 生产切换（11-17 凌晨）后的实际端口/目录归属（`D:\01_olbr_portal` 与 `D:\02_olbr_portal` 两套 nginx 并存，80/443 归属曾来回变过两次，`AGENTS.md` 四-6b）。

### 需 Muse 1 决策

1. **ZIP 打包脚本的归属**：`build_olbr_portable.ps1` 与 `manifest.json` 是从主仓库 `D:\01_RPA` 移植进本仓库，还是本仓库只消费主仓库的打包产物？这决定 Phase 1 的工作范围。
2. **Linux x64 的目标范围**：是“主栈也要上 Linux”（需重写启停/打包/冒烟），还是“Linux 只覆盖 agent-worker，ZIP 交付先只做 Windows”？Issue 写的是双平台矩阵，需明确。
3. **升级策略**：原地覆盖升级 vs 并行目录 + 切换；升级前备份是否作为强制门禁（脚本拦截），以及回滚演练是否纳入验收。
4. **PostgreSQL 独立服务形态**：Issue 约束“PostgreSQL 作为独立服务管理”——是指系统服务（Windows 服务 / systemd）托管，还是维持现状的“便携实例由 `start_all.ps1` 用 `pg_ctl` 拉起”？现状是后者，无服务注册。
5. **证书策略**：自签证书私钥是否继续随包分发（现状），还是改为目标机首次启动时自动生成并强制替换？
6. **Issue 正文的两处笔误请确认**：约束写的是“不允许 **Muse 4** 自己宣布生产验收通过”（应为 Muse 5？）；“所属分支”写的是 `agent/task--muse-5-zip-phase0`（双连字符），与任务指派的 `agent/task-100-muse-5-zip-phase0` 不一致。以哪一个为准？

---

## 九、约束对照表（Issue #100 原文约束 vs 代码现状）

| # | Issue 约束 | 代码现状是否满足 | 缺什么 |
| --- | --- | --- | --- |
| 1 | 不能用开发环境测试代替生产包测试 | ⚠ 部分满足：冒烟（`smoke.ps1`）明确在隔离副本 `D:\02_olbr_portal` 上跑生产包形态（构建产物 + 便携 PG + nginx），且 §8.4 断言生产 80/443 不受影响；但冒烟脚本硬编码 `D:\02` 路径、且本次未实跑 | 需在干净机上实跑冒烟并留档；需明确“生产包”的定义（ZIP 解压物 vs 副本目录） |
| 2 | 不允许 Muse 5（原文作“Muse 4”，疑似笔误）自己宣布生产验收通过 | ✅ 流程上满足：`auto-merge.yml` 要求 CI 全绿 + `smoke-passed`（绑定当前 head SHA）才自动合并；Issue 验收标准要求 Muse 1 审核通过；本报告明确声明不构成验收通过 | 需 Muse 1 确认笔误与审核流程 |
| 3 | 未经 Muse 1 批准不部署生产 | ✅ 流程上满足：生产（`D:\01_olbr_portal`，80/443/5432）与副本隔离；`start_all.ps1` 拒绝生产端口、拒绝无隔离标志启动；`smoke-watcher.ps1` 有硬守卫拒绝在 `D:\01_olbr_portal` 下执行 | 生产部署本身无脚本化流程，需 Phase 1 定义“批准后如何部署” |
| 4 | PostgreSQL 作为独立服务管理 | ⚠ 不满足（按字面理解）：现状 PG 是便携实例，由 `start_all.ps1` 用 `pg_ctl` 拉起、由 `stop_all.ps1` 用 `pg_ctl -m fast stop` 停，无 Windows 服务 / systemd 注册；`install-smoke-watcher.ps1` 注册的计划任务只针对冒烟 watcher | 需 Muse 1 明确“独立服务”的定义（系统服务托管 vs 进程托管） |
| 5 | 不允许升级时覆盖用户数据 | ⚠ 部分满足：同步白名单与 `SKIP_TABLES` 保护凭据/任务配置/收件人；`prune` 保护 `.env`；`bundle_data.ps1` restore 前备份；但 ZIP 升级路径不存在，保护只覆盖已停用的同步链与快照灌入两条路 | 需 ZIP 升级脚本明确数据目录（`pgdata`、`.env`、`portal-auth-data`、三表）保留清单与校验 |
| 6 | 不允许未备份数据库就执行破坏性迁移 | ❌ 不满足：无任何代码/脚本在 `alembic upgrade` 前检查备份；`downgrade()` 含大量 `drop_table`；定期备份任务不存在 | 需在迁移入口加备份门禁（备份不存在/过期则拒绝执行），并定义备份保留策略 |

---

## 十、依据索引

| 结论 | 文件路径 |
| --- | --- |
| 版本与依赖（Python 3.12.10 / Node 24.16.0 / PG 16 / nginx 1.28.0） | `README.md:24,49-51`、`runtime/README.md:9-11` |
| 开发态环境（Node 18+ / Python 3.12） | `docs/workspace/development.md:13,38` |
| 后端依赖清单 | `apps/my_app/requirements.txt`、`apps/my_app/requirements.lock.txt` |
| 启动六步、端口熔断、PG initdb、data_directory 校验 | `ops/deploy/start_all.ps1`（498 行；预检约 300–320 行；PG 约 330–380 行；熔断约 130–140 行） |
| 停止七步、pid 三重校验 | `ops/deploy/stop_all.ps1`（126 行） |
| 配置模板与密钥留空要求 | `ops/deploy/env_template.txt` |
| 部署说明（打包阶段、首次运行、端口、安全边界、排障、本地权威表迁移） | `docs/workspace/部署说明.md`（§一–§八） |
| 打包脚本不在仓库内（引用主仓库路径） | `README.md:42`、`docs/workspace/部署说明.md:3,26-29` |
| 快照 dump/restore、restore 前备份、search_path 补回 | `ops/deploy/bundle_data.ps1`（247 行；备份约 208–220 行） |
| 冒烟 §8.1–§8.4、健康检查六项、硬编码 `D:\02_olbr_portal` | `ops/smoke/smoke.ps1`（1166 行；§8.3 约 990–1060 行） |
| 冒烟薄壳与 watcher | `ops/deploy/smoke-local.ps1`、`ops/deploy/smoke-watcher.ps1`、`ops/deploy/install-smoke-watcher.ps1`、`ops/deploy/README.md` |
| 健康端点 | `apps/my_app/backend/routers/system.py:23-28` |
| 自检脚本与检查实现 | `ops/deploy/agent-done-check.mjs`、`ops/deploy/lib/agent-checks.mjs` |
| 离线质量检查 9 步 | `ops/quality/check_all.ps1` |
| CI（windows-latest 为主、仅 agent-worker 有 ubuntu job） | `.github/workflows/quality.yml` |
| 自动合并门禁（CI 全绿 + smoke-passed 绑定 head SHA） | `.github/workflows/auto-merge.yml:152-163` |
| Alembic 迁移 7 个版本、downgrade 含 drop_table | `apps/my_app/alembic/versions/`（`0001_baseline.py:100-105`、`5415704359dc_phase4_coverage.py:857-881`） |
| 迁移约定、动态表不进 migration、无 seed | `apps/my_app/docs/manual/03-data-layer.md:175-184`、`docs/workspace/development.md:419` |
| 同步链已停用 | `ops/deploy/README.md`（“已停用”一节）、`AGENTS.md` 二 |
| 本地权威表与 2026-11-11 事故 | `docs/workspace/部署说明.md` §八、`AGENTS.md` 二、`ops/deploy/local_authority_tools.py` |
| SKIP_TABLES（凭据三表不受 PG→SQLite 同步覆盖） | `apps/my_app/backend/scripts/sync_pg_to_sqlite.py:82-87` |
| 门户密码 scrypt 哈希 | `apps/portal/server/auth/password.ts:1-60` |
| 门户只绑 127.0.0.1 | `apps/portal/server/index.ts:24,41,104` |
| nginx 监听与 TLS、自签证书 | `nginx.conf.template:64-80` |
| DB 模式熔断（默认 sqlite，PG 需 DATABASE_URL） | `apps/my_app/backend/database.py:26-29` |
| Linux agent-worker 安装与 systemd | `apps/agent-worker-linux/install.sh`、`apps/agent-worker-linux/README.md` |
| 工作区硬性约束（回滚、文档守卫、VMAX 只读） | `AGENTS.md`（三-9、四-8、四-9、五） |
| Issue #100 原文（五项输出、约束、验收标准） | https://github.com/Jouwala/noc-soc-intelligent-platform/issues/100（2026-11-16 09:59 UTC 抓取） |

---

*本报告为只读审查草稿。未修改任何业务代码，未执行安装/启动/冒烟，未创建分支、PR、评论或标签。生产验收通过须由 Muse 1 审核批准，本报告不作此宣布。*
