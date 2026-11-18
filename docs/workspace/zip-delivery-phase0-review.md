# ZIP 交付 Phase 0：生产构建与打包只读审查报告

| 项 | 内容 |
| --- | --- |
| 负责人 | Muse 4（Production Build & Packaging Engineer） |
| 任务 | Issue #99（ZIP 交付 Phase 0：生产构建与打包只读审查） |
| 审查基线 | main HEAD `2014a27`（`2014a272f44b1850497d4662eddf46566223c345`，2026-11-16 09:29Z，PR #102 合并） |
| 审查日期 | 2026-11-16 |
| 审查方式 | 只读。浅克隆公开仓库到本地做静态分析，未改任何业务代码，未做任何 GitHub 写操作 |
| 拟定入库路径 | `docs/workspace/zip-delivery-phase0-review.md`（本文件为工作区草稿，PR 时落入该路径） |

> 约束声明：本报告遵守任务约束——不默认用 Docker；不重建现有业务架构；不擅自换数据库；
> 不把 PostgreSQL 数据目录打进程序 ZIP；不把密钥写入 ZIP；不允许首次启动时从互联网下载生产依赖；
> 配置、数据、日志、上传文件必须独立于版本目录。第 6 节的方案按这些约束设计。

## 0. 结论总览

1. **技术栈**：4 个子系统 + nginx 反代 + 捆绑运行时。my_app 是 FastAPI（Python 3.12）+ React 19/Vite 前端；
   门户是 React 19/Vite + Express/TS 鉴权服务；WhatsApp bot 是 Node（Baileys）+ Python 分析脚本；
   另有 Python 的 agent-worker / control-plane 调度代码。运行时固定为 Python 3.12.10、Node v24.16.0、
   PostgreSQL 16、nginx 1.28.0。
2. **依赖**：Python 用 `requirements.txt`（`>=` 范围）+ `requirements.lock.txt`（95 行精确锁定）；
   Node 有 4 个独立的 `package.json` + lockfile v3，无根 `package.json`、无 workspace。
   全部依赖安装都需联网（含 SheetJS CDN 的 `xlsx` tarball 与 Playwright 浏览器下载），离线交付必须在构建期预装。
3. **启动方式**：生产与隔离副本各一套 Windows 启动链。生产走各子系统的 `start.bat` →
   `start_hidden.ps1`（watchdog + 10 组 worker）；隔离副本走根 `start.bat` → `ops/deploy/start_all.ps1`
   六步编排（便携 PG → nginx 配置 → 后端 → 前端 → 门户 → nginx），端口全部错开，调度器强制关闭。
   无 Docker、无 systemd（应用侧）、无统一进程管理器。
4. **数据库**：双模。`MY_APP_DB_MODE=pg` 是生产事实（PostgreSQL 16，SQLite 文件名 stem 映射为 PG schema，
   共 10 个 search_path）；`sqlite` 是默认熔断回退。结构迁移用 Alembic（7 个版本），动态表不在迁移管理内；
   三张本地权威表（`credentials` / `task_schedule_config` / `email_recipients`）固定写 SQLite，
   迁移必须单独搬。PG 数据目录在 `runtime/pgdata/`（首次启动 initdb），绝不进 ZIP。
5. **Windows/Linux 兼容性**：生产启动链几乎全是 Windows 专属（62 个 `.bat/.ps1/.vbs/.cmd`，仅 1 个 `.sh`）；
   实证问题包括 LF 检出的根 `.bat`、只认 `.exe` 的 Python 探测、Windows 互斥量/taskkill 的 watchdog、
   PS5.1 专属的启动脚本、Windows 计划任务保活、`.bat` CRLF 与 nginx 配置无 BOM 的字节级要求。
   Linux 侧唯一现成资产是 agent-worker 的 systemd 模板，不可直接复用于应用服务。
6. **ZIP 方案**：双产物模型——应用 ZIP（源码 + 预构建前端 + 预装依赖，版本目录只读）与运行时 ZIP
   （Python/Node/PG/nginx 发行包）分离；`vX.Y.Z` 标签 + `build-info.json` + SHA256 校验；
   GitHub Actions 在 `windows-latest`/`ubuntu-latest` 双 runner 上构建并上传 Release；
   配置/数据/日志/上传一律落在版本目录外的外部目录，首次启动零下载。

关键风险（Phase 1 前必须先定）：① 新鲜空 PG 上 `alembic upgrade head` 是否能独立建出 schema
未核实；② Linux 上整套服务从未跑通过，watchdog/worker/计划任务链无 Linux 等价物；
③ `node_modules` 与 Python venv 打进 ZIP 后的体积未实测；④ `db/app.db` 路径写死在源码相对位置，
“数据独立于版本目录”需要后续代码改动配合（Phase 0 只审查，不改代码）。

---

## 1. 技术栈清单

### 1.1 子系统总览

仓库是单体多子系统结构（`apps/README.md`、`docs/architecture/module-map.md`）。根 `README.md`
“这是什么”一节列出 4 个子系统；实测 `apps/` 下还有 agent-worker、control-plane、eflow 知识库：

| 子系统 | 目录 | 技术栈 | 入口 / 端口（生产 / 隔离） |
| --- | --- | --- | --- |
| my_app（Data Insight MVP，主系统） | `apps/my_app/` | FastAPI + SQLAlchemy + React 19 + Vite | 后端 7038 / 7039，前端 7090 / 7091 |
| 统一门户 | `apps/portal/` | React 19 + Vite + Tailwind v4 + Express（TS）鉴权服务 | 9267 / 9277 |
| WhatsApp 知识库 bot | `apps/whatsapp-kb-bot/` | Node（Baileys）+ Python 分析脚本 | HTTP 服务 9269 / 9279 |
| WhatsApp 分析服务 | `apps/portal/scripts/whatsapp_analysis/` | Express + TS（tsx） | 9268 / 9278 |
| eFlow 知识库 | `apps/eflow_Q&A/` | Markdown + Python 工具 | 无服务（`tools/inspect_tt.py` 被后端预热调用） |
| agent-worker / agent-worker-linux | `apps/agent-worker/`、`apps/agent-worker-linux/` | Python 标准库（抽查 import 未见第三方依赖） | 任务执行器，非业务服务 |
| control-plane（调度/分派） | `apps/control-plane/backend/` | Python 标准库（抽查同上） | 调度器 + GitHub 分派模块 |
| 浏览器扩展 | `extensions/` | MV3 扩展（JS） | 2 个扩展，非服务端 |
| 反向代理 | `nginx-1.28.0/`（不入库） | nginx 1.28.0 | 80/443（生产），8080/8443（隔离） |

证据：`README.md:24`（“`nginx-1.28.0/` 作反向代理、`runtime/` 作捆绑运行时（Python 3.12 / Node 24 /
PostgreSQL 16）”）；`apps/README.md` 子系统表；`apps/my_app/backend/main.py:181`（`app = FastAPI(...)`，
`main.py:184` `version="1.0.0"`）；`apps/portal/server/index.ts`（Express 生产入口）。

### 1.2 运行时版本（固定真相源）

| 运行时 | 版本 | 证据 |
| --- | --- | --- |
| Python | 3.12.10（捆绑）；CI 用 3.12 | `README.md:49`、`runtime/README.md:9`；`.github/workflows/quality.yml` 多处 `python-version: "3.12"` |
| Node.js | v24.16.0（捆绑）；CI 用 24 | `README.md:50`、`runtime/README.md:10`；`quality.yml:65` `node-version: "24"` |
| PostgreSQL | 16（便携服务端二进制） | `README.md` 运行时表、`runtime/README.md`；`ops/deploy/start_all.ps1:49`（`runtime/postgresql16/bin`） |
| nginx | 1.28.0 | `README.md:24`；`ops/deploy/start_all.ps1:50`（`nginx-1.28.0/nginx.exe`） |
| 前端构建 | my_app 用 Vite 8 + TS ~6.0.2；门户用 Vite 6 + TS ~5.8.2 | `apps/my_app/frontend/package.json`、`apps/portal/package.json`（两套 Vite 大版本不同，见 §2.2） |
| 前端框架 | React 19 + React DOM 19（两处都是） | 同上两个 `package.json` |
| 后端框架 | FastAPI 0.139.0（锁定）+ Uvicorn + SQLAlchemy 2.0.51 + Alembic 1.19.0 + APScheduler 3.11.3 | `apps/my_app/requirements.lock.txt` |

补充说明：

- **无 Docker**：全仓无 `Dockerfile`/`docker-compose`（`find` 验证；仅知识库文档里提到 Docker 安全基线，
  与本平台部署无关）。无容器化现状，方案不引入 Docker。
- **无根 `package.json`**：根目录没有 `package.json`（`ls` 验证），`AGENTS.md` 也明确“本仓库没有统一的根
  `package.json`”。各前端项目独立安装、独立构建。
- **门户鉴权存储用 Node 内置 `node:sqlite`**：`apps/portal/server/authDb.ts:7`（“首选后端：SQLite（Node 内置模块
  node:sqlite，零 npm 依赖）”）、`authDb.ts:809`（“当前 Node 不支持内置 node:sqlite（需 >= 22.5），回退到 JSON
  文件存储”）。Node 24 满足要求，双平台一致；但 JSON 兜底的存在意味着低版本 Node 会静默换存储后端，
  打包时必须锁定 Node 版本，不得依赖宿主机 Node。
- **Python 配置文件不是依赖声明**：`apps/my_app/pyproject.toml` 只有 `[tool.ruff]` 与 `[tool.pytest.ini_options]`，
  无 `[project]` 依赖表。Python 依赖的唯一真相源是两个 requirements 文件（见 §2.1）。

### 1.3 外部系统（不上 ZIP、但运行时依赖）

以下系统只读/调用，不属于打包范围，首次启动检查时应明确区分“缺失即失败”与“缺失即降级”：

- FineBI 决策平台、eFlow（10.100.20.8:28001）、ZTE UME、VMAX 数仓 GBase 8a（**只读**，`pymysql`；
  `docs/architecture/data-source-registry.md` 明确“只能查询，不得修改或创建”）。
- Ollama（本地嵌入，`OLLAMA_URL` 默认 `http://127.0.0.1:11434`，模型 `bge-m3:latest`；
  `ops/deploy/env_template.txt`）、OpenWebUI / WorkBuddy 代理（`http://127.0.0.1:8788/v1`，LLM 问答）。
- Chrome（WhatsApp 发图截图，`CHROME_PATH` 默认指向 `D:\01_RPA\chrome-win\...`；
  `ops/deploy/env_template.txt` 明确“副本不带 Chrome（779MB）”）。
- Playwright 浏览器二进制（`requirements.txt` 有 `playwright>=1.40.0`，锁定 1.61.0；浏览器本体需另行下载，
  见 §2.4）。

---

## 2. 依赖清单

### 2.1 Python 依赖：声明、锁定、安装

| 项 | 现状 | 证据 |
| --- | --- | --- |
| 声明文件 | `apps/my_app/requirements.txt`（48 行，`>=` 范围为主） | 文件头注释：“统一依赖清单（由项目内多份 requirements 去重并集生成）” |
| 锁定文件 | `apps/my_app/requirements.lock.txt`（95 行，全部 `==` 精确锁定） | 文件头：“Pinned lockfile — generated 2026-08-03 from system Python 3.12” |
| 直接依赖（约 25 个） | fastapi、uvicorn[standard]、sqlalchemy、alembic、pandas、openpyxl、numpy、python-multipart、pydantic、apscheduler、requests、httpx、cryptography、xlsxwriter、matplotlib、playwright、geoip2、scipy、shapely、psycopg2-binary（`==2.9.12`，两处都钉死）、pymysql、paramiko、exchangelib、Pillow、python-dotenv，另有 pytest、ruff（开发/测试） | `requirements.txt` 全文 |
| 安装方式 | `pip install -r requirements.txt`（或 lock 文件） | `requirements.txt:2` 注释、`requirements.lock.txt:3` 注释 |
| 是否需联网 | **是**。pip 安装需访问 PyPI；仓库内无 wheelhouse、无 pip 缓存目录 | 全仓无 `*.whl` 随附目录（`find` 验证）；`runtime/python312/` 的 site-packages 是“官方安装包”现场装好的，不入库 |

