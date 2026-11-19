# ZIP 交付外部目录（P1-6）：NOC_HOME 模式

> 适用：ZIP 交付 Phase 1（Issue #146，P1-6）。设计依据：Phase 0 审查报告 §6.5
> （`docs/workspace/zip-delivery-phase0-review.md`，PR #106 已于 2026-11-18 合入）。
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
├── data/                 # NOC_DATA_DIR：pgdata、portal-auth-data、bot-data、portal-default.json、app.db（P1-7）
├── logs/                 # NOC_LOGS_DIR：pid 文件、进程 .out/.err、nginx/、nginx-temp/
└── backups/
```

| 变量 | 默认值 | 说明 |
|---|---|---|
| `NOC_HOME` | —（未设即便携模式） | 部署根 |
| `NOC_CONFIG_DIR` | `<NOC_HOME>\config` | `.env`、展开后的 `nginx.conf`、证书 |
| `NOC_DATA_DIR` | `<NOC_HOME>\data` | PG 数据、门户鉴权库、bot 数据、sqlite 库 |
| `NOC_LOGS_DIR` | `<NOC_HOME>\logs` | pid 文件与全部进程/nginx 日志 |
| `NOC_RUNTIME_DIR` | `<NOC_HOME>\runtime` | 解释器与 PG/nginx 发行包 |
| `NOC_ENV_FILE` | `<NOC_CONFIG_DIR>\.env` | 配置文件路径 |

解析集中在 `ops/deploy/noc-paths.ps1`（`Get-NocPaths`），`start_all.ps1` 与
`stop_all.ps1` 点源共用，是唯一真相源；文件缺失时两个脚本都拒绝启动。

nginx 安装目录解析：外部模式优先 `<NOC_RUNTIME_DIR>\nginx-1.28.0`，其次
`<Root>\nginx-1.28.0`；便携模式顺序相反（旧行为优先）。

## 3. 外部模式下派生变量的指向

`start_all.ps1` 在外部模式下派生以下变量（P1-6 建制 + P1-7 新增的业务代码覆盖口）：

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
| `MYAPP_LOG_DIR` | `logs/` | `auth_health_check.py:42`；`portal_qa_ctl.py` / `realtime_proxy_ctl.py` / `backend_watchdog.py`（P1-7 覆盖口） |
| `MY_APP_DB_PATH`（P1-7） | `data/app.db` | `backend/database.py`（uploads/attachments/instance.lock 随之派生） |
| `MW_LINKS_PUBLIC_DIR`（P1-7） | `off`（不写 public 副本） | `services/mw_ume/mw_links_store.py` |
| `BOT_LOG_DIR`（P1-7） | `logs/bot` | bot `src/config.js` |

故意不设：`KNOWLEDGE_DIR`（知识库是随包只读内容，仍在版本目录内）；
`FE_DIST_OUT`（构建产物目录；geojson 刷新仍写它，见 §5 注）。

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

## 5. P1-7 已实施的业务代码路径覆盖（Muse 1 已批）

以下写路径在 P1-6 交付时没有环境变量覆盖口，列入 P1-7 提案；Muse 1 于
2026-11-18 10:55Z 批准后实施。全部默认不变（未设置时与旧行为一致）：

| 写路径 | 证据 | P1-7 实施 |
|---|---|---|
| `db/app.db`（固定相对路径） | `backend/database.py` `DB_PATH` | `MY_APP_DB_PATH` 环境变量覆盖（默认不变） |
| `db/uploads/`、`db/attachments/`、`instance.lock` | `routers/upload.py:19`、`routers/mw_links.py:26`、`routers/feedback.py:62`、`main.py:556`（均由 `DB_PATH` 派生） | 随 `DB_PATH` 覆盖自动外移 |
| `import_topology.py` 自算 `DB_PATH` | `backend/import_topology.py`（现复用 `database.py` 的 `DB_PATH`） | 复用同一覆盖口（含 `MY_APP_DB_PATH`） |
| 前端产物 `geojson` 双写 | `services/mw_ume/mw_links_store.py`（写 `frontend/public/` 与 `FE_DIST_OUT`） | `MW_LINKS_PUBLIC_DIR` 覆盖首选落盘目录，`off` 则不写 public 副本 |
| bot 运行日志 `data/logs` | bot `src/config.js` `logDirAbs`（原写死相对路径） | `BOT_LOG_DIR` 覆盖 |
| 问答/代理服务日志 | `portal_qa_ctl.py`、`realtime_proxy_ctl.py`（`_ROOT` 派生） | `MYAPP_LOG_DIR` 覆盖日志根 |
| 后端 `.env` 搜索路径 | `database.py`（只读 `backend/.env` 与父级 `.env`） | `MY_APP_ENV_FILE` 优先（独立拉起时用；start_all 模式不需要） |
| 看门狗日志 `PROJECT_ROOT/log` | `backend_watchdog.py`（看门狗链专用，副本不跑） | `MYAPP_LOG_DIR` 覆盖 |
| `outputs/auth_health/`（每日巡检报告） | `auth_health_check.py:44` `REPORT_DIR`；`:359/:361` mkdir + 写盘；`task_registry.py:783` 每日 07:00 任务调 `run_auth_health_check()` 必写 md + JSON | 无覆盖口；后续事项（需新增环境变量覆盖口或记为例外，待 Muse 1 定，不静默修） |

注：`FE_DIST_OUT` 在外部模式下仍指向版本目录内的预构建 `_fe_dist`（vite
preview 就从那里提供服务），geojson 刷新会写版本目录内的一个可再生静态文件。
除它之外，下列运行时写版本目录行为也已逐项核实（B3：Muse 1 17:20Z 评审指出
的反例 + 启动路径核查，main 9dad10e 全文检索为证；「唯一已知」的说法不实，
已删除）：

| 写路径 | 证据 | 副本内是否触发 | 覆盖口 / 处置 |
|---|---|---|---|
| `_fe_dist` 内 geojson 文件 | `mw_links_store.py`（写 `FE_DIST_OUT`） | 是（geojson 刷新时） | `MW_LINKS_PUBLIC_DIR=off` 只停 public 副本；整体外移需首次启动复制，留待后续 |
| `data/cxo_dashboard/`、`data/vmax_cxo_daily/` 缓存 | `services/cxo/cxo_dashboard_cache.py:46,192`、`vmax_cxo_daily_cache.py:58,465` | 是：缓存服务含写盘路径（mkdir + 原子写），由看板路由与主程序引用 | 无覆盖口；后续事项（需业务代码改动 + 实测，不静默修） |
| `data/olib_soc_report/`（sqlite/xlsx/json） | `services/olib_soc_report/olib_soc_kpi_store.py:38`、`olib_soc_report_service.py:86,639,991` | 是：报表服务含写盘路径（mkdir + sqlite/xlsx 落盘） | 无覆盖口；后续事项（同上） |
| `data/sp_material_report/` 等邮件报表 Excel | `services/email/kpi_email_report.py:92`（同文件另有 `sp_email`、`sp_weekly`、`ms_sla`、`in_data`、`_remote_kpi`） | 仅任务触发（调度器关闭时定时不触发；手动触发/补跑时会写） | 无数据目录覆盖口；后续事项 |
| `log/worker_restart_marker.*` | `backend/restart_marker.py:37-39,121`（`append_marker` 写） | 仅任务/看门狗链调用时；副本常规运行不触发，手动触发任务时可能写 | 无覆盖口（不读 `MYAPP_LOG_DIR`）；后续事项 |
| `log/`（worker_watchdog 日志） | `backend/worker_watchdog.py:62` | 否：该进程只由生产 `start_hidden.ps1:331` 拉起，副本启动器不拉它 | 生产路径，不在 ZIP 交付范围 |
| `TMP/`（清理任务作用目录） | `services/cleanup/tmp_cleanup.py:44` | 仅每日定时任务；副本调度器关闭不触发 | 无 TMP 目录覆盖口（仅保留天数/干跑两个变量）；后续事项 |
| 各包目录下 `__pycache__` | 启动路径无 `PYTHONDONTWRITEBYTECODE` / `dont_write_bytecode`（grep 核实） | 是：首启 import 即写 | 无；§4 零写入核查须把 `__pycache__` 排除在外 |

不在 P1-7 内：`battery_dashboard.py:39` 的 `d:\01_RPA\...` 硬编码路径是历史遗留，
`env_template.txt` 已有警告，另立事项处理。

## 6. Linux 编排器（P1-4）：`start.sh` / `stop.sh`

Linux 侧对标 `start_all.ps1` / `stop_all.ps1` 的编排器，入口为副本根的
`start.sh` / `stop.sh`（薄壳），逻辑在 `ops/deploy/start.sh` / `stop.sh`，
目录解析在 `ops/deploy/noc-paths.sh`（与 `noc-paths.ps1` 同一套模式/变量/断言）。

| Windows（`.ps1`） | Linux（`.sh`） |
| --- | --- |
| 六步：PG →（D1：alembic upgrade head）→ nginx 配置 → 后端 → 前端 → 门户 → nginx | 相同六步、相同顺序、相同参数，D1 位置一致 |
| pid 文件 `pid\|进程名\|ticks` | pid 文件 `pid\|comm\|starttime`（`/proc/<pid>/comm` + `/proc/<pid>/stat` 字段 22） |
| 端口熔断（生产端口集 + 去重） | 相同端口集与规则 |
| `Test-PortListening`（TcpClient） | Python socket 直连探测（用副本 Python） |
| `Start-Process` 脱离 | 子 shell + `exec`，`trap '' HUP` 使子进程忽略挂起信号 |
| `write_nginx_conf.ps1` 展开配置 + `nginx -t` | 内置 awk 展开器：同一 `nginx.conf.template`、同一 9 个占位符、同一未替换断言，展开后同样跑 `nginx -t` |
| `Stop-Process -Force` | TERM，5 秒未退再 KILL（安全保证相同：只动 pid 文件校验过的本副本进程） |
| `node\node.exe`、`python312\python.exe` | `node/bin/node`、`python312/bin/python3`（按 `python3` → `python3.12` → `python` 探测） |
| `nginx.exe` | `nginx-1.28.0/sbin/nginx` 或 `nginx-1.28.0/nginx` |

可执行位：ZIP 构建时 `.sh` 按后缀置 0o755（`zip-package.mjs`），解压后可直接
执行；仓库检出后须 `chmod +x` 或用 `sh` 显式调用。

验证状态（2026-11-18，Muse VM 夹具实测，桩程序代替真实 PG/nginx/后端）：
编排顺序、pid 文件、端口等待、熔断、干跑、外部模式零写入均已验证；真实
PG/nginx/后端在 Linux 上的行为、真实 `nginx -t`、wa_analysis/bot 路径未验证，
待 P1-8 在 Linux 目标机上实测（Phase 0 附录 B 第 2、14 项）。
