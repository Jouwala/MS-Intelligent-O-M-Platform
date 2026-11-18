# ZIP 交付外部目录（P1-6）：NOC_HOME 模式

> 适用：ZIP 交付 Phase 1（Issue #146，P1-6）。设计依据：Phase 0 审查报告 §6.5
> （`docs/workspace/zip-delivery-phase0-review.md`，PR #106 未合入前见任务分支）。
> 本文件只写 P1-6 落地后的行为与核查法，不重复设计论证。

## 1. 两种模式

| 模式 | 触发 | 路径来源 | 版本目录写入 |
|---|---|---|---|
| 便携（legacy） | 未设 `NOC_HOME` | 全部由脚本所在根 `$Root` 派生，与旧行为逐行一致 | 旧行为（`.env`、`TMP/`、`backend\.env`、`runtime/pgdata` 等） |
| 外部（external） | 设了进程环境变量 `NOC_HOME` | 部署根下的外部目录（见 §2） | **零写入**（启动脚本断言，见 §4） |

`NOC_HOME` 与 `NOC_*_DIR` **只从进程环境读**，写在 `.env` 里无效——因为 `.env`
文件本身的位置就靠它们定。由首次启动脚本 / 部署包装器在拉起前设置。

## 2. 外部目录布局与变量

```
<NOC_HOME>/
├── versions/vX.Y.Z/      # 版本目录（$Root，脚本所在处；只读）
├── current -> versions/vX.Y.Z/
├── runtime/              # NOC_RUNTIME_DIR：python312、node、postgresql16、nginx-1.28.0
├── config/               # NOC_CONFIG_DIR：.env、nginx.conf、certs/
├── data/                 # NOC_DATA_DIR：pgdata、portal-auth-data、bot-data、portal-default.json
├── logs/                 # NOC_LOGS_DIR：pid 文件、进程 .out/.err、nginx/、nginx-temp/
└── backups/
```

| 变量 | 默认值 | 说明 |
|---|---|---|
| `NOC_HOME` | —（未设即便携模式） | 部署根 |
| `NOC_CONFIG_DIR` | `<NOC_HOME>\config` | `.env`、展开后的 `nginx.conf`、证书 |
| `NOC_DATA_DIR` | `<NOC_HOME>\data` | PG 数据、门户鉴权库、bot 数据 |
| `NOC_LOGS_DIR` | `<NOC_HOME>\logs` | pid 文件与全部进程/nginx 日志 |
| `NOC_RUNTIME_DIR` | `<NOC_HOME>\runtime` | 解释器与 PG/nginx 发行包 |
| `NOC_ENV_FILE` | `<NOC_CONFIG_DIR>\.env` | 配置文件路径 |

解析集中在 `ops/deploy/noc-paths.ps1`（`Get-NocPaths`），`start_all.ps1` 与
`stop_all.ps1` 点源共用，是唯一真相源；文件缺失时两个脚本都拒绝启动。

nginx 安装目录解析：外部模式优先 `<NOC_RUNTIME_DIR>\nginx-1.28.0`，其次
`<Root>\nginx-1.28.0`；便携模式顺序相反（旧行为优先）。

## 3. 外部模式下派生变量的指向

`start_all.ps1` 只设代码**已支持环境变量覆盖**的项（不改业务代码）：

| 变量 | 外部模式指向 | 代码读取点 |
|---|---|---|
| `PGDATA` | `data/pgdata` | `start_all.ps1` 派生（旧行为即覆盖） |
| `PORTAL_AUTH_DATA_DIR` | `data/portal-auth-data` | `apps/portal/server/index.ts:51-52` |
| `PORTAL_DEFAULT_FILE` | `data/portal-default.json` | `apps/portal/server/index.ts:46-47` |
| `PORTAL_QA_NODE` / `TT_PYTHON` | 运行时目录下的解释器 | `portal_qa_ctl.py` / bot `pythonPath.js` |
| `CHAT_CA_FILE` | `config/certs/liberia-noc-ca.pem` | bot `src/config.js:55`（缺失只告警） |
| `INDEX_PATH` / `LOOKUPS_PATH` | `data/bot-data/*.json` | bot `src/config.js:286-289` |
| `AUTH_DIR` / `NOTIFY_AUTH_DIR` | `data/bot-data/auth*` | bot `src/config.js:291` / `src/notify.js:78` |
| `QA_LOG_PATH` | `data/bot-data/qa.sqlite` | bot `src/config.js:297` |
| `MYAPP_LOG_DIR` | `logs/` | `auth_health_check.py:42` |