已发现的不一致（打包时必须先收口，第 6 节方案已考虑）：

1. **CI 不用锁定文件**：`.github/workflows/quality.yml` 的 pytest 作业跑的是
   `pip install -r requirements.txt`（范围版），不是 `requirements.lock.txt`。同一份代码在 CI 与锁定安装下
   可能装出不同版本。ZIP 构建必须以 `requirements.lock.txt` 为准，并在 CI 构建作业里同样用 lock 文件。
2. **锁定文件有手工补丁史**：`requirements.lock.txt` 头注释记录 2026-09-27 两次补入（pytest/ruff；
   alembic/requests/playwright/geoip2/shapely/psycopg2-binary/pymysql/exchangelib 及传递依赖），
   原因是“此前 lock 只覆盖了一部分包，装出来的环境跑不起生产”。锁定文件当前是全的，但生成方式是手工维护、
   非 `pip freeze` 式可复现流程——Phase 1 构建时应在干净环境验证 `pip install -r requirements.lock.txt`
   后 `import` 全通过，并把验证步骤写进构建脚本（不得只信注释）。
3. **`requirements.txt` 里 `scipy`/`shapely` 无版本下界**（裸包名），lock 里分别是 `scipy==1.18.0`、
   `shapely==2.1.2`。范围安装存在漂移风险，再次印证“构建用 lock”。
4. **agent-worker / control-plane 无依赖清单**：`find` 确认三处（`apps/agent-worker/`、
   `apps/agent-worker-linux/`、`apps/control-plane/`）均无 `requirements*.txt`/`pyproject.toml`；
   抽查其 `import` 均为标准库（`argparse/json/urllib/subprocess/signal/...`），CI 也只为它们装 `pytest`
   （`quality.yml` 的 `agent-tests` 作业）。结论：这三处按“标准库 only”打包，不预装第三方包；
   但未做全量 import 审计，列入未核实项。

### 2.2 Node 依赖：4 个独立项目，4 份锁定

| 项目 | `package.json` | lockfile | 关键依赖 | 安装命令（现状） |
| --- | --- | --- | --- | --- |
| my_app 前端 | `apps/my_app/frontend/package.json` | `package-lock.json`（v3，202 KB） | React 19.2.7、Vite ^8.1.1、TS ~6.0.2、antd 6、echarts 6、leaflet、exceljs、`xlsx`（CDN tarball） | `npm ci` |
| 门户 | `apps/portal/package.json` | `package-lock.json`（v3，135 KB） | React 19、Vite ^6.2.3、TS ~5.8.2、Express 4、tsx、Tailwind v4、nodemailer | `npm ci` |
| WhatsApp bot | `apps/whatsapp-kb-bot/package.json` | `package-lock.json`（v3，55 KB） | `baileys`（钉死 `6.7.24`）、qrcode-terminal；`engines: node >=20` | `npm ci` |
| WhatsApp 分析服务 | `apps/portal/scripts/whatsapp_analysis/package.json` | `package-lock.json`（v3） | Express 4、node-cron、tsx；`engines: node >=23.4.0` | 未见于 README（缺口，见下） |

要点：

- **4 份 lockfile 均为 `lockfileVersion: 3`**，`npm ci` 可复现安装。`node_modules/` 被根 `.gitignore:147` 排除，
  不入库；但启动预检要求它存在（`ops/deploy/start_all.ps1:309` 查 `frontend/node_modules/vite/bin/vite.js`，
  `:313` 查 `portal/node_modules/tsx/dist/cli.mjs`）。**结论：`node_modules` 必须由构建产物提供，不能期望用户现场 `npm ci`**（现场安装既需联网，又与“首次启动零下载”冲突）。
- **`xlsx` 不走 npm registry**：`apps/my_app/frontend/package.json:52` 为
  `"xlsx": "https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz"`；`:6` 的注释说明 registry 版停留在 0.18.5
  且有已知 CVE，SheetJS 新版只从自家 CDN 分发，升级前须人工核验 tarball 完整性并同步
  `apps/my_app/docs/THIRD-PARTY-NOTICES.md`。构建机必须能访问该 CDN，或在构建缓存中预置该 tarball；
  产物校验时应核对该包的完整性哈希（lockfile 内含 `integrity` 字段，`npm ci` 会校验）。
- **README 的安装清单漏了第 4 个项目**：`README.md`“装依赖”一节只列了 my_app 前端、portal、bot 三处 `npm ci`，
  未列 `whatsapp_analysis`。该服务默认不开（`START_WA_ANALYSIS=0`），但 ZIP 若宣称“全功能”，构建清单必须补上它；
  若不含它，须在交付说明中明确标注。
- **Vite 大版本分裂**：my_app 用 Vite 8（lock 中 `vite@8.1.4`），门户用 Vite 6。两者不可合并安装、不可共用
  `node_modules`，打包时按项目分别处理，不得“统一升级”。
- **门户 `clean` 脚本是 Unix 命令**：`apps/portal/package.json` 的 `"clean": "rm -rf dist server.js"` 在 Windows
  `cmd` 下不可用（需 Git Bash/PowerShell 替代）。打包脚本不得调用它；清理产物应由构建脚本用跨平台方式做。

### 2.3 运行时发行包（不入库、构建期获取）

| 发行包 | 来源（现状） | 证据 |
| --- | --- | --- |
| Python 3.12.10 + site-packages | 官方安装包，主仓库打包脚本 `-Stage runtimes` | `README.md:49`、`runtime/README.md:9` |
| Node v24.16.0 | 官方安装包，同上 | `README.md:50`、`runtime/README.md:10` |
| PostgreSQL 16 服务端 | 官方安装包，同上 | `runtime/README.md` |
| nginx 1.28.0（含地图瓦片 `liberia.pmtiles` 29.4 MB） | 随副本目录，不入库 | `nginx.conf.template` 头注释（`/tiles/`）、`README.md`“不入库的内容”表（37 MB） |

现状打包脚本 `scripts/build_olbr_portable.ps1` **不在本仓库**（属主仓库 `D:\01_RPA`），本仓库只有它铺出来的
产物脚本（`start_all.ps1` 等）与 `docs/workspace/部署说明.md` 对其阶段的描述（`quarantine` / `source` / `prune` /
`nodemodules` / `runtimes` / `nginx` / `config`）。Phase 1 的 Actions 构建是新实现，不得假设该脚本可用；
但阶段划分与“源码/运行时/数据分离”的思想应继承（见 §6.3）。

### 2.4 联网需求汇总

| 动作 | 是否需联网 | 说明 |
| --- | --- | --- |
| `pip install -r requirements*.txt` | 是（PyPI） | 构建期做，产物带 venv/wheels |
| `npm ci`（4 个项目） | 是（npm registry + SheetJS CDN） | 构建期做，产物带 `node_modules` |
| `playwright install`（浏览器二进制） | 是（Playwright CDN） | 现状无任何脚本/文档提及该步骤的执行位置；截图链路（site_outage/whatsapp/kpi_email_report）依赖它。列入未核实项：生产机浏览器二进制从何而来 |
| 运行时发行包下载（Python/Node/PG/nginx） | 是（各官方渠道） | 构建期做，打进运行时 ZIP；或复用 Actions runner 自带工具链再抽取——两种路线见 §6.4 |
| 首次启动（目标机） | **必须否** | 任务约束。`start_all.ps1` 现状已是离线启动（仅 initdb/建库/拉进程），ZIP 方案必须保持并验证这一点 |

---

## 3. 当前启动方式

### 3.1 端口总表（生产 vs 隔离副本）

真相源：`ops/deploy/env_template.txt`（隔离默认值）与 `README.md` 第五节对照表；`apps/portal/src/config.ts`
是“主机名与端口的单一真相源”（`AGENTS.md` 环境拓扑一节）。

| 服务 | 生产端口 | 隔离端口（02） | 绑定 | 备注 |
| --- | --- | --- | --- | --- |
| nginx HTTP / HTTPS | 80 / 443 | 8080 / 8443 | 0.0.0.0 | 自签证书，浏览器告警属预期 |
| my_app 后端 FastAPI | 7038 | 7039 | 127.0.0.1 | |
| my_app 前端 vite preview | 7090 | 7091 | 127.0.0.1 | 服务 `apps/_fe_dist` 构建产物 |
| 统一门户 Express | 9267 | 9277 | 127.0.0.1 | `server/index.ts:39,41`（`PORT`/`PORTAL_HOST`，默认 9267/127.0.0.1） |
| WhatsApp 分析服务 | 9268 | 9278 | 127.0.0.1 | 默认不开 |
| 问答助手 HTTP 服务 | 9269 | 9279 | 127.0.0.1 | 后端按需拉起（见 §3.4） |
| PostgreSQL | 5432 | 55432 | 127.0.0.1 | 便携实例 |
| 实时代理 FMS / VMAX | 8787 / 8790 | 8887 / 8890 | — | 后端启动线程自动拉起（`main.py:745,751`） |

`start_all.ps1:133-136` 有隔离熔断：副本端口落在生产端口集合
`@(80, 443, 7038, 7090, 9267, 9268, 9269, 5432, 8787, 8790)` 或端口重复时直接拒绝启动。
ZIP 的默认配置必须沿用“隔离端口 + 熔断”语义，转正生产时由部署方显式改端口并删除 `ISOLATED_COPY.flag`
（`README.md` 第五节）。

### 3.2 开发启动（各子系统独立，无统一编排）

| 子系统 | 开发命令 | 证据 |
| --- | --- | --- |
| my_app 前端 | `cd apps/my_app/frontend && npm run dev`（`vite`，7090，`/api` 代理到后端） | `package.json` scripts；`vite.config.ts:39,43`（`base: '/myapp/'`，`port: 7090`） |
| my_app 后端 | `python -m uvicorn backend.main:app --host 127.0.0.1 --port <端口>`；或直接跑 `main.py`（`__main__` 用 `0.0.0.0:8000` + reload，与生产端口不一致，见下） | `start_all.ps1:401-403` 的生产命令形态；`main.py:808-810` |
| 门户 | `cd apps/portal && npm run dev`（`vite --port=9267 --host=0.0.0.0`，`predev` 先拉 WhatsApp 分析服务） | `apps/portal/package.json` scripts |
| WhatsApp bot | `npm start`（`node --env-file-if-exists=.env src/index.js`） | `apps/whatsapp-kb-bot/package.json` |
| WhatsApp 分析服务 | `npm run dev`（`tsx watch server/index.ts`） | `apps/portal/scripts/whatsapp_analysis/package.json` |

注意：`main.py:808-810` 的 `__main__` 分支用 `port=8000, reload=True`，与任何已文档化的端口（7038/7039）
都不一致，且 `vite.config.ts` 的代理默认指向 `MY_APP_BACKEND_PORT || '7038'`。开发文档不得以 `python main.py`
为准；ZIP 不提供该路径。

### 3.3 生产启动（Windows，D:\01 实例）

生产启动链是“每子系统一套 `.bat` + `.ps1` + watchdog”，无统一入口（根 `start.bat` 是隔离副本的，
不是生产的）：

- **my_app**：`apps/my_app/start.bat` → `start_hidden.ps1`，拉起三部分（`start_hidden.ps1:246` 后端 watchdog、
  `:331` worker watchdog、前端 vite preview）：`backend_watchdog.py` 托管 uvicorn（7038，队列模式），
  `worker_watchdog.py` 托管 10 组 worker（`start_hidden.ps1:217`：`MY_APP_WORKER_GROUPS='u,n,s,w,m,c,v,x,g,p'`；
  `apps/my_app/start.bat` 末尾按 10 组 pid 文件逐个验活），前端 `vite preview`（7090）。
  另有 `start_ensure.bat` / 计划任务保活、`stop.bat` 树杀。
- **门户**：`apps/portal/start.bat` → `start_hidden.ps1`：先试 `npm run start`（`tsx server/index.ts`，9267），
  失败则直调 `node node_modules/tsx/dist/cli.mjs server/index.ts`（`start_hidden.ps1:201,213`）；
  再起 WhatsApp 分析服务（9268）。`apps/portal/README.md` 与 `docs/` 有商用交付工单背景。
