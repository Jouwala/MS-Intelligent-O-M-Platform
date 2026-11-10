# MS-Intelligent-O-M-Platform —— 利比里亚 NOC 数据看板统一门户（便携部署版）

本仓库是 `D:\01_olbr_portal` 的**源码与运维脚本**版本库。

| 项 | 说明 |
| --- | --- |
| 包含 | 统一门户（React + Vite）、my_app（FastAPI 后端 + React 前端）、WhatsApp 知识库 bot、便携部署与运维脚本 |
| 不包含 | 运行时二进制、数据库、构建产物、任何凭据（见下方「不入库的内容」） |
| 配套主仓库 | `Jouwala/my_tools_Liberia_noc1-rpa`（`D:\01_RPA`）—— 开发态主仓库，本仓库是可独立部署的便携副本 |

---

## 一、这是什么

一套在利比里亚运营的可视化运维平台，由 4 个可独立运行、也可整套联动的子系统组成：

| 子系统 | 目录 | 端口（隔离实例） | 说明 |
| --- | --- | --- | --- |
| 统一门户 | `liberia-noc_soc-数据看板统一门户/` | 9277 | 静态壳 + React 应用 + Node 鉴权服务（含加盐密码哈希） |
| my_app | `Task/my_app/` | 7039 / 7091 | FastAPI 后端（采集、KPI、告警、看板数据）+ React 前端 |
| 问答助手挂件 | `liberia-noc_soc-数据看板统一门户/v3/` | — | 嵌入门户的大屏与问答组件 |
| WhatsApp bot | `Task/whatsapp-kb-bot/` | 9279 | Baileys bot，知识库 RAG 问答 |

外加 `nginx-1.28.0/` 作反向代理、`runtime/` 作捆绑运行时（Python 3.12 / Node 24 / PostgreSQL 16）。

---

## 二、快速开始

### 1. 拿代码

```bash
git clone https://github.com/Jouwala/MS-Intelligent-O-M-Platform.git
cd MS-Intelligent-O-M-Platform
```

### 2. 补运行时（必需，仓库里没有）

仓库只存源码，运行时需另行准备。可从主仓库的打包脚本获取：

```powershell
powershell -NoProfile -File D:\01_RPA\scripts\build_olbr_portable.ps1 -Stage runtimes
```

或手工准备这 4 样到 `runtime/` 下：

| 目录 | 内容 | 来源 |
| --- | --- | --- |
| `runtime/python312/` | Python 3.12.10 + fastapi/uvicorn/psycopg2/sqlalchemy/alembic/apscheduler | 官方安装包 |
| `runtime/node/` | Node v24.16.0 | 官方安装包 |
| `runtime/postgresql16/` | PostgreSQL 16 服务端 | 官方安装包 |
| `runtime/pgdata/` | 便携 PG 数据目录 | **首次启动时自动 initdb**，无需预置 |

### 3. 装依赖

```bash
cd Task/my_app/frontend && npm ci
cd ../../liberia-noc_soc-数据看板统一门户 && npm ci
cd ../Task/whatsapp-kb-bot && npm ci
```

### 4. 配置

```bash
cp scripts/env_template.txt .env
```

按需改端口与主机。**`.env` 不入库**（含口令）。

### 5. 启动

```bat
start.bat
```

或用 `my_portal/` 里的分步入口（`0-status` / `1-start-all` / `5-nginx-check` …）。

入口：<https://localhost:8443/>（自签证书，浏览器提示不信任属预期）

详细部署说明见 **`部署说明.md`**。

---

## 三、不入库的内容

以下内容**刻意不进版本库**，但**必须留在磁盘上**才能正常运行 —— 忽略跟踪 ≠ 删除。

