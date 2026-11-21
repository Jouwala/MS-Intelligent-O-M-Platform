# NOC/SOC Intelligent Platform —— 利比里亚 NOC/SOC 智能运维平台（生产仓库）

`D:\02_olbr_portal` 是本平台的**源码仓库 + 生产部署目录**。2026-11-18/19 起，它接管了
本机（MSNOC1 / `192.168.100.20`）的 80/443/7038/7090/9267/9268 与系统 PostgreSQL 5432，
**是实际在跑的那棵树**，不再是什么「便携副本」。

| 项 | 说明 |
| --- | --- |
| 角色 | 生产树（根目录有 `PRODUCTION.flag`，**勿删**）。启停走 `start.bat` / `stop.bat` → `ops\deploy\portal-ctl.ps1` |
| 包含 | 统一门户、my_app、WhatsApp 知识库 bot、eFlow 知识库、Agent Worker / 控制面，以及 `docs/`、`ops/` 下的文档与运维脚本 |
| 不包含 | 运行时二进制、数据库、构建产物、任何凭据（见第三节「不入库的内容」） |
| 版本库 | `Jouwala/MS-Intelligent-O-M-Platform`（`origin`；远端默认分支 **`master`**，分支保护也开在 `master` 上）。另有 `platform` 远程指向 `Jouwala/noc-soc-intelligent-platform`，本地 `main` 跟踪 `platform/main` |
| 与 `D:\01_RPA` | **已完全脱离**：不同步、不互为主副本、没有共享入口；见第四节 |

---

## 一、这是什么

一套在利比里亚运营的可视化运维平台，由若干可独立运行、也可整套联动的子系统组成：

| 子系统 | 目录 | 生产端口 | 说明 |
| --- | --- | --- | --- |
| 统一门户 | `apps/portal/` | 9267（nginx 的 `/uniplam/`） | 静态壳 + React 应用 + Node 鉴权服务（含加盐密码哈希） |
| my_app | `apps/my_app/` | 7038 / 7090（`/api/`、`/myapp/`） | FastAPI 后端（采集、KPI、告警、看板数据）+ React 前端 |
| WhatsApp bot | `apps/whatsapp-kb-bot/` | 按需 | Baileys bot + 知识库 RAG 问答 |
| eFlow 知识库 | `apps/eflow_Q&A/` | — | 问答 / 排障文档与取数工具 |
| Agent Worker / 控制面 | `apps/agent-worker/`、`apps/control-plane/` | — | 多 worker 任务调度与执行 |
| nginx | `nginx-1.28.0/` | 80 / 443 | 反向代理与静态壳（本机生效目录就是这里） |
| 便携运行时 | `runtime/` | — | **只服务隔离副本 / 打包**；生产用系统 Python 3.12、系统 Node、系统 PG 5432 |

## 二、快速开始

### 1. 启停（生产）

```bat
cd /d D:\02_olbr_portal
start.bat                  REM 转发 ops\deploy\portal-ctl.ps1 -Action start
stop.bat                   REM 转发 ops\deploy\portal-ctl.ps1 -Action stop
ops\shortcuts\0-status.bat REM 只读查端口 / 健康 / 计划任务
```

也可以直接调 `powershell -NoProfile -File ops\deploy\portal-ctl.ps1 -Action status|start|stop|restart`。
桌面入口 `ops\deploy\olbr-start.bat` / `olbr-stop.bat` / `olbr-status.bat` 与之等价。

入口：

- 首页 <https://localhost/>（443，自签证书提示不信任属预期；80 与 443 共用同一份 location 清单）
- 门户 <http://192.168.100.20/uniplam/>
- my_app <http://192.168.100.20/myapp/>
- 同事 / 其他机器用 `http://192.168.100.20/…`；`192.168.100.19` 是 FineBI 与 .19 自己那台 nginx，**不是本门户的入口**

### 2. 依赖

仓库只存源码，`node_modules/` 不入库。新克隆的机器按需装：

```bash
cd apps/my_app/frontend && npm ci
cd ../../apps/portal && npm ci
cd ../whatsapp-kb-bot && npm ci
```

生产机上这些目录已就绪，不要为了「对齐」重装。