- **nginx**：本机 `nginx-1.28.0/nginx.exe`，配置由 `write_nginx_conf.ps1` 从 `nginx.conf.template` 展开
  （`{{ROOT}}`/`{{HTTP_PORT}}` 等占位符，见 §3.5）。
- **WhatsApp bot**：`apps/whatsapp-kb-bot/autostart.ps1` / `register_whatsapp_tasks.ps1`（计划任务）+
  `bot_watchdog_hidden.vbs` 保活；主进程连真实 WhatsApp 会话，隔离副本默认不起（`START_WA_BOT=0`）。

`docs/adr/0002-copy-isolation.md` 与 `apps/README.md` 有硬性禁令：02 隔离副本**禁止**运行
`apps/my_app/start_hidden.ps1` 与同目录 `start.bat`（生产脚本）。ZIP 交付的启动器必须分清“生产形态”与
“隔离形态”，不得混用。

### 3.4 隔离副本启动（Windows，D:\02，ZIP 方案的继承基线）

根 `start.bat` 是薄壳，只调 `ops/deploy/start_all.ps1`（PowerShell 5.1 兼容，文件头注明不用 `::new()`、
三元、`-Parallel`、`$IsWindows`）。`start_all.ps1` 六步（行号为各步骤标题）：

| 步 | 内容 | 关键证据 |
| --- | --- | --- |
| 预检 | 要求 `ISOLATED_COPY.flag` 存在；校验 Python/Node 解释器、`backend/main.py`、vite、pg_ctl、门户 tsx 齐全 | `start_all.ps1:47-51`（解释器路径由 `$Root` 推出）、`:302-322`（预检） |
| 1/6 便携 PG | 无 `PG_VERSION` 则 `initdb -U myapp -A trust --encoding=UTF8`；`pg_ctl start`；轮询 `SELECT 1` 至可连接；校验 `SHOW data_directory` 等于副本 `pgdata`；无库则 `createdb` | `start_all.ps1:324,331-335,360,367,374` |
| 2/6 nginx 配置 | 调 `write_nginx_conf.ps1` 展开模板并 `nginx -t` 校验 | `start_all.ps1:387-396` |
| 3/6 my_app 后端 | `runtime/python312/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 7039`，工作目录 `apps/my_app` | `start_all.ps1:400-403` |
| 4/6 my_app 前端 | `runtime/node/node.exe node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port 7091 --strictPort` | `start_all.ps1:407-411` |
| 5/6 门户 + 可选服务 | `node node_modules/tsx/dist/cli.mjs server/index.ts`（9277）；`START_WA_ANALYSIS=1` 时起分析服务（9278）；`START_WA_BOT=1` 时起 bot 主进程（默认 0） | `start_all.ps1:415-444` |
| 6/6 nginx | `nginx.exe -p <NginxDir> -c conf/nginx.conf`，读 `logs/nginx.pid` 登记 | `start_all.ps1:449-470` |

编排机制（ZIP 的 Linux 启动器需要等价实现，见 §5/§6）：

- **配置真相源**：副本根 `.env`（模板 `ops/deploy/env_template.txt`），由 `start_all.ps1` 逐行读入且
  **优先于继承的进程/系统环境变量**（防生产污染）；缺 `.env` 时首次启动从模板复制。
  派生变量（`PGDATA`、`FE_DIST_OUT`、`PORTAL_AUTH_DATA_DIR`、`PORTAL_QA_NODE` 等）无条件按副本根覆盖
  （`start_all.ps1:140-151`）。
- **进程托管**：`Start-Detached`（`start_all.ps1:268`）用 `Start-Process` 拉起并写
  `TMP/portable-<name>.pid`（内容为 `pid|进程名|启动时间ticks`）；`stop_all.ps1` 的 `Get-TrustedPid`
  做三重校验后才杀，绝不按端口盲杀、绝不 tree kill（防误杀同机生产实例）。
- **强制安全边界**：`MY_APP_DISABLE_SCHEDULER=1` 强制设置（`main.py:168-170,695` 跳过 APScheduler）、
  不设 `MY_APP_TASK_QUEUE`、不起 watchdog/worker 组/计划任务（`start_all.ps1` 文件头注释）。
- **问答助手按需拉起**：不在 `start_all.ps1` 内；首次提问时后端 `portal_qa_ctl.py` 以 DETACHED 方式拉起
  `node --env-file-if-exists=.env src/http-server.js`（`portal_qa_ctl.py:166,171`），node 路径取
  `PORTAL_QA_NODE` 或 PATH（`portal_qa_ctl.py:104-109`，Windows 取 `node.exe`、其余取 `node`）。
  bot 目录由 `portal_qa_ctl.py:39` 按“my_app 的父目录 / `whatsapp-kb-bot`”推导，**无环境变量可覆盖**——
  这是 §6 布局设计的硬约束之一（`my_app` 与 `whatsapp-kb-bot` 必须同级，`docs/architecture/module-map.md` §1）。

### 3.5 nginx 配置生成

- 模板 `nginx.conf.template`（306 行）：占位符 `{{ROOT}} {{HTTP_PORT}} {{HTTPS_PORT}} {{BE_PORT}} {{FE_PORT}}
  {{PORTAL_PORT}}`；`{{ROOT}}` 为正斜杠绝对路径（Windows 上 nginx conf 里反斜杠是转义字符）。
  路由保留：`/`（导航页）、`/uniplam/`（门户）、`/myapp/`（前端，不剥前缀）、`/api/`（后端）、
  `/MsPortal/`、`/v3/olbrportal/`、`/v3/<team>_portal/`、`/tiles/`（离线瓦片）。
  模板头注释列明已删的路由（FineBI、eFlow 反代、ollama 等），副本不带这些上游。
- 生成器 `ops/deploy/write_nginx_conf.ps1`：按 `$PSScriptRoot` 推导根、读 `.env` 解析端口、展开模板、
  输出必须是 **UTF-8 无 BOM**（PS 5.1 的 `Set-Content -Encoding UTF8` 会加 BOM，脚本改用 .NET
  `UTF8Encoding($false)`；nginx 会把 BOM 当成 directive 而 `[emerg]`）。
- 证书：`nginx-1.28.0/certs/portal.crt` + `portal.key`（自签私钥）。`docs/workspace/部署说明.md` 第一节警告：
  “随副本分发等于交出这把钥匙……交给别人之前，请在目标机重新生成一对自签证书并替换”。
  **ZIP 不得内置该私钥**；首次启动脚本应在证书缺失时生成新自签证书（或明确要求部署方提供）。

### 3.6 服务编排现状小结

| 维度 | 现状 |
| --- | --- |
| 编排器 | Windows 上是 PowerShell 脚本（`start_all.ps1` / 各 `start_hidden.ps1`）；Linux 上**没有**应用编排器 |
| 保活 | Windows 计划任务（`install-smoke-watcher.ps1` 的 `OLBR_SmokeWatcher`、`install_ensure_task.bat`、门户 `ensure_portal.ps1`）+ watchdog 进程 + VBS 隐藏启动器；Linux 上只有 agent-worker 的 systemd 模板（见 §5.5） |
| 健康检查 | 端口监听 + HTTP 探测（`Wait-Port` / `Confirm-Http`）；后端 `/api/health` 等路由存在（`routers/system.py`）；无统一健康检查端点汇总 |
| 日志 | `TMP/portable-*.out/.err`（隔离副本）、各子系统 `log/` 目录（生产）、`nginx-1.28.0/logs/`；日志目录均在版本/副本目录**内**——ZIP 方案须外移（见 §6.5） |
| 停止 | 隔离副本 `stop_all.ps1`（pid 文件 + 三重校验）；生产各 `stop.bat`（树杀，禁用于隔离副本） |

---

## 4. 数据库结构和迁移方式

### 4.1 双模架构（同一份代码，两种后端）

`apps/my_app/backend/database.py` 是数据库真相源：

- **模式开关**：`database.py:27-29`：`DB_MODE = os.environ.get("MY_APP_DB_MODE", "sqlite")`，只接受
  `sqlite|pg`，否则启动即 `RuntimeError`。默认 `sqlite`；生产事实是 `pg`
  （`docs/architecture/data-source-registry.md`：“平台数据库模式 `MY_APP_DB_MODE=pg`；健康检查返回
  `db_mode=pg`”；“SQLite 的角色是熔断回退，不是当前主库”）。回退只需改环境变量，无需改代码。
- **SQLite 路径**：`database.py:31`：`DB_PATH = Path(__file__).parent.parent / "db" / "app.db"`，
  即 `apps/my_app/db/app.db`。**该路径无环境变量覆盖入口**（Alembic 的 `MY_APP_DB_OVERRIDE` 只影响迁移，
  不影响运行时），是“数据独立于版本目录”需要后续代码改动的点之一（见 §6.5 与未核实项）。
- **PG 连接**：`database.py:34,37`：`PG_DATABASE_URL` 取 `DATABASE_URL` 环境变量（`backend/.env` 由
  `load_dotenv` 载入，`database.py:10-19`，`backend/.env` 优先、缺失回退项目根 `.env`，`override=False`
  保证 OS 环境变量优先）；`pg` 模式下缺连接串则启动即报错（`database.py:49-51`），不静默降级。
  引擎池：`pool_size=20`、`max_overflow=10`（`PG_POOL_SIZE`/`PG_MAX_OVERFLOW` 可覆盖，`database.py:54-55`）。
- **双模原始连接**：`get_raw_connection()`（`database.py`）在 sqlite 模式返回 sqlite3 连接
  （`busy_timeout=30000` + `synchronous=NORMAL`，不用 WAL——注释说明 WAL 在长运行服务里偶发
  "closed database"）；在 pg 模式按路径推断 schema 返回 psycopg2 包装连接，并把 SQLite 的 `?` 占位符
  转成 psycopg2 的 `%s`（`_qmark_to_pyformat`），使既有 `?` 写法零改动运行。
- **永不走 PG 的本地连接**：`get_sqlite_connection()` / `get_sqlite_readonly_connection()` 只开本地文件，
  用于本地权威表与本地专属数据文件（见 §4.4）。

### 4.2 PostgreSQL 结构：schema 映射

PG 侧没有“一张大表清单”式的单一真相源；结构由“SQLite 文件 → schema”映射规则 + Alembic 静态模型 +
动态建表三部分组成：

**映射规则**（`database.py::_resolve_pg_schema`）：取 SQLite 文件名 stem 映射为 schema 名；
`data/ran_ume/` 目录下的读库映射为 `ran_ume_ro`（读写分离）。映射表：`app→app`、`eflow→eflow`、
`lte_pl→lte_pl`、`battery_kpi→battery`、`lbr_ms_sla_kpi→ms_sla`、`fw_db→fw`、`ipran_ume_kpi→ipran_ume`、
`ran_ume→ran_ume`、`ran_kpi_daily→ran_ume`；不在表内时直接用 stem。

**search_path**（`database.py:386,392`）：

```
app,eflow,lte_pl,battery,ms_sla,fw,ipran_ume,ran_ume,ran_ume_ro,public
```

**静态模型**（`apps/my_app/backend/models/__init__.py` 注册到 `Base.metadata`，供 Alembic 与 `create_all`）：

| 模型 | 表 | 说明 |
| --- | --- | --- |
| `AnalysisArchive`、`UploadedFile` | `analysis_archive`、`uploaded_files` | `models/analysis.py` |
| `TaskScheduleConfig` | `task_schedule_config` | 本地权威表之一（见 §4.4） |
| `TaskExecutionLog` | `task_execution_log` | |
| `TaskQueue` | 任务队列（跨进程队列，Phase 3） | `models/task_queue.py` |
| `Credential` | `credentials` | 本地权威表之一 |
| `SpSyncLog` | `sp_sync_log` | |

**独立库模型**（各自 Base，不参与主库 Alembic）：`models/packet_loss.py`（`lte_pl.sqlite3`）、
`models/battery.py`（`battery_kpi.sqlite3`）。**动态表**（automap 反射，无静态模型）：
`models/eflow.py`（`eflow_*` / `realtime_alarm` / `history_alarm`）、`models/site.py`
（`master_site_db` / `topology`）。