| 路径 | 体积 | 原因 |
| --- | --- | --- |
| `runtime/` | 6.5 GB | Python/Node/PG 第三方发行包 + PG 数据目录（不可移植、远超 GitHub 单文件上限） |
| `nginx-1.28.0/` | 37 MB | nginx 二进制 + 地图瓦片 + `certs/portal.key` 自签私钥 |
| `Task/_fe_dist/` | 277 MB | 前端构建产物，可由 `npm run build` 重建 |
| `Task/my_app/db/` | 2.5 GB | `app.db`（SQLite 兜底库）+ 主站点库 xlsx |
| `Task/my_app/data/` | 2.7 GB | 派生 SQLite |
| `Task/whatsapp-kb-bot/data/` | 103 MB | 向量索引 + 问答记录 + WhatsApp 登录凭据 |
| `TMP/` | 182 MB | PG 快照、切换备份等中间态 |
| `Task/whatsapp_session/` | 40 MB | Baileys 设备凭据（auth_info） |
| `*.env` | — | 含明文口令 / API token；模板以 `.env.example` 形式入库 |

另有抓包样本（`url*.txt`）、本地凭据文件（`_local-credentials.js`、`CREDENTIALS_LOCAL_REFERENCE.txt`）被显式排除 —— 它们含**有效会话凭据**。

---

## 四、与主仓库的关系

```
D:\01_RPA  (主仓库 / 开发态)          D:\01_olbr_portal  (本仓库 / 便携部署态)
        │                                        │
        │  scripts/build_olbr_portable.ps1        │
        │  -Stage source                          │
        │    └─ 阶段末尾自动跑修路径 ─────────────►│
        └────────────────────────────────────────►│
                                                 │  同步源码
                                                 │  scripts/post_sync_fix_paths.ps1
                                                 │  ← 修回同步时被冲掉的隔离期路径
```

**同步纪律**（每次从主仓库同步后必做）：