故意不设：`KNOWLEDGE_DIR`（知识库是随包只读内容，仍在版本目录内）；
`FE_DIST_OUT`（构建产物目录，见 §5 的 P1-7 项）。

外部模式下的行为差异（除路径外）：

- `.env` 不存在时从模板生成到 `NOC_ENV_FILE`（不是版本目录）。
- 不生成 `apps/my_app/backend/.env`——`database.py` 的 `load_dotenv(override=False)`
  保证进程环境优先，连接串已由启动脚本放进进程环境；服务必须经启动脚本拉起。
- nginx 配置展开到 `config/nginx.conf`，`nginx -p` 仍指安装目录（`mime.types`
  相对它解析），`-c` 用绝对路径；日志/pid/临时目录/证书一律用模板占位符
  `{{NGINX_LOGS}}` / `{{NGINX_TEMP}}` / `{{NGINX_CERTS}}` 指向外部目录。
- 证书由首次启动脚本在 `config/certs/` 生成（自签）或由部署方提供正式证书；
  证书文件**不进 ZIP**（`*.pem` 硬排除）。

## 4. 零写入核查

外部模式启动时，`Assert-NocPathsOutsideRoot` 先断言全部写目标不在版本目录内，
否则直接抛错（防 `NOC_HOME` / `NOC_*_DIR` 配错）。

启动后核查（版本目录在启动后零写入，P1-6 验收标准）：

```powershell
# Windows：启动前记一份快照，启动后比对（除读取时间外，文件清单与写入时间应不变）
Get-ChildItem -Recurse <版本目录> | Select-Object FullName, Length, LastWriteTime |
  Export-Csv before.csv -NoTypeInformation
# …启动、跑一阵、停止…
Get-ChildItem -Recurse <版本目录> | Select-Object FullName, Length, LastWriteTime |
  Export-Csv after.csv -NoTypeInformation
Compare-Object (Import-Csv before.csv) (Import-Csv after.csv) -Property FullName, Length, LastWriteTime
```

```sh
# Linux：touch 一个标记文件，启动后找比它新的文件（应为空）
touch /tmp/p16-mark
# …启动、跑一阵、停止…
find <版本目录> -newer /tmp/p16-mark -print
```

干跑可先看目录规划：`start_all.ps1 -DryRun` 会打印两种模式的完整路径表，
不启动任何进程（外部模式下断言同样生效）。

## 5. 仍需 P1-7（改业务代码，另走确认）的写路径

以下写路径没有环境变量覆盖口，启动脚本改不动，列入 P1-7 提案等 Muse 1 确认
后再改代码；P1-6 交付时它们仍是已知缺口：

| 写路径 | 证据 | P1-7 方向 |
|---|---|---|
| `db/app.db`（固定相对路径） | `backend/database.py:31` `DB_PATH` | 加 `MY_APP_DB_PATH` 环境变量覆盖（默认不变） |
| `db/uploads/`、`db/attachments/`、`instance.lock` | `routers/upload.py:19`、`routers/mw_links.py:26`、`routers/feedback.py:62`、`main.py:556`（均由 `DB_PATH` 派生） | 随 `DB_PATH` 覆盖自动外移 |
| `import_topology.py` 自算 `DB_PATH` | `backend/import_topology.py:10`（不 import `database.py`） | 复用同一覆盖口 |
| 前端产物 `geojson` 双写 | `services/mw_ume/mw_links_store.py:376-382`（写 `frontend/public/` 与 `FE_DIST_OUT`） | 写目标改由环境变量定 |
| bot 运行日志 `data/logs` | bot `src/config.js:292` `logDirAbs`（写死相对路径） | 加 `BOT_LOG_DIR` 覆盖 |
| 问答/代理服务日志 | `portal_qa_ctl.py:38,157`、`realtime_proxy_ctl.py:295-298`（`_ROOT` 派生） | 日志根改由环境变量定 |
| 后端 `.env` 搜索路径 | `database.py:13-19`（只读 `backend/.env` 与父级 `.env`） | 可选：`MY_APP_ENV_FILE` 覆盖 |
| 看门狗日志 `PROJECT_ROOT/log` | `backend_watchdog.py:44`（看门狗链专用，副本不跑） | 随看门狗链改造一并处理 |

不在 P1-7 内：`battery_dashboard.py:39` 的 `d:\01_RPA\...` 硬编码路径是历史遗留，
`env_template.txt` 已有警告，另立事项处理。