**动态表不在迁移管理内**：`alembic/versions/0001_baseline.py` 头注释明确“动态表（eflow_* /
realtime_alarm / history_alarm / master_site_db / topology / SP 备件数据表）由同步服务按 API 字段动态创建，
不在 migration 管理范围内”；`alembic/env.py` 的反向工程模式也排除 `eflow_*` / `_temp_*` / `sqlite_*` /
`alembic_version`。**结论：Alembic 只能保证结构表的 schema，业务数据表的结构由同步任务在运行时建立；
空库首次启动后，数据表是逐步长出来的，不是迁移一次建全的。** ZIP 的首次启动验证必须按这个口径设计
（验结构表 + 验服务可用，不验“所有业务表存在”）。

### 4.3 迁移工具与流程（Alembic）

| 项 | 现状 | 证据 |
| --- | --- | --- |
| 工具 | Alembic 1.19.0（锁定），配置 `apps/my_app/alembic.ini`（`script_location = alembic`，`sqlalchemy.url` 仅为占位符，真实 URL 由 `env.py` 计算） | `alembic.ini`、`requirements.lock.txt` |
| URL 解析 | `alembic/env.py::_resolve_url`：`pg` 模式优先 `MY_APP_DB_OVERRIDE`，其次 `DATABASE_URL`；`sqlite` 模式用 `DB_PATH` 绝对路径，`MY_APP_DB_OVERRIDE` 可指向测试库；`render_as_batch` 仅 sqlite 启用 | `alembic/env.py` 全文 |
| 版本数 | 7 个版本文件 | `apps/my_app/alembic/versions/`：`0001_baseline`、`0002_task_queue`、`0003_alarm_epoch_columns_bigint`、`0004_reflected_real_to_double`、`0005_tt_exclude_table`、`0006_manual_stop_flag`、`5415704359dc_phase4_coverage`（51 KB，Phase 4 覆盖） |
| 0001 基线 | 6 张结构表（见 §4.2）；“已存在的生产库执行 `alembic stamp head` 标记即可，不重复建表；空库执行 `alembic upgrade head` 可建出与生产一致的结构” | `0001_baseline.py` 头注释 |
| 常用命令 | `alembic revision --autogenerate -m "<模块>-<变更>"` → `alembic upgrade head`；回退 `alembic downgrade base`；反向工程模式 `MY_APP_AUTOGEN_REFLECT=<存量 sqlite>` | `AGENTS.md`“变更速查”；`alembic/env.py` 头注释 |
| PG schema 归属 | 迁移不加 schema 前缀，靠 search_path 落到 `app` | `0005_tt_exclude_table.py:33` 注释：“统一不加 schema 前缀 —— 与既有迁移保持一致（PG 侧由 search_path 落到 app）” |
| schema 创建 | 由数据搬运脚本建：`CREATE SCHEMA IF NOT EXISTS` | `backend/migrations/transfer_lib.py:544` |

**未核实的关键点**：新鲜空 PG 库（仅 `createdb`，无任何 schema）上直接跑 `alembic upgrade head` 时，
`app` schema 由谁创建？迁移本身不建 schema（`0005` 注释依赖 search_path 的首位存在）；`transfer_lib.py`
的建 schema 逻辑属于“SQLite → PG 搬运”流程，不属于 Alembic 流程。若首位 schema 不存在，PostgreSQL
建表行为取决于 search_path 解析细节，**本次只读审查未实测，不得假设**。Phase 1 必须在干净 PG 上实测
“initdb → createdb → alembic upgrade head”全链路，并把建 schema 步骤（如需要）写进首次启动脚本。
已列入未核实项第 1 条。

**其他迁移/搬运脚本**（`apps/my_app/backend/migrations/`，共 19 个文件）：`transfer_app.py`（跳过
`alembic_version`，搬 37 张业务表）、`transfer_lane_a.py` / `transfer_lane_b.py`（后者建 `ran_ume_ro`
schema 再搬 4 表）、`transfer_lib.py`、`reconcile.py`、`sync_pg_to_sqlite.py`（PG → SQLite 全量同步，
见 §4.4）、`fix_*_text.py` / `fix_tt_alert_log_columns.py`（字段类型修复）、`smoke_test_pg.py`、
`check_pg_alert_log.py`（PG 口令改读 `PG_ALERT_LOG_PASSWORD` 环境变量，缺失则退出码 2，见 `README.md`
第六节）。这些脚本是“数据搬运/修复”层，不是 Alembic 替代品；ZIP 不默认跑它们，只在数据迁移场景
按文档显式调用。

### 4.4 本地权威三表与双库同步

| 表 | 内容 | 写入方 | 证据 |
| --- | --- | --- | --- |
| `credentials` | 设置页账号/口令（20 组） | `backend/services/credential/credential_store.py` | `docs/workspace/部署说明.md` 第八节 |
| `task_schedule_config` | 任务启停 / cron（74 行） | `backend/services/task/task_runner.py` | 同上；`task_runner.py` 注释“权威只有本地 `db/app.db` 这一份” |
| `email_recipients` | 邮件收件人名单 | `backend/services/email/email_recipient_store.py` | 同上 |

规则（`docs/architecture/data-source-registry.md` + `AGENTS.md`）：

- 三张表由应用**固定写 SQLite**（`get_sqlite_connection`，不走 PG）；PG 里那份只是镜像。
- `backend/scripts/sync_pg_to_sqlite.py` 把它们列进 `SKIP_TABLES`，避免“从 PG 重建 app.db”时 DROP 本地数据；
  同步器按策略不搬 `db/` 目录。因此“从 PG 重建 app.db”的部署路径**天生带不出它们，且不会报错**——
  2026-11-11 实测事故：漏搬后 16 个任务报“拒绝以空密码登录”、4 个手动停用任务变回启用。
- 迁移工具：`ops/deploy/local_authority_tools.py check/migrate --source <旧app.db> --target <新app.db>`
 （先核对、再事务内替换、逐行哈希校验；备份 `TMP\_bak_local_authority_*.json` 含明文口令，只留本机）。
- 三张表含生产凭据：只放本机数据库，不 `git add`，**绝不进 ZIP**（见 §6.5 排除清单）。

双库现状（`data-source-registry.md`）：`eflow_tt` 双库（同步写 `app.db`）；告警表双写、页面读 `app.db`；
其余表/文件库“未逐个盘清”，标“待确认”。**ZIP 方案不得宣称“已盘清全量数据流”**；Phase 1 只保证结构迁移 +
本地权威表迁移工具可用，逐表盘点是第二轮工作（该文档“目标”一节明确未完成）。

### 4.5 数据目录位置总表

| 数据 | 位置（默认） | 可覆盖方式 | 是否进 ZIP |
| --- | --- | --- | --- |
| 便携 PG 数据目录 | `runtime/pgdata/`（隔离）；另有 `runtime/pgdata.off-20261112/`（02 冒烟用已有数据目录） | `PGDATA` 环境变量（`start_all.ps1` 填成副本根下的 `runtime/pgdata`） | **否**（任务约束；`runtime/README.md`：“不要把 `pgdata` 当备份：PG 数据文件跨版本/跨机器不保证可移植”） |
| 主 SQLite 库 | `apps/my_app/db/app.db`（2.5 GB 级，含主站点库 xlsx 引用） | 无运行时覆盖入口（见 §4.1） | 否（`.gitignore` 排除；`README.md`“不入库的内容”表） |
| 派生 SQLite | `apps/my_app/data/`（2.7 GB）及各任务专用库文件 | 各任务自定，多数相对路径 | 否 |
| 门户鉴权库 | 默认 `apps/portal/auth-data/`（`portal-auth.db`，WAL）；隔离副本由 `start_all.ps1` 改到 `runtime/portal-auth-data/` | `PORTAL_AUTH_DATA_DIR` | 否（含加盐密码哈希） |
| 门户默认目录 | `apps/portal/portal-default.json` | `PORTAL_DEFAULT_FILE` | 否（运行时数据） |
| Bot 向量索引/问答记录/登录凭据 | `apps/whatsapp-kb-bot/data/`（103 MB）：`index.json`、`lookups.json`、`qa.sqlite`、`data/auth/`（Baileys） | `INDEX_PATH`、`AUTH_DIR` 等（`src/config.js:286-297`） | 否（`data/auth` 是设备凭据；索引可重建，`npm run ingest`） |
| WhatsApp 会话 | `apps/whatsapp_session/`（40 MB） | `WHATSAPP_SESSION` | 否 |
| 上传文件 | `uploaded_files` 表 + 文件落盘（`UploadedFile` 模型）；具体落盘根未在本次审查中逐一核实 | — | 否（运行时数据，须外移；见未核实项） |
| 日志/中间态 | `TMP/`（182 MB，PG 快照/切换备份）、各 `log/` 目录、`nginx-1.28.0/logs/` | — | 否 |
| 地图瓦片 | `nginx-1.28.0/html/tiles/liberia.pmtiles`（29.4 MB） | — | **是**（静态资源，随运行时/应用包提供；缺失时地图白屏，`部署说明.md` 第七节） |

PG 快照灌入流程（数据迁移场景，不属于常规首次启动）：`ops/deploy/bundle_data.ps1` 分 `dump`（从生产
`backend/.env` 的 `DATABASE_URL` 读连接信息，`pg_dump -Fc` 落盘，口令只经 `PGPASSWORD` 进程环境变量传递、
用完清除）与 `restore`（副本 initdb → `pg_ctl start` → `createdb` → `pg_restore`）两阶段；要求副本带
`ISOLATED_COPY.flag` 且源/副本路径不重叠。该脚本属已停用的同步链备查文件（`ops/README.md`），但灌库逻辑
本身是数据迁移的参考实现；ZIP 方案中数据快照作为**独立交付物**（另见 §6.3），不与程序 ZIP 混装。

---

## 5. Windows/Linux 兼容性问题

审查方法：枚举全仓脚本类型、grep 平台相关调用（`win32`/`ctypes`/`taskkill`/`.exe`/硬编码盘符）、
实测检出行尾与可执行位。以下每项均有实证；“影响”列写的是对 ZIP 双平台交付的具体后果。

### 5.1 脚本生态：Windows 专属占绝对多数

实测：全仓 `.bat/.ps1/.vbs/.cmd` 共 62 个，仅 1 个 `.sh`（`apps/agent-worker-linux/install.sh`）。
生产与隔离启动链（§3.3/§3.4）全部是 `.bat` + PowerShell 5.1 脚本 + VBS 隐藏启动器：

| Windows 专属机制 | 实证 | Linux 影响 |
| --- | --- | --- |
| `.bat` 薄壳 + PowerShell 编排 | 根 `start.bat`/`stop.bat`、`ops/deploy/start_all.ps1`（498 行，PS 5.1 兼容注释）、`ops/shortcuts/*.bat` | Linux 需新写等价的 `.sh` 编排器（或跨平台 Node/Python 编排器）；不得假设目标机有 PowerShell（`pwsh` 未在任何文档中列为依赖） |
| VBS 隐藏启动器 | `apps/my_app/start_hidden.vbs`、`start_ensure_hidden.vbs`、`apps/portal/start_ensure_hidden.vbs`、`apps/whatsapp-kb-bot/*_hidden.vbs`、`scripts/tt-bridge-hidden.vbs` | VBS 仅 Windows。Linux 上“后台 + 脱离终端”改用 `nohup`/`setsid`/systemd |
| Windows 计划任务保活 | `ops/deploy/install-smoke-watcher.ps1`（注册 `OLBR_SmokeWatcher`）、`apps/whatsapp-kb-bot/register_whatsapp_tasks.ps1`、`apps/portal/install_ensure_task.bat`、`install_nginx_log_rotate_task.bat` | Linux 需 systemd unit + timer（仓库内无应用侧模板，见 §5.5） |
| `netstat`/`taskkill` 进程管理 | `apps/my_app/start.bat` 用 `netstat -ano \| findstr ":7038"` 验活；`backend_watchdog.py:452` 用 `taskkill /PID <pid> /T /F` 清孤儿；`stop.bat` 树杀 | Linux 改用 `ss`/`lsof` + 信号（`SIGTERM`/`SIGKILL` 进程组）；`agent-worker-linux` 已有 POSIX 进程组实践可参考，但那是任务执行器，不是应用服务 |

### 5.2 行尾与编码：字节级要求，检出形态不可信

`.gitattributes` 的规则（仓库级硬约束，文件头有长篇注释说明原因）：