1. **只跑 `-Stage source`**，绝不跑 `-Stage all` / `-Stage config` —— config 会覆盖本副本特有的 `部署说明.md`、`my_portal/status.ps1`。
2. **6 个正在被写入的大 SQLite 已由 manifest 自动跳过**（`ran_kpi_avalability_1day` / `db_sqlite3` / `bp_report` / `olib_soc_report` / `vmax_multi_data` / `soc_trend`，合计约 1.8 GB）—— 文件级复制会撕裂正在写的库。主仓库的 `scripts/portable/manifest.json` 里已登记，无需手工加 `/XF`。
3. **同步后无需再手工跑 `scripts/post_sync_fix_paths.ps1`** —— `-Stage source` 阶段末尾会自动调用它（`build_olbr_portable.ps1` 的 `Invoke-PathFix`）。它负责把本副本隔离期手改过的两处路径冲回 `D:\01_RPA` 之前修回来：
   - `Task/my_app/monitor_fixes/nginx_monitor.ps1` 的 5 个 param
   - 门户 `rotate_nginx_logs.ps1` 的 `$Prefix`

   不修的话，切生产时 nginx watchdog 会去探测旧目录的 nginx，可能起出第二个实例抢端口。

   需要手工跑时（幂等，可反复执行）：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\post_sync_fix_paths.ps1 -Force
   ```

### 换机器 clone 后必做一步

```bash
node scripts/setup-git-hooks.mjs
```

它把 `core.hooksPath` 指向仓库内的 `.githooks/`（该配置是**本地配置**，不入库，每台机器装一次）。装上之后：

| 钩子 | 触发时机 | 作用 |
| --- | --- | --- |
| `.githooks/post-checkout` | clone / checkout / 切分支 | 把两个脚本里的**本机绝对路径**改成本机副本根目录 |
| `.githooks/post-merge` | merge / pull | 同上 |

为什么必须有这一步：本仓库跟踪着两个**含本机绝对路径**的脚本，而路径必须因机器而异。git 检出的是入库时的字节，`D:\01_olbr_portal\...` 会原样出现在新机器上 —— nginx watchdog 会去探测一个不存在的目录，nginx 根本起不来。

> 换机器后 `git status` 会显示那两个文件有差异，这是**预期**的：那正是「本机路径」的体现。在同一台机器上，改写结果是逐字节一致的，status 保持干净。

---

## 五、隔离模式

本副本当前是**完全隔离实例**，端口与生产（`D:\01_RPA`）完全错开，可与生产并行启动：

| | 生产 | 本副本 |
| --- | --- | --- |
| HTTP / HTTPS | 80 / 443 | 8080 / 8443 |
| my_app 后端 / 前端 | 7038 / 7090 | 7039 / 7091 |
| 门户 / bot / 分析 | 9267 / 9269 / 9268 | 9277 / 9279 / 9278 |
| PostgreSQL | 5432 | 55432 |

根目录的 `ISOLATED_COPY.flag` 让 `my_portal_ctl.ps1` 拦截所有 start/stop 命令（防误停生产）。**转正为生产时**：改回生产端口 + 删除该 flag。

> ⚠ 后端仍有约 52 处 `D:\01_RPA` 硬编码路径（SQLite 路径、`Scripts\python.exe`、`Task\_fe_dist`、`Task\eflow_Q&A`）。旧目录保留故能读，但部分服务仍会往旧目录写 → 数据分叉。转正前需收口。

---

## 六、安全提示

本仓库为 **PUBLIC**，故：

- 所有口令、token、私钥、加盐密码哈希**一律不入库**（见「不入库的内容」）。
- **两处历史遗留的硬编码凭据已于 2026-11-10 从源码收口**（同步自主仓库 PR #9）：
  - `Task/my_app/backend/migrations/check_pg_alert_log.py` 的 PG 口令 → 读环境变量 `PG_ALERT_LOG_PASSWORD`；
    未提供时脚本以退出码 2 结束并提示（刻意「不连」而非用弱口令连接）。
  - `Task/whatsapp-kb-bot/scripts/tt-bridge-hidden.vbs` 的 TT 桥接令牌 → 依次读环境变量
    `TT_BRIDGE_TOKEN`、同目录 `.tt_bridge_token` 文件；两者都没有则报错退出。
    该文件已被 `.gitignore` 挡住，**请在本机自建**（内容即原来的令牌值），
    否则手工启动 TT 桥接会报「token is not configured」。
  - ⚠ **改代码不能消除历史泄露** —— 这两个旧值在 git 历史里仍可见，仍需服务端轮换。
- `cxo_dashboard.html` 内嵌抓包数据含有效 `JSESSIONID`，属历史遗留，同样应视为已泄露。
- 抓包样本 `url*.txt` 被显式排除；其中 `vmax_cca/url.txt` 含有效 `JSESSIONID`、`cm_data/new_url.txt` 含有效 `forgerydefense` 会话 cookie。
  - ⚠ **子目录 `.gitignore` 优先级高于根目录** —— 主仓库 `Task/my_app/.gitignore` 若再出现
    `!` 放行（例如 `!/backend/services/vmax_cca/url.txt`），同步过来会**打穿**根目录更严的
    `**/*url*.txt`，把含凭据的抓包重新暴露。改动忽略规则后请两端一起复核。

---

## 七、开发约定

- **i18n 铁律**：新文案一律 `t('key')` 并同时补 zh/en，禁止内联三元与 `label_zh`。
- **标注准确性铁律**：写「某函数访问 X / 上传 FTP」必须 grep 真实调用点，禁止由常量定义推断。
- **快照不进版本库**：`*_bak_*` / `*.bak_*` / `*.bak-*` / `*.bak.*` 一律排除。
- **改动前先跑守卫**：`liberia-noc_soc-数据看板统一门户/scripts/check-*.mjs`（整洁度 / 团队范围 / 文档一致性 / i18n）。
- 详见 `AGENTS.md`。

## 八、许可

内部项目，版权归 ZTE 所有。