> **运行前提**：本目录含源码、静态资产与 `runtime\` 便携运行时，但**生产启动用的是本机系统
> Python 3.12 / Node 24 / PostgreSQL 16**（数据在系统 PG 数据目录），个别任务还依赖本机的
> CN EMS Java 客户端、Chrome/Edge 与 FineBI 导出目录。完整清单见 `docs/workspace/部署说明.md` §一「运行前提」。

### 3. 配置

- 后端：`apps/my_app/backend/.env`（`DATABASE_URL`、调度器开关、上游凭据等）
- bot：`apps/whatsapp-kb-bot/.env`
- 根 `.env` 是**隔离副本 / 打包**用的端口模板，生产启动不读它；生产端口由各启动脚本与
  `nginx-1.28.0/conf/nginx.conf` 决定。**不要**在生产树上跑 `write_nginx_conf.ps1`（它按根 `.env`
  展开，会把 80/443 覆盖成 8080/8443）。

### 4. 新克隆 / 换机器

```bash
node ops/deploy/setup-git-hooks.mjs
```

把 `core.hooksPath` 指向仓库内的 `.githooks/`（本地配置，不入库，每台机器装一次）。装好后
`post-checkout` / `post-merge` 会按角色标记把两个监控脚本里的本机绝对路径改成本机路径：
有 `PRODUCTION.flag` 写生产端口 80，否则按 `.env` 写隔离端口。不装的话，钩子不跑，
nginx 看门狗会去探测一个不存在的目录。

> 刚 checkout 完 `git status` 可能显示那两个监控脚本有差异，这是**预期**的（本机路径的体现）；
> 在同一台机器上重复执行结果是逐字节一致的。

详细运维说明见 **`docs/workspace/部署说明.md`**；生产启停与角色标记见 **`ops/deploy/README.md`**。

---

## 三、不入库的内容

以下内容**刻意不进版本库**，但**必须留在磁盘上**才能正常运行 —— 忽略跟踪 ≠ 删除。

| 路径 | 体积（约） | 原因 |
| --- | --- | --- |
| `runtime/` | 6.5 GB | Python/Node/PG 第三方发行包 + PG 数据目录（不可移植、远超 GitHub 单文件上限） |
| `nginx-1.28.0/` | 37 MB | nginx 二进制 + 地图瓦片 + `certs/portal.key` 自签私钥 |
| `apps/_fe_dist/` | 277 MB | 前端构建产物，可由 `npm run build` 重建 |
| `apps/my_app/db/` | 2.5 GB | `app.db`（SQLite 兜底库）+ 主站点库 xlsx |
| `apps/my_app/data/` | 2.7 GB | 派生 SQLite |
| `apps/whatsapp-kb-bot/data/` | 103 MB | 向量索引 + 问答记录 + WhatsApp 登录凭据 |
| `TMP/` | 182 MB | PG 快照、切换备份等中间态 |
| `apps/whatsapp_session/` | 40 MB | Baileys 设备凭据（auth_info） |
| `*.env` | — | 含明文口令 / API token；模板以 `ops/deploy/env_template.txt` 形式入库 |

另有抓包样本（`url*.txt`）、本地凭据文件（`_local-credentials.js`、`CREDENTIALS_LOCAL_REFERENCE.txt`）被显式排除 —— 它们含**有效会话凭据**。

---

## 四、与 `D:\01_RPA` / `D:\01_olbr_portal` 的关系：已完全脱离

**本仓库不从 `D:\01_RPA` 同步任何东西，也不向它回写任何东西。** 两者没有 git 关联，
没有共享启动脚本，没有「主仓库 → 副本」的上下游关系。

已经删掉 / 停用的入口（**不要恢复**）：

| 东西 | 现状 |
| --- | --- |
| 根 `sync-now.bat` | 已删除（第一轮重构批 C） |
| `D:\01_RPA\scripts\deploy_to_copy.ps1` | 属 01，本仓库不调用；文档里出现只作历史说明 |
| `ops/deploy/sync-engine.mjs`、`sync-policy.mjs`、`sync_from_main.ps1` | 文件保留备查，**禁止对本目录运行** |
| `ops/deploy/bundle_data.ps1` | 同属已停用的同步链，默认源仍指向 01，**禁止运行** |

历史脉络（解释「为什么旧文档还在讲同步」）：

1. 2026-11-13：第一轮重构把代码从 `D:\01_RPA` 收拢到 `D:\02_olbr_portal`，当时 02 是**隔离副本**，
   靠 `sync-now.bat` 从 01 单向同步（ADR 0002）。
2. 2026-11-18/19：切生产，02 接管 80/443/7038/7090/9267/9268 与 PG 5432，`D:\01_olbr_portal`
   退役为回退目标（ADR 0003）。同步链在批 C 删除，02 从此独立。
3. 之后所有「主仓库 → 02 副本」「双击 sync-now.bat」的段落都是**第 1 阶段的历史残留**，
   不代表现状；看到就改掉，不要照着做。

两棵旧目录现在的定位：

- **`D:\01_olbr_portal`**：切换前的旧生产，**只作为回退目标**存在，只有
  `ops/deploy/cutover/rollback-to-01.ps1` 会碰它。日常不在这里改代码、不起服务。
- **`D:\01_RPA`**：旧的编辑区单体仓库，与 02 无 git 关系。它的 `nginx-1.28.0` **已不持有本机 80/443**，
  不要再改它；`Y:\01_RPA\nginx-1.28.0\` 是 **.19 那台**机器的线上目录，更不要动。

> 唯一保留的一条「从旧目录搬东西」的脚本是 `ops/deploy/sync-ui-config-from-01.ps1`：把旧机三份
> 全局 UI 配置（`ui_tool_*.json` / `ui_menu_layout.json`）迁到本树，默认干跑、`-Apply` 才写、写前校验 key。
> 那是**一次性迁移工具**，不是持续同步，跑之前先确认旧机确实还有需要搬的配置。

---

## 五、运行模式与角色标记（2026-11-18/19 已转正为生产）

本目录不再是与生产并行的隔离副本：**它自己就是生产**。根目录有 `PRODUCTION.flag`、**没有**
`ISOLATED_COPY.flag`；`D:\01_RPA` 与 `D:\01_olbr_portal` 的进程都不再服务本机 80/443，
后者只作回退目标（见第四节）。

| | 本目录（现状 / 生产） | 新打包的隔离副本（默认） |
| --- | --- | --- |
| HTTP / HTTPS | 80 / 443 | 8080 / 8443 |
| my_app 后端 / 前端 | 7038 / 7090 | 7039 / 7091 |
| 门户 / bot / 分析 | 9267 / 9269 / 9268 | 9277 / 9279 / 9278 |
| 实时代理 FMS / VMAX | 8787 / 8790 | 8887 / 8890 |
| PostgreSQL | 本机已安装的 16（5432） | 便携版 55432 |

同一套脚本要服务「这台机器是生产」和「这台机器是便携 / 冒烟副本」两种角色，角色**只由根目录的两个标记决定，且必须二选一**（两者都不入库）：

| 标记 | 谁写 | 含义 | 端口 |
| --- | --- | --- | --- |
| `PRODUCTION.flag` | 切生产时人工放（**当前本树就有，勿删**） | 生产树，调度器在跑 | 80/443/7038/7090/9267/9268，PG 5432 |
| `ISOLATED_COPY.flag` | `provision-smoke-runner.ps1` / 打包脚本 | 便携 / 冒烟副本 | 8080/8443/7039/7091/9277，便携 PG 55432 |

**两个启动器不能混用**：

| 目录状态 | `start.bat` / `stop.bat` 实际执行 | 原因 |
| --- | --- | --- |
| 有 `ISOLATED_COPY.flag`（隔离副本） | `ops\deploy\start_all.ps1` / `stop_all.ps1` | 强制 `MY_APP_DISABLE_SCHEDULER=1`、自带便携 PG、拒绝生产端口 |
| 有 `PRODUCTION.flag`（本目录） | `ops\deploy\portal-ctl.ps1 -Action start\|stop` | 生产编排：my_app → 门户 → bot → nginx；桌面 `olbr-start` / `olbr-stop` 快捷方式走的就是它 |

2026-11-16 修复的正是这里：转正只改了 `.env` 并删了 flag，`start.bat` 仍直连 `start_all.ps1`，
于是被「副本端口 80 与生产端口冲突」拦住，整台机器起不来。现在由 flag 决定分流：
`start_all.ps1` 见到 `PRODUCTION.flag` 直接 throw（它会改 `apps\my_app\backend\.env`、起 55432 的
第二套 PG、往 8080/8443 起第二套服务），在已转正目录里直接调它仍会拒绝生产端口 —— 这是刻意的。
`portal-ctl.ps1` 的起停命令也受同一个 flag 拦截（防误停生产），flag 删除后才会放行。

- 根 `.env` 是**隔离副本 / 打包**用的端口模板（8080/8443/7039/7091/9277/55432、
  `MY_APP_DISABLE_SCHEDULER=1`），生产启动不读它；生产端口由各启动脚本与
  `nginx-1.28.0\conf\nginx.conf` 决定，后端开关在 `apps\my_app\backend\.env`（生产为
  `MY_APP_DISABLE_SCHEDULER=0`）。**不要**在生产树上跑 `write_nginx_conf.ps1` —— 它按根 `.env`
  展开，会把 80/443 覆盖成 8080/8443。
- 想**退回隔离试跑**：把根 `.env` 端口改回 8080/8443/7039/7091/9277/55432，重建 `ISOLATED_COPY.flag`
  并删掉 `PRODUCTION.flag`，然后先停掉生产栈（`stop.bat`）。
- `start_all.ps1` 的隔离保障仍适用于隔离副本：副本 `.env` 优先于继承的进程环境（防生产污染）、
  拒绝任何落在生产端口上的配置、派生变量无条件按副本根覆盖、清除 `PYTHONPATH/PYTHONHOME/NODE_PATH`、
  强制 `MY_APP_DISABLE_SCHEDULER=1` 与 `START_WA_BOT=0`、启动前预检核心入口、PG 写入前校验
  `data_directory` 属于副本。实时代理控制器按「进程命令行含本副本脚本绝对路径」验归属，
  **绝不复用或击杀生产 8787/8790 代理**。
- 生产模式下这些都不适用：调度器按 `apps\my_app\backend\.env` 的 `MY_APP_DISABLE_SCHEDULER=0`
  正常运行（生产树是唯一采集端），PG 是本机 5432，`start_hidden.ps1` 负责 backend/worker watchdog 与按需拉起。
- 冒烟**不在生产树里跑**：用 `ops/deploy/provision-smoke-runner.ps1 -Apply` 在项目内生成
  `.smoke\` 副本（已 gitignore），再按 `ops/deploy/README.md` 的说明装 watcher。
- 生产树只应有 `PRODUCTION.flag`；两个都在或都不在时，入口一律拒绝执行（角色不明宁可不跑）。

> 源码侧的 `D:\01_RPA` 硬编码路径已于 2026-11-11 收口：业务数据目录、邮件/代理解释器（`sys.executable`）、
> 前端产物目录（`FE_DIST_OUT`/相对路径）、Chrome、cn_ems 模板、eflow 问答工具均改为按副本位置定位或 env 覆盖。

细节与事故背景见 `ops/deploy/README.md`「角色标记」与 `docs/adr/0003-production-vs-isolated-markers.md`。

---

## 六、安全提示

本仓库为 **PUBLIC**，故：

- 所有口令、token、私钥、加盐密码哈希**一律不入库**（见「不入库的内容」）。
- **两处历史遗留的硬编码凭据已于 2026-11-10 从源码收口**：
  - `apps/my_app/backend/migrations/check_pg_alert_log.py` 的 PG 口令 → 读环境变量 `PG_ALERT_LOG_PASSWORD`；
    未提供时脚本以退出码 2 结束并提示（刻意「不连」而非用弱口令连接）。
  - `apps/whatsapp-kb-bot/scripts/tt-bridge-hidden.vbs` 的 TT 桥接令牌 → 依次读环境变量
    `TT_BRIDGE_TOKEN`、同目录 `.tt_bridge_token` 文件；两者都没有则报错退出。
    该文件已被 `.gitignore` 挡住，**请在本机自建**（内容即原来的令牌值），
    否则手工启动 TT 桥接会报「token is not configured」。
  - ⚠ **改代码不能消除历史泄露** —— 这两个旧值在 git 历史里仍可见，仍需服务端轮换。
- `cxo_dashboard.html` 内嵌抓包数据含有效 `JSESSIONID`，属历史遗留，同样应视为已泄露。
- 抓包样本 `url*.txt` 被显式排除；其中 `vmax_cca/url.txt` 含有效 `JSESSIONID`、`cm_data/new_url.txt` 含有效 `forgerydefense` 会话 cookie。
  - ⚠ **子目录 `.gitignore` 的规则优先级高于根目录** —— 若 `apps/my_app/.gitignore` 里出现
    `!` 放行（例如 `!/backend/services/vmax_cca/url.txt`），会**打穿**根目录更严的
    `**/*url*.txt`，把含凭据的抓包重新暴露。改忽略规则后请两端一起复核。

---

## 七、开发约定

- **改完必须自检**：`node ops\deploy\agent-done-check.mjs`（默认快守卫）；改过前端 / 后端再加 `--full`。
- **i18n 铁律**：新文案一律 `t('key')` 并同时补 zh/en，禁止内联三元与 `label_zh`。
- **标注准确性铁律**：写「某函数访问 X / 上传 FTP」必须 grep 真实调用点，禁止由常量定义推断。
- **快照不进版本库**：`*_bak_*` / `*.bak_*` / `*.bak-*` / `*.bak.*` 一律排除。
- **改动前先跑守卫**：门户 `npm run lint` / `npm run build` / `npm run check:docs` / `npm run check:i18n`；
  my_app `npm run check:i18n` + `python tests\test_doc_consistency.py`。
- **合入走 PR**：分支 → PR 到 `master`（远端默认分支，分支保护开在这里）→ CI 全绿且 `smoke-passed` 后合并；**不要直接推 `master`**。
- 更多约束见 `AGENTS.md` 与 `docs/workspace/development.md`。

## 八、许可

内部项目，版权归 ZTE 所有。