- `*.bat` / `*.cmd`：`-text eol=crlf`——注释：“cmd.exe 解析纯 LF 的批处理会**静默吞掉部分行的首字符**
  （实测症状：`"REM"` -> `"EM"`）……一旦某行恰好是控制语句，行为就会真的跑偏。”
- `*.ps1`：`text eol=crlf`；`.githooks/*`：`text eol=lf`（`#!/bin/sh\r` 会报 "bad interpreter"）。
- 冒烟脚本：`ops/deploy/smoke-local.ps1`、`ops/smoke/*.ps1|*.cmd`：`-text eol=crlf`，保留 UTF-8 BOM + CRLF
  入库字节（PS 5.1 无 BOM 会解析失败）。
- nginx 配置：必须是 UTF-8 **无 BOM**（`write_nginx_conf.ps1` 头注释；BOM 会导致 nginx `[emerg] unknown directive`）。

**实测异常**：本次在 Linux 浅克隆中，`git ls-files --eol` 显示根 `start.bat`/`stop.bat` 为
`i/lf w/lf attr/-text`——入库 blob 与检出工作区**均为 LF**，与 `eol=crlf` 的期望不一致；
而 `ops/deploy/start_all.ps1` 为 `i/lf w/crlf`（符合期望）。按 `.gitattributes` 自身注释，
纯 LF 的 `.bat` 在 cmd.exe 下有静默损坏风险。可能原因（未在 Windows 上验证）：`-text` 与 `eol=crlf`
组合在不同 Git 版本/平台下的行为差异，或该文件入库时即为 LF 且属性未生效。

**对 ZIP 的要求**（无论根因为何，打包步骤必须显式处理，不得依赖检出形态）：

1. 打包脚本在组装阶段显式规范化：Windows 产物中的 `.bat`/`.cmd` → CRLF、ASCII（根 `start.bat` 注释要求
   “Keep this file ASCII-only and CRLF; non-ASCII breaks cmd parsing”）；`.ps1` → CRLF + UTF-8 BOM（与
   `start_all.ps1` 入库形态一致，实测为 BOM+CRLF）；Linux 产物中的 `.sh` → LF + 可执行位。
2. nginx 配置生成结果 → UTF-8 无 BOM（Linux 生成器同样遵守）。
3. 在 Windows runner 上对 ZIP 解压产物做“行尾断言”测试（`file` / 字节抽查），作为构建门禁之一。

### 5.3 路径：硬编码、盘符、分隔符、解释器探测

| 问题 | 实证 | 影响与对策 |
| --- | --- | --- |
| Python 解释器探测只认 `.exe` | `apps/whatsapp-kb-bot/src/pythonPath.js::CANDIDATES`：`runtime/python312/python.exe`、`C:/Users/ZTEEMS/.../python.exe`、`C:/Python312/python.exe`、`Task/my_app/Scripts/python.exe`——4 个候选全是 Windows 路径，无 `bin/python3` 候选；且第 4 个候选的 `Task/` 路径已过时（现为 `apps/`），属静默失效的兜底 | Linux 上 bot 的 Python 脚本调用（TT 分析、规则查找）会全部回落到不存在的第一个候选。Phase 1 需给该模块加平台分支（`process.platform`），或由 ZIP 启动器显式设 `TT_PYTHON` 环境变量（该变量优先级最高，可绕过候选列表而不改代码——首选过渡方案） |
| 含本机绝对路径的被跟踪脚本 | `apps/my_app/monitor_fixes/nginx_monitor.ps1`（5 个 param）、`apps/portal/rotate_nginx_logs.ps1`（`$Prefix`）；`.githooks/post-checkout`/`post-merge` 在检出后逐字节改写为本机根目录；`README.md`“换机器 clone 后必做一步”要求跑 `setup-git-hooks.mjs` | ZIP 解压不是 git 检出，钩子不会触发。打包时必须把这两处路径改成“启动时推导”或由首次启动脚本重写；不得把构建机的绝对路径打进 ZIP |
| 盘符/Windows 路径残留 | `apps/my_app/backend/services/ms/ms_sla_report.py:1150-1154` 字体候选 `C:/Windows/Fonts/msyh.ttc` 等；`apps/my_app/backend/services/cn_ems/cs_ems_login.py:21` 注释中的 `C:/Users/ZTEEMS/.../python.exe`；`ops/deploy/bundle_data.ps1` 默认 `MainRepo='D:\01_RPA'`；`ops/deploy/sync-engine.mjs:17` 默认源 `D:/01_RPA`（已停用，仅备查）；`apps/whatsapp-kb-bot/src/myAppPaths.js:75` 候选 `D:/01_RPA/Task/my_app` | 多数为注释/默认值/兜底，但字体路径是运行期行为：Linux 无 `C:/Windows/Fonts`，报表字体回退链需验证（matplotlib 默认字体 + 中文缺字风险）。`bundle_data.ps1` 的默认值在 Linux 上无意义，数据迁移脚本需平台分支或参数化 |
| 路径分隔符混用 | `apps/portal/server/teamScopeDb.ts:56` 注释指出 Windows 上 `fileURLToPath` 得 `"/D:/01_RPA/..."`，前导斜杠会让 `path.resolve` 丢盘符（已有处理）；`ops/deploy/post_sync_fix_paths.ps1:83-84` 注释 mixed separators 风险；`nginx.conf.template` 要求 `{{ROOT}}` 正斜杠 | 现有代码多处已有防御，但 ZIP 构建脚本（新写）必须全程用 `path.posix`/`path.resolve` 规范化，不得字符串拼接；nginx 模板展开在 Linux 上同样用正斜杠（天然一致） |
| `node` vs `node.exe` | `portal_qa_ctl.py:104-109` 已做平台分支（win32 取 `node.exe`，其余取 `node`），是正面范例 | 新写启动器照此模式；同时 `PORTAL_QA_NODE` 环境变量可显式指定解释器，ZIP 启动器应始终显式设置它 |
| 目录名含 `&` 与大写 | `apps/eflow_Q&A/`（`&` 在 shell 中是后台操作符，URL 中需编码；`Q&A` 大小写在 Windows 不敏感、Linux 敏感） | 构建/打包脚本中引用该路径必须加引号；`main.py` 预热 `tools/inspect_tt.py` 与 `eflow_qa.py` 的“向上 3 级”推导依赖叶子名** exactly**（`module-map.md` §2：不改名、不放进 `docs`）。ZIP 内保留原名，不得“顺手规范化” |
| `Scripts` vs `bin` | Windows venv 解释器在 `Scripts/python.exe`（`start_hidden.ps1:143-146` 探测），Linux 在 `bin/python` | 打包 Python venv 时，启动器按平台取解释器路径；不得写死任一布局 |

### 5.4 Windows 专属 API 与进程语义

| 问题 | 实证 | 影响 |
| --- | --- | --- |
| 命名互斥量（单例锁） | `apps/my_app/backend/backend_watchdog.py:115-116,139-142`（`ctypes.WinDLL("kernel32")` + `CreateMutexW`）、`worker_watchdog.py:24-25,143-144`（同款）；`backend_watchdog.py:112,333,368` 有 `sys.platform != "win32"` 分支（非 Windows 时跳过/降级，具体行为未逐行审计） | watchdog 的单例语义在 Linux 上无等价实现。隔离副本启动链**不用** watchdog（`start_all.ps1` 头注释），故 Phase 1 的 Linux 隔离启动可绕过；但生产形态的 Linux 移植需要重写单例机制（文件锁 `flock`），属 Phase 2 工作，不得在 Phase 0/1 宣称完成 |
| `taskkill` / 进程树杀 | `backend_watchdog.py:452`（`taskkill /PID ... /T /F`）、`:62`（清理要起 `netstat` + `taskkill` 两个子进程） | Linux 改信号 + 进程组；`agent-worker` 的 `windows_tree.py` 与 Linux 进程组测试（`quality.yml` 的 `agent-linux-process-tests` 作业，`os.killpg`/SIGTERM 真实执行）可作参考实现 |
| 控制台脱离 | `main.py` 的 `FreeConsole()` 调用有 `sys.platform == "win32"` 守卫（`main.py` 顶部） | 正面范例：平台相关调用已守卫。后端主进程本身无硬 Windows 依赖（除 watchdog 链外），是 Linux 可行性的积极信号 |
| `CREATE_NO_WINDOW` | `portal_qa_ctl.py` 用 `_CREATE_NO_WINDOW`（`creationflags`，Windows 专属；非 Windows 时应为 0——该模块已按平台取值，见 `portal_qa_ctl.py` 的 `_CREATE_NO_WINDOW` 定义） | 新写启动器照此模式，不得无条件传 Windows `creationflags` |
| `python` vs `python3` | `ops/deploy/lib/agent-checks.mjs:61` 的 `python()` 助手写死 `'python'`；本审查环境（Linux，`python3.12`，无 `python` 命令）实测 `python: command not found` | 自检脚本与所有新写构建脚本在 Linux 上必须用 `python3`（或探测 `python`→`python3` 回落）。CI 的 `quality.yml` 跑在 `windows-latest` 上用 `python` 无问题，但 Linux 构建作业不得照搬 |
| 环境变量大小写 | `start_all.ps1` 头部有 PS 5.1 专属处理：`Start-Process` 在进程环境同时存在同名不同大小写键（如 `HTTP_PROXY`/`http_proxy`）时会抛 "Item has already been added"，脚本只保留大写键 | Linux shell 环境变量大小写敏感，`HTTP_PROXY` 与 `http_proxy` 是两个变量。ZIP 的 `.env` 解析与启动器必须只认大写规范键，并在文档中声明；不得把 PS 的去重逻辑原样搬到 Linux |

### 5.5 服务管理：Linux 侧无应用级方案

- **Windows 侧**：计划任务（`OLBR_SmokeWatcher` 每分钟重启、`Nginx_EnsureRunning` 每 5 分钟、门户 ensure 任务）、
  watchdog 进程、VBS 隐藏启动器、开机自启 `start_autostart.bat`，是一套完整但 Windows 绑定的保活体系。
- **Linux 侧**：仓库内唯一的 systemd 资产是 `apps/agent-worker-linux/agent-worker-linux.service`
  （模板，`@@...@@` 占位符由 `install.sh` 渲染到 `/etc/systemd/system/`，`User/WorkingDirectory/ExecStart/
  Restart=always/ReadWritePaths/NoNewPrivileges/PrivateTmp`）与配套 `install.sh`（校验 `python3 >= 3.10`、
  幂等安装、打印回滚步骤）。但它只管 agent-worker **一个**进程，不管 4 个业务服务、不管 nginx/PG。
- **结论**：Linux 生产保活需要 Phase 1 新写 systemd unit 模板（每服务一个 unit + `Wants/After` 编排，
  或一个编排 unit + 进程组管理），并复用 `agent-worker-linux.service` 的加固模式
  （`NoNewPrivileges`、`ReadWritePaths` 限定数据目录）。Phase 0 不写 unit，只定方向；
  Phase 1 的 Linux ZIP 先以“前台编排脚本 + pid 文件”达到与 `start_all.ps1` 对等的可启停，
  systemd 模板作为可选的第二步（避免一次性引入过多新机制）。

### 5.6 其他兼容性实证

| 问题 | 实证 | 影响 |
| --- | --- | --- |
| 文件名大小写冲突 | 实测无冲突：全仓文件路径小写化后无重复（`find \| tr A-Z a-z \| uniq -d` 为空） | 好消息：ZIP 在大小写敏感文件系统上解压不会自冲突。但 Python `import` 大小写敏感：全量 import 审计未做，列入未核实项 |
| 可执行位 | `apps/agent-worker-linux/install.sh` 与 `.githooks/pre-commit` 在 git 中带可执行位（检出为 `rwxrwx---`）；其余脚本无 | ZIP 格式本身可存 Unix 权限，但 Windows 工具打包/解压常丢权限。Linux ZIP 必须由构建脚本在打包后校验 `.sh` 的可执行位，并在首次启动脚本中 `chmod +x` 兜底（幂等） |
| `rm -rf` 脚本 | 门户 `clean` 脚本（见 §2.2） | 构建脚本不得调用 |
| PowerShell 版本 | `start_all.ps1` 明确 PS 5.1 兼容（不用 `::new()`/三元/`-Parallel`/`$IsWindows`）；`write_nginx_conf.ps1` 显式调 `WindowsPowerShell\v1.0\powershell.exe` | Windows ZIP 的启动器继续以 PS 5.1 为基线，不得要求 `pwsh` 7+；Linux 侧不依赖 PowerShell |
| 换行 + BOM 组合 | `start_all.ps1` 实测 BOM+CRLF；`smoke-local.ps1` BOM+CRLF；`install.sh` 无 BOM+LF | 见 §5.2 的规范化要求 |
| 时区/区域设置 | 未发现硬编码时区；日志时间戳用本地时间（`logging` 默认）；`tzdata`/`tzlocal` 在锁定文件中 | 低风险。ZIP 不改时区行为；多机部署时在文档中注明“各机本地时间”口径 |

---

## 6. ZIP 打包设计方案

> 本节是**设计方案**（Phase 1+ 的实施依据），不是现状描述。方案中的新文件、新工作流均不存在于基线中，
> 需后续任务实现；Phase 0 只审查、不新建这些文件。方案严格遵守抬头约束。

### 6.1 产物划分：双 ZIP 模型（继承现有“源码/运行时分离”思想）

现状已有分离先例：`runtime/`（6.5 GB，不入库）与源码分离（`runtime/README.md`）；`build_olbr_portable.ps1`
的阶段划分（`source` / `nodemodules` / `runtimes` / `nginx` / `config`）也是分离思想。方案将其形式化为
两个产物：

| 产物 | 内容 | 更新频率 | 大小量级（估算，需 Phase 1 实测） |
| --- | --- | --- | --- |
| **应用 ZIP** `noc-soc-platform-<版本>-<平台>.zip` | 源码（白名单）+ 预构建前端（`apps/_fe_dist/`、门户 `dist/`）+ 预装 `node_modules`（4 个项目）+ 预装 Python venv（lock 文件安装）+ 启动/停止脚本 + `nginx.conf.template` + `build-info.json` + `SHA256SUMS` | 每次发版 | 源码 49 MB + 构建产物（`_fe_dist` 约 142–277 MB，见 `README.md` 与 `部署说明.md`，口径不一）+ `node_modules` + venv（均未实测，列入未核实项） |
| **运行时 ZIP** `noc-soc-runtime-<运行时版本>-<平台>.zip` | Python 3.12.10、Node v24.16.0、PostgreSQL 16、nginx 1.28.0（含地图瓦片 `liberia.pmtiles`，不含证书私钥）+ Playwright 浏览器二进制（如确认需要） | 运行时版本变化时 | 现状 `runtime/` 6.5 GB 含 `pgdata`（约 4.9 GB）；**去掉 `pgdata` 后**的纯发行包体积未实测，需 Phase 1 称重后决定是否拆分为“最小运行时”与“完整运行时” |

为什么不打成一个 ZIP：① 运行时发行包体积大且变化慢，与应用分开发布可避免每次发版重传数 GB；
② 运行时 ZIP 可在多版本应用间复用；③ 符合“配置、数据、日志独立于版本目录”的约束——运行时目录
与应用版本目录、数据目录三者互不嵌套（见 §6.5 布局图）。

平台后缀：`win-x64` 与 `linux-x64`。Linux 应用 ZIP 与 Windows 应用 ZIP 的源码部分相同，
差异只在：启动/停止脚本（`.bat/.ps1` vs `.sh`）、行尾/权限规范化、venv 解释器布局（`Scripts` vs `bin`）、
平台相关的 `node_modules` 原生绑定（如有——`npm ci` 必须在对应平台的 runner 上跑，不得跨平台复用
`node_modules`）。

**不打包的内容**（硬排除清单，构建脚本必须断言——对照 `ops/quality/check_all.ps1` 第 3 步的检查思想）：

- `runtime/pgdata/` 及任何 `PG_VERSION` 所在目录；`*.sql`/`*.dump` 快照文件。
- 任何 `.env`、`.env.*`（模板 `.env.example` / `env_template.txt` 除外）、`*.key`、`*.pem`、
  `certs/portal.key`、`*session*.json`、`auth*.json`、`*credential*`、`config.json`（bot 的含密钥配置）、
  `apps/whatsapp-kb-bot/data/`、`apps/whatsapp_session/`、`apps/portal/auth-data/`、
  `runtime/portal-auth-data/`、`apps/my_app/db/`、`apps/my_app/data/`、`TMP/`、`log/`、`*.log`、
  `node_modules` 之外的缓存（`.cache`、`__pycache__`）、`.git/`、`*_bak_*`/`*.bak*` 快照。
- 构建脚本在打包前跑“排除断言”：按清单 grep 产物暂存目录，命中即失败；并跑一次凭据模式扫描
  （`.env` 文件存在性、`BEGIN PRIVATE KEY`、`JSESSIONID` 等——对照 `README.md` 第六节的泄露史，
  扫描规则宁可误报不可漏报，误报由人工复核）。

### 6.2 版本号规范

现状：无版本真相源——`apps/portal/package.json` 与 `apps/my_app/frontend/package.json` 的 `version` 均为
`0.0.0`，bot 为 `1.0.0`，FastAPI `version="1.0.0"`（`main.py:184`），三者互不关联；全仓无 `VERSION`/
`CHANGELOG` 文件；远程唯一标签是 `refactor-baseline-2026-11-13`（非发版标签，`git ls-remote --tags` 验证）；
bot 的 `src/buildinfo.js` 用源码最大 mtime 作“版本指纹”（进程新旧判断，非发版版本）。

方案（新规范，需 Muse 1 / 用户确认后由 Phase 1 落地）：

1. **发版版本 = git 标签**，格式 `v<major>.<minor>.<patch>`（SemVer 语义：`major` 破坏性变更、
   `minor` 功能新增、`patch` 修复；发版前由负责人打标签，CI 只认标签触发，不认分支）。
2. **产物文件名**：`noc-soc-platform-v1.2.3-win-x64.zip` / `noc-soc-platform-v1.2.3-linux-x64.zip`；
   运行时产物：`noc-soc-runtime-python3.12.10-node24.16.0-pg16-nginx1.28.0-win-x64.zip`
   （运行时版本直接写发行包版本组合，避免第二套 SemVer 语义混乱）。
3. **版本内嵌**：每个应用 ZIP 根部放 `build-info.json`（构建时生成，不入库）：
   ```json
   {
     "name": "noc-soc-intelligent-platform",
     "version": "v1.2.3",
     "commit": "<40 位 HEAD SHA>",
     "commit_short": "<7 位>",
     "built_at": "<UTC ISO 8601>",
     "platform": "win-x64",
     "runtime": {"python": "3.12.10", "node": "24.16.0", "postgresql": "16", "nginx": "1.28.0"},
     "alembic_head": "<迁移头 revision，如 5415704359dc>",
     "artifacts": ["noc-soc-platform-v1.2.3-win-x64.zip"]
   }
   ```
   `alembic_head` 由构建脚本跑 `alembic heads`（或解析版本文件链）取得，供首次启动脚本校验
   “数据库迁移头是否与代码匹配”（不匹配时拒绝启动并提示先迁移——避免 §3.4 所述 split-brain 类事故
   在数据层重演）。启动脚本在日志首行打印 `version + commit_short`，替代 mtime 指纹的“进程新旧”判断
   （`buildinfo.js` 的机制保留，不强制替换）。
4. **`0.0.0` 占位**：Phase 1 不批量改各 `package.json` 的 `version`（避免与现有守卫/文档产生无关 diff）；
   发版版本只由标签 + `build-info.json` 承载。待发版流程跑通后，再由后续任务决定是否同步各包版本。

### 6.3 应用 ZIP 内部布局（版本目录只读）

解压后形成一个版本目录（示例 `noc-soc-platform-v1.2.3/`），**启动后不得向其中写入**（除首次启动生成的
nginx 配置——见下；该文件也应生成到外部配置目录，见 §6.5）：

```
noc-soc-platform-v1.2.3/
├── build-info.json               # §6.2
├── SHA256SUMS                    # 本 ZIP 内关键文件的哈希（见 §6.6）
├── README-DEPLOY.md              # 部署说明（从 docs/workspace/部署说明.md 精简，面向拿到 ZIP 的人）
├── apps/
│   ├── my_app/                   # 源码 + frontend 源码（保留，供排障；不含 node_modules 以外的构建输入冗余）
│   │   ├── frontend/             # 含 node_modules/（预装）
│   │   └── backend/              # Python 源码（venv 在外层，见下）
│   ├── portal/                   # 源码 + dist/（预构建）+ node_modules/（预装）
│   ├── whatsapp-kb-bot/          # 源码 + node_modules/（预装）
│   ├── eflow_Q&A/                # 知识库（原名保留，见 §5.3）
│   ├── _fe_dist/                 # my_app 前端构建产物（预构建，FE_DIST_OUT 默认指向此处）
│   ├── agent-worker/             # 源码（标准库 only，不含 venv）
│   ├── agent-worker-linux/       # 源码 + install.sh（Linux 产物保留可执行位）
│   └── control-plane/            # 源码
├── docs/                         # 部署相关文档子集（部署说明、数据字典索引；不含全量历史文档——由白名单定）
├── ops/
│   ├── deploy/                   # start/stop 脚本（平台相关）、write_nginx_conf、env_template.txt、agent-done-check
│   └── smoke/                    # 冒烟脚本（与 CI 冒烟同一套，供目标机自检）
├── extensions/                   # 浏览器扩展源码（按需）
├── nginx.conf.template           # 模板（生成器在启动时展开到外部配置目录）
├── start.bat / stop.bat          # Windows 入口（CRLF + ASCII，构建时规范化）
├── start.sh / stop.sh            # Linux 入口（LF + 可执行位；新写，见 §6.4）
└── .venv/                        # 预装 Python 虚拟环境（lock 文件安装；Windows: Scripts/，Linux: bin/）
    └── ...                       # 解释器本身仍用运行时 ZIP 的 Python（.venv 用 --system-site-packages? 否——见下）
```

关键设计决策（需 Phase 1 实测验证，均已列入未核实项）：

- **venv 归属**：两种路线——(A) `.venv` 打进应用 ZIP（自包含，但体积大、且 venv 含绝对路径 shebang，
  换目录即失效，需 `virtualenv --relocatable` 或启动时重建 venv 链接）；(B) 构建期把 lock 文件的 wheel
  打进 `wheels/` 目录，首次启动时用运行时 Python **离线** `pip install --no-index --find-links=wheels`
  建 venv（换目录安全，首次启动稍慢但零下载）。**推荐 (B)**：符合“首次启动零下载”且规避 venv  relocation
  坑；代价是首次启动多一步（幂等，已有 venv 则跳过）。`node_modules` 无此问题（相对路径为主），直接打进 ZIP。
- **前端产物**：`apps/_fe_dist/` 与门户 `dist/` 必须由 CI 构建后打进 ZIP，不得期望目标机 `npm run build`
  （目标机可能无网络、且构建需 `node_modules` 完整）。构建脚本跑 `npm run build` 时的守卫
  （`check-i18n`、`tsc`）失败即整单失败。
- **`_fe_dist` 的 `emptyOutDir: false`**（`vite.config.ts:93`）：CI 构建必须在干净暂存目录中进行，
  避免陈旧 chunk 混入；并在构建后跑 `prune-fe-dist.mjs` 的闭包校验（或等价断言）确认产物无陈旧文件。
- **源码白名单**：沿用 `sync-policy.mjs` 的白名单思想（现已停用、仅备查）+ `.gitignore` 的排除思想，
  由构建脚本用显式 include/exclude 列表组装，不得 `git archive` 全量（全量会带入 `.agent-work/`、
  `.workbuddy-ai/` 记忆文件、`TMP/` 残留、`*.bak*` 等无关内容——`git ls-files` 共 1700 个文件，
  需逐类过白名单）。

### 6.4 GitHub Actions 自动构建与 Release 上传（新工作流提案）

现状：11 个工作流（`agent-orchestrator`、`auto-dispatch`、`auto-fix`、`auto-merge`、`ci-failure-notify`、
`dispatch-check`、`issue-to-pr`、`lease-reaper`、`pr-merged-writeback`、`quality`、`refs-census`），
**无任何 release/artifact 工作流**（grep `softprops|upload-release-asset|gh release|upload-artifact` 为空）。
`quality.yml` 是唯一的质量门（`windows-latest` 为主 + 1 个 `ubuntu-latest` 作业，Python 3.12 / Node 24）。

提案：新增 `.github/workflows/release-build.yml`（Phase 1 实现，Phase 0 只定接口）：

```yaml
# 提案骨架（非最终 YAML，以 Phase 1 实现为准）
name: release-build
on:
  push:
    tags: ['v*']            # 只认 SemVer 标签
  workflow_dispatch:        # 手动重跑/预演（dry-run 模式只构建不发布）
permissions:
  contents: write           # 创建 Release + 上传产物（仅此工作流）
jobs:
  build:
    strategy:
      matrix:
        os: [windows-latest, ubuntu-latest]
        # windows-latest -> win-x64；ubuntu-latest -> linux-x64
        # node_modules 必须在目标平台 runner 上 npm ci，不得跨平台复用
    steps:
      - checkout（fetch-depth 0，取标签 + 完整历史供 build-info 取 SHA）
      - setup-python 3.12 / setup-node 24（与 quality.yml 相同 pin）
      - 质量门：复用 quality.yml 的关键作业（compileall、ruff E9/F821、门户 lint/test、
        my_app 文档守卫、pytest 离线集）——或声明 `needs: quality` 式依赖；
        质量门失败则不构建（避免把红代码打进 ZIP）
      - npm ci（4 个项目，逐个跑；含 whatsapp_analysis）
      - npm run build（my_app 前端 + 门户；产物校验：_fe_dist/index.html 与 dist/index.html 存在）
      - pip download -r requirements.lock.txt -d wheels/（构建 wheels 目录；--only-binary 优先，
        需编译的包在 runner 上编好 wheel，避免目标机现场编译）
      - 组装暂存目录（白名单 + 排除断言 + 凭据扫描，见 §6.1/§6.3）
      - 行尾/权限规范化（见 §5.2）+ build-info.json 生成 + SHA256SUMS 生成
      - 压缩：Windows 用 PowerShell Compress-Archive 或 7z（需验证中文文件名与长路径）；
        Linux 用 zip（保留权限）；产物名见 §6.2
      - 上传 artifact（供人工下载验证）+ 发布到 GitHub Release（标签触发时），附 SHA256SUMS 与 Release Notes
        （Notes 由标签与 PR 列表自动生成 + 人工填写的“迁移提示”段）
```

补充规则：

- **运行时 ZIP 单独构建**：`runtime-build.yml`（或同一工作流的独立 job），触发条件为手动 dispatch +
  运行时版本变量变化；步骤为下载官方发行包 → 校验官方哈希 → 组装 `runtime/` 布局 → 压缩 → 发布。
  官方哈希校验是必须项（供应链安全），哈希值写进工作流或 `runtime-manifest.json`（新文件，入库）。
- **Linux 构建的 smoke 范围**：Linux runner 上只能做“构建 smoke”（compileall、前端构建、Alembic
  离线 SQL 生成、启动脚本 shellcheck）；**不能**宣称“Linux 全链路验证通过”（watchdog/worker/计划任务链
  无 Linux 等价物，见 §5.4/§5.5）。Linux 全链路验证需 Phase 1 在 Linux 目标机（或容器外的 VM）上手工跑通后，
  再把步骤固化为 CI 作业——不得在未验证前把 Linux ZIP 标记为生产可用。
- **Windows 构建的 smoke 范围**：可在 `windows-latest` 上跑 `start_all.ps1 -DryRun`（干跑不启动进程）
  与 `nginx -t`（需 nginx 二进制随运行时 ZIP 提供或 runner 现取）；真实启动验证仍依赖 D:\02 冒烟
  （`smoke-watcher.ps1` + `ops/smoke/smoke.ps1`），Release 发布前须有 D:\02 对该标签的冒烟通过记录。
- **不触发 auto-merge**：Release 构建只读源码、不产生 PR，与现有 auto-merge/auto-dispatch 链无交集；
  但构建脚本与工作流本身的 PR 仍走正常 CI + 冒烟 + Muse 1 审核流程。

### 6.5 配置、数据、日志、上传独立于版本目录

目标布局（部署根目录下三者分离；`NOC_HOME` 为部署根，由部署方选定，版本目录、运行时目录、数据目录
均为其子目录，互不嵌套）：

```
<NOC_HOME>/
├── versions/
│   └── v1.2.3/                    # 应用 ZIP 解压目录（只读；升级 = 解压新版本 + 切换符号链接/配置指向）
├── current -> versions/v1.2.3/    # Linux 符号链接；Windows 用启动脚本参数/ junction（需验证）或直接写版本号
├── runtime/                       # 运行时 ZIP 解压目录（Python/Node/PG/nginx；多版本共享）
├── config/                        # 外部配置目录
│   ├── .env                       # 唯一配置真相源（从 env_template.txt 复制，部署方填写）
│   ├── backend.env                # my_app backend/.env（如需与根 .env 分离）
│   ├── bot.env                    # bot .env
│   └── nginx.conf                 # 生成的 nginx 配置（启动时由模板展开至此，不写版本目录）
├── data/                          # 外部数据目录
│   ├── pgdata/                    # PG 数据目录（PGDATA 指向此处；首次启动 initdb）
│   ├── app.db                     # 主 SQLite 库（需代码改动配合，见下）
│   ├── myapp-data/                # 派生 SQLite 与任务数据（逐步迁移）
│   ├── portal-auth-data/          # 门户鉴权库（PORTAL_AUTH_DATA_DIR 指向此处）
│   ├── portal-default.json        # 门户默认目录（PORTAL_DEFAULT_FILE）
│   ├── bot-data/                  # bot 的 index/lookups/qa.sqlite/auth（INDEX_PATH/AUTH_DIR 等指向此处）
│   └── uploads/                   # 上传文件（需核实落盘根后定，见未核实项）
├── logs/                          # 外部日志目录
│   ├── portable-*.log             # 启动器日志（TMP 的替代）
│   ├── backend/                   # 后端/ worker 日志
│   ├── portal/                    # 门户日志
│   └── nginx/                     # nginx access/error 日志
└── backups/                       # 升级前备份（迁移前快照、local_authority 备份；不进版本目录）
```

落地手段（按“无需改代码 / 需后续改代码”分两档，Phase 0 只审查、分档，不实施）：

**无需改代码（环境变量/启动脚本即可，Phase 1 可做）：**

| 数据/配置 | 手段 | 证据 |
| --- | --- | --- |
| PG 数据目录 | 启动脚本设 `PGDATA=<NOC_HOME>/data/pgdata`；`start_all.ps1` 现状已用派生变量覆盖（`:140,151`） | `start_all.ps1:140`、`env_template.txt`（PGDATA 段） |
| 门户鉴权数据 | `PORTAL_AUTH_DATA_DIR=<NOC_HOME>/data/portal-auth-data` | `server/index.ts:51-52`、`vite.config.ts` 的 `AUTH_DATA_DIR` |
| 门户默认目录 | `PORTAL_DEFAULT_FILE=<NOC_HOME>/data/portal-default.json` | `server/index.ts:46-47` |
| 前端产物目录 | `FE_DIST_OUT` 指向版本目录内的 `_fe_dist`（产物随版本走，正确）；不得指向共享目录（多版本会互相污染） | `vite.config.ts:84` |
| Bot 数据 | `INDEX_PATH`、`AUTH_DIR`、`QA_LOG_PATH` 等指向 `data/bot-data/` | `src/config.js:286-297` |
| 问答服务 node 路径 | `PORTAL_QA_NODE=<NOC_HOME>/runtime/node/node(.exe)` | `portal_qa_ctl.py:104-109` |
| Bot Python 解释器 | `TT_PYTHON=<NOC_HOME>/runtime/python312/python(.exe)`（绕过 §5.3 的 `.exe` 候选问题） | `pythonPath.js`（`TT_PYTHON` 最高优先级） |
| 数据库连接 | `DATABASE_URL` + `MY_APP_DB_MODE` 由外部 `.env` 提供；连接串不进 ZIP | `database.py:27,34` |
| 模型服务密钥 | 外部 `.env` 的 `OPENWEBUI_API_KEY`/`CHAT_API_KEY`，留空则功能降级（现状模板即留空） | `env_template.txt` |

**需后续代码改动（Phase 1+ 任务，不得在 Phase 0 偷改）：**

| 数据/配置 | 现状障碍 | 建议方向（后续任务定） |
| --- | --- | --- |
| `db/app.db` | `database.py:31` 写死相对路径，无运行时覆盖入口 | 加环境变量（如 `MY_APP_DB_PATH`）覆盖，默认保持相对路径（向后兼容）；同步改 `task_runner.py` 的 `DB_PATH` 与 `init_db.py` 的 `DB_PATH`（三处同义定义需收敛） |
| 各任务专用 SQLite | 多数相对路径散落各服务，逐个盘点未完成（`data-source-registry.md`“待确认”） | 逐表/逐文件盘点后，给每个落盘点加“数据根”推导（`NOC_DATA_DIR` + 相对路径），默认仍为现路径 |
| 上传文件落盘根 | `UploadedFile` 模型存在，但落盘根未逐一核实 | 核实后同上处理 |
| 日志目录 | `TMP/` 与各 `log/` 均在版本/副本目录内；`log_writer.py` 的落盘路径未逐一核实 | 启动脚本设日志根环境变量（如已有则复用），或 Phase 2 加日志目录配置项；过渡期用符号链接（Linux）/目录联接（Windows junction）把版本目录内的 `TMP/`、`log/` 指向外部 `logs/`（需验证 junction 在 Windows 非管理员下的可用性） |
| nginx 日志/临时目录 | `nginx-1.28.0/logs/` 在运行时目录内 | nginx 配置中 `error_log`/`access_log`/`pid`/`client_body_temp_path` 等指向外部 `logs/` 与 `config/`（模板改动，属配置层，不动业务代码） |
| 证书 | `nginx-1.28.0/certs/` 在运行时目录内 | 首次启动脚本在 `config/certs/` 生成自签证书（不存在时），nginx 配置指向外部路径；部署方提供正式证书时替换该处文件即可 |

**升级/回滚语义**（由布局保证）：升级 = 解压新版本到 `versions/vX.Y.Z/` → 跑 `alembic upgrade head`
（对外部 PG）→ 切换 `current` 指向 → 重启服务；回滚 = 切回旧版本 → `alembic downgrade <旧头>`
（如迁移不可逆则需备份恢复，见 `backups/`）→ 重启。数据目录在升级/回滚中**原地不动**，
版本目录**整体替换**（`emptyOutDir: false` 的陈旧 chunk 问题因此消失：旧版本目录整体删除，不存在增量污染）。

### 6.6 SHA256 校验

1. **构建时**：对每个产物 ZIP 生成 `<产物名>.sha256`（单行 `SHA256  文件名`，`sha256sum` 格式）；
   同时在 ZIP 内放 `SHA256SUMS`（关键文件清单：启动脚本、模板、`build-info.json`、前端入口 `index.html`），
   供目标机在启动前自检“解压是否完整/是否被篡改”。
2. **发布时**：Release 页面同时上传 ZIP 与 `.sha256`；Release Notes 首行写明校验命令
   （Windows：`Get-FileHash -Algorithm SHA256`；Linux：`sha256sum -c`）。
3. **首次启动时**：启动脚本（新写部分）在拉起服务前校验 `SHA256SUMS`（版本目录内文件未被改动）；
   校验失败则拒绝启动并报错（避免“解压损坏却带病启动”）。校验范围只含版本目录内文件，
   不含外部数据/配置（那些是运行时可变的）。
4. **运行时发行包**：官方下载后立即用官方公布哈希校验（见 §6.4），校验失败则构建失败；
   哈希值入库到 `runtime-manifest.json`，供审计。
5. **不做代码签名**（Phase 0 结论）：仓库内无签名证书/流程，Windows SmartScreen 与 Linux 包签名均不在
   Phase 1 范围；SHA256 + GitHub Release（HTTPS + 仓库权限）是 Phase 1 的信任边界。后续如需签名，
   另立任务（需证书采购/保管流程，用户决策）。

### 6.7 首次启动流程（目标机，零下载）

```
1. 解压运行时 ZIP -> <NOC_HOME>/runtime/（如已存在同版本则跳过）
2. 解压应用 ZIP   -> <NOC_HOME>/versions/vX.Y.Z/，切换 current 指向
3. 复制 config 模板 -> <NOC_HOME>/config/.env（已存在则不覆盖），部署方填写端口/密钥/模型地址
4. 启动脚本自检：SHA256SUMS 校验、解释器存在性、端口冲突熔断（沿用 start_all.ps1 的生产端口集合）、
   ISOLATED_COPY.flag 语义（隔离/生产形态选择——由 config 显式声明，不依赖 flag 文件存在性推断）
5. 首次启动建环境（幂等，已存在则跳过）：
   - 生成自签证书到 config/certs/（不存在时）
   - initdb -> data/pgdata/（无 PG_VERSION 时），pg_ctl start，createdb
   - 建 PG schema（如 Phase 1 实测确认 Alembic 不建 schema——见 §4.3 未核实项）
   - alembic upgrade head（对外部 PG；失败则停止并保留日志，不带病启动）
   - 离线建 venv（pip --no-index --find-links=wheels/，见 §6.3 路线 B；已有则跳过）
   - 展开 nginx.conf 到 config/ 并 nginx -t
6. 按 §3.4 的六步顺序拉起服务（Linux 用 .sh 等价实现），端口逐个验活
7. 打印入口 URL + 版本/commit（build-info.json），写启动日志到 logs/
```

数据迁移（已有部署升级/换机）是**独立流程**，不在首次启动内自动跑：`bundle_data.ps1` 的 dump/restore
（PG 快照）与 `local_authority_tools.py` 的 check/migrate（三张本地权威表）由部署方显式执行，
并在 `backups/` 留快照。首次启动脚本不得静默灌入任何快照（快照含生产数据，与程序 ZIP 分离是硬约束）。

### 6.8 Phase 1 实施清单（供后续任务拆解，本报告不实施）

| # | 任务 | 验收标准（要点） |
| --- | --- | --- |
| P1-1 | 干净 PG 实测：initdb → createdb → alembic upgrade head，确认 schema 创建责任方 | 实测记录 + 首次启动脚本中的建 schema 步骤（如需） |
| P1-2 | 称重：`node_modules`（4 项目）、venv/wheels、运行时发行包（去 pgdata 后）的体积 | 体积表 + 是否拆包的决策 |
| P1-3 | 构建脚本（跨平台 Node/Python 实现）：白名单组装 + 排除断言 + 凭据扫描 + 行尾/权限规范化 + build-info/SHA256 生成 | 在 `windows-latest` 与 `ubuntu-latest` 上跑通，产物可解压 |
| P1-4 | Linux 启动/停止 `.sh` 编排器（对标 `start_all.ps1` 六步 + pid 文件 + 熔断） | 在 Linux 目标机上跑通隔离启动/停止全流程 |
| P1-5 | `release-build.yml` + `runtime-build.yml`（或等价）+ Release 发布 | 标签触发后 Release 出现双平台产物 + `.sha256` |
| P1-6 | 外部目录改造（无需改代码档）：启动脚本全面改用外部 config/data/logs | 版本目录在启动后零写入（`find -newer` 验证） |
| P1-7 | `db/app.db` 等路径的环境变量覆盖（需改代码档，另立任务、走正常审核） | 外部数据目录切换后服务正常，默认行为不变 |
| P1-8 | D:\02 对首个标签产物的冒烟（`smoke-watcher`/`smoke.ps1`）+ Linux 目标机手工验证 | 冒烟报告 + 验证记录 |

---

## 附录 A：本次审查的自检记录（草稿阶段）

执行环境：Linux 浅克隆（`/tmp`，HEAD `2014a27`），Node v24.22.1，Python 3.12.3（仅 `python3` 命令，
无 `python` 命令），未安装 `node_modules`，未安装 Python 依赖。草稿置于拟定路径
`docs/workspace/zip-delivery-phase0-review.md` 后执行。

命令：`node ops/deploy/agent-done-check.mjs --full`

结果（2026-11-16，审查当日，两次运行一致）：**7 项中 6 项通过，1 项未通过；未通过项与本草稿无关，
系审查环境缺 `python` 命令所致**（对照验证：移走草稿后重跑，结果逐项相同）。

| 检查项 | 结果 | 说明 |
| --- | --- | --- |
| 门户整洁度 | ✓ 通过 | |
| 团队门户生成物一致性 | ✓ 通过 | |
| 门户文档一致性 | ✓ 通过 | |
| 门户 i18n | ✓ 通过 | |
| my_app 文档一致性 | ✗ 未通过（0.003s，detail 为空） | 自检脚本 `ops/deploy/lib/agent-checks.mjs:61` 的 `python()` 写死 `'python'` 命令，本机无此命令，spawn 失败。用 `python3` 直接跑同一脚本（`cd apps/my_app && python3 tests/test_doc_consistency.py`）结果为 **17/17 PASS**，证明守卫本身通过、失败纯属环境命令名问题。未改代码去迁就。 |
| my_app i18n | ✓ 通过 | |
| my_app pytest（相对基线无新增失败） | ✓ 显示“全绿”（0.002s） | **该“通过”不可信**：同一原因（`python` 命令缺失），pytest 实际未执行；`fullChecks` 只解析输出中的 `FAILED/ERROR` 行，未校验进程退出码，空输出即判“全绿”。此为自检脚本在缺 `python` 环境下的假通过（latent bug），已如实记录，不得引用为“pytest 已过”。本草稿为纯文档新增，无 `.py` 改动，pytest 本就不在本次变更的验证范围内。 |

未执行的项（按脚本设计跳过，非失败）：my_app 前端构建（仅改动 `frontend/` 时跑）、门户 lint
（仅改动 `portal/` 时跑）——本次变更仅 1 个 `docs/` 文件，两项均按 `fullChecks` 的 `touched()` 条件跳过，
符合“只在真正相关的文件被动过时才跑”的设计。

结论（第一次运行）：**自检未完全跑通，原因在环境而非草稿**。后经复核运行（见下）确认守卫本身通过。

复核运行（Muse 4，2026-11-16，同一 Linux 环境加 `python` → `python3` 垫片后重跑，草稿为最终版内容）：
`node ops/deploy/agent-done-check.mjs --full` 退出码 0，**7 项全部通过**（指纹 `e6d36e4d1f66`），
含 my_app 文档一致性。但 pytest 项在本环境中仍为空跑：本机未安装 pytest
（`python -m pytest` 报 `No module named pytest`，0.0s 未实际执行），脚本按“相对基线无新增失败”
判通过——不得引用为 pytest 已过；真实 pytest 由 CI（quality.yml）执行。本次变更仅 1 个 `docs/` 文件，
未动任何 `.py` 文件，前端构建与门户 lint 按脚本设计跳过。

自检命令（任务要求）：`node ops/deploy/agent-done-check.mjs --full`。本附录记录上述两次实际执行结果；
PR 后以 CI 结果为准。

## 附录 B：未核实项

以下项目在本次只读审查中**未能从 main 代码中确认**，报告中未对它们下结论；不得将本节内容读作“已确认无问题”。
编号供后续任务引用。

1. **新鲜空 PG 上 `alembic upgrade head` 的 schema 创建责任**：迁移不建 schema（`0005` 注释依赖 search_path
   首位 `app` 存在），`transfer_lib.py` 的建 schema 逻辑属于搬运流程。空库直接 `upgrade head` 是否成功、
   表落到哪个 schema，未实测。（见 §4.3；Phase 1 任务 P1-1。）
2. **Linux 上整套服务从未跑通过**：后端主进程的平台守卫是积极信号，但 worker/watchdog/计划任务保活链、
   `portal_qa_ctl` 的 DETACHED 拉起、实时代理子进程、bot 的 Python 桥接在 Linux 上的实际行为均未验证。
   本报告的 Linux 结论限于静态分析。（见 §5；Phase 1 任务 P1-4。）
3. **产物分体积未实测**：4 个项目的 `node_modules` 总和、Python venv / wheels 目录、去 `pgdata` 后的
   运行时发行包、`_fe_dist` 与门户 `dist` 的实际构建体积，均未在干净环境构建称重。
   `README.md` 与 `部署说明.md` 对 `_fe_dist` 的体积口径不一致（277 MB vs 142 MB），以实测为准。
   （Phase 1 任务 P1-2。）
4. **Playwright 浏览器二进制来源**：`playwright` 在锁定文件中，但全仓未见 `playwright install` 的执行位置；
   生产机浏览器二进制从何而来、是否随运行时发行、版本为何，未核实。截图链路（site_outage / whatsapp /
   kpi_email_report）是否因此在新机上断裂，未核实。
5. **上传文件落盘根**：`UploadedFile` 模型与上传路由存在，但上传文件的实际落盘目录（及是否可由环境变量
   覆盖）未逐一核实。外部 `uploads/` 目录的设计待核实后定。（见 §4.5、§6.5。）
6. **日志落盘点全量清单**：`log_writer.py`、各 watchdog、任务日志的落盘路径未逐一核实；
   “日志全部外移”的改动面需逐点确认。（见 §6.5。）
7. **agent-worker / control-plane 全量 import 审计**：仅抽查顶层 `import` 为标准库，未做全量（含动态导入、
   `importlib` 加载、条件导入）审计。“标准库 only”的结论以抽查为限。
8. **Python import 大小写全量审计**：未做。文件名本身无大小写冲突（已验证），但 `import` 语句与文件名
   大小写不一致会在 Linux 上失败，需全量审计后确认。
9. **根 `start.bat`/`stop.bat` 的 LF 检出根因**：Linux 浅克隆中实测 blob 与工作区均为 LF，
   与 `.gitattributes` 的 `eol=crlf` 期望不一致；Windows 检出时是否转为 CRLF 未验证。
   （见 §5.2；Phase 1 构建门禁须含行尾断言。）
10. **各子系统 `.env` 的完整键清单与优先级**：已核实 `backend/.env`、根 `.env`、bot `.env`、门户
    `.env.local` 的存在与大体优先级，但全量键名、默认值、跨文件覆盖的完整矩阵未逐一核实；
    外部 `config/` 目录的最终文件划分待 P1-6 实施时以代码为准。
11. **GBase / FineBI / eFlow / UME 上游的可用性与版本**：仅确认代码侧的连接方式与只读约束，
    上游实际版本、网络可达性、凭据轮换状态不在本次审查范围（也不应在本报告中猜测）。
12. **Windows 非管理员账户下的 junction/符号链接可用性**：§6.5 过渡方案（junction 指向外部日志目录）
    依赖该能力，未验证；如不可用，改走“启动脚本显式设日志路径”路线。
13. **`whatsapp_analysis` 服务的构建产物形态**：该服务用 `tsx` 直跑 TS 源码，无构建步骤；
    ZIP 中是否保留源码直跑、还是预编译为 JS，未定（Phase 1 构建设计时定，倾向保留源码直跑以减少构建面，
    但须确认 `tsx` 在目标机离线可用——它已在 `node_modules` 中，随包提供）。
14. **nginx Windows 二进制与 Linux 二进制的配置差异**：模板本身跨平台，但 `nginx -t` 在两平台的
    具体行为（`mime.types` 路径、`logs/` 相对路径解析、Windows 上的 `worker_processes` 语义）未实测；
    Linux 侧 nginx 来源（系统包 vs 捆绑二进制）未定。
15. **Issue #99 正文的完整验收标准**：已核实（Muse 4 于 2026-11-16 对照 Issue 正文复核）：六项输出与正文一致；验收标准（报告覆盖六项且基于 main 实际代码核实；ZIP 方案明确双平台产物、版本号规范、SHA256 校验、GitHub Actions 自动构建与 Release 上传；PR 通过 CI 并经 Muse 1 审核）均已在本报告 §1–§6 中覆盖，CI 与 Muse 1 审核待 PR 后进行。

---

*报告结束。本报告为只读审查结论与设计方案；所有“方案”“提案”“建议”均需后续任务实现并经 CI、冒烟与 Muse 1
审核后方可生效。*
