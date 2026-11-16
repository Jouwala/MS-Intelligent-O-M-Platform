# ops/deploy — 02 副本的运维脚本目录

本目录是隔离副本（`D:\02_olbr_portal`）的运维脚本目录，由第一轮重构批 C
从仓库根的 `scripts\` 搬入。入口：

- 根 `start.bat` / `stop.bat` → `start_all.ps1` / `stop_all.ps1`
  （一键启动 / 停止本副本的四个子系统：my_app、portal、nginx、WhatsApp bot）
- `ops/shortcuts/` 里的桌面快捷方式 bat（状态、干跑、打开首页/日志等）
- `setup-git-hooks.mjs`：把 `core.hooksPath` 指向仓库内的 `.githooks/`
- `post_sync_fix_paths.ps1`：checkout / merge 后把两个监控脚本里的本机
  绝对路径改成本机副本根目录（由 `.githooks/post-checkout`、`post-merge` 调用）
- `local_authority_tools.py`：迁移时校验 / 复制 SQLite 本地权威三表
  （`credentials`、`task_schedule_config`、`email_recipients`）
- `sync-ui-config-from-01.ps1`：把生产（`D:\01_olbr_portal`）的三份「全局 UI 配置」
  （`ui_tool_visibility.json` / `ui_tool_order.json` / `ui_menu_layout.json`）同步到本副本。
  默认试运行，`-Apply` 才写；**写之前先校验**：从本副本自己的代码取真相源
  （`ui_tool_order.py` 的 `VALID_TOOL_KEYS`、`App.tsx` 的 `VALID_PAGES`），未知 key
  整批拒绝（前端对未知页面键是静默丢弃，坏了不报错，只会让页面从侧栏消失）；覆盖走
  `.tmp` + `Move-Item` 原子替换（直接 `Copy-Item` 覆盖时，读方可能读到半截）；旧文件留
  `*.bak_<时间>_from01`（每份只留最近 3 份），运行记录写 `TMP\sync-ui-config.log`；
  目标推导出落在生产目录下时直接拒绝执行。
- `ensure_portal.ps1`：门户保活（计划任务调用）
- `ops/smoke/smoke.ps1`：真正的检查引擎（§8.1–§8.4 + 离线 pytest，PR #26–#43
  已反复使用）。`smoke-local.ps1` 只是薄壳：UTF-8 BOM 让 PS 5.1 能解析、
  `TMP\smoke.lock` 防并发、校验 HEAD == `-HeadSha`、写
  `TMP\smoke-pr-<PR>-<sha7>.md`（干跑写 `.dryrun.md`，不覆盖真跑报告）。
- `smoke-watcher.ps1`：每 60 秒轮询 GitHub，CI 变绿且无 `smoke-passed` 的
  open PR 就地冒烟、贴评论、通过才打标签；贴前重读 PR，head 变了就作废。
  **冒烟前 `git fetch` 并 `checkout --detach` 到该 PR head，finally 切回原分支**
  （Grok 当年的 checkout 由它接管）；工作树有未提交的已跟踪改动时跳过该 PR，
  不覆盖他人在途工作。Token 依次取 `SMOKE_GITHUB_TOKEN`/`GITHUB_TOKEN`/
  `GH_TOKEN`，都没有时回落本机 `gh auth token`（只在内存，不落盘）。
  开头有硬守卫：推导出的根目录落在 `D:\01_olbr_portal` 下直接拒绝执行（生产只读）。
- `install-smoke-watcher.ps1`：把 watcher 注册成常驻计划任务 `OLBR_SmokeWatcher`
  （登录自启、崩溃每分钟重启、单实例）；`-Status` 查、`-Uninstall` 卸。
  日志 `TMP\smoke-watcher.log`。
- **2026-11-14 已与 Grok 对齐**：PR #44 同 head 下本地链路 18/18 逐项一致
  （见 `TMP\smoke-pr-44-vs-grok.md`）。对齐后方可停 Grok；停之前不要动
  agent-orchestrator 的 webhook。
- `bundle_data.ps1`：把生产 PostgreSQL 快照灌进副本便携实例
- `my_portal_ctl.ps1`、`write_nginx_conf.ps1`：§11-5 自仓库根归位至此；
  两者都按自身位置向上推导仓库根，`ops/shortcuts/` 的 bat 已指向新位置

## 已停用：从 RPA（主仓库）来的同步

- `sync-engine.mjs` 和 `sync-policy.mjs` **已停用**，只保留文件备查。
- `sync_from_main.ps1`（同步引擎的薄壳）和 `bundle_data.ps1`
  同样属于已停用的同步链：它们的默认源仍指向旧编辑区
  `D:\01_RPA`，**禁止对这个新目录跑从 RPA 来的同步**。
- 本仓库不再从 `D:\01_RPA` 同步；根 `sync-now.bat` 已随批 C 删除。
  如需取数 / 取码，改走主仓库的正常开发流程，不要在这里恢复同步入口。

## 已停用：生产目录里的初版同步脚本

- `D:\01_olbr_portal\scripts\sync-runtime-config-to-02.ps1`（2026-11-16 初版）**已被本目录的
  `sync-ui-config-from-01.ps1` 取代**：初版只有 `Copy-Item`，没有校验、没有备份、覆盖非原子。
  生产机上那份留在原处容易跑错，**请删除**；以后只用本目录这份。

## 路径约定

- 所有脚本都用自身位置推导仓库根（`ops/deploy` 的上一级的上一级），
  不写死盘符；`start_all.ps1` / `stop_all.ps1` 要求副本带
  `runtime\`（python312 / node / postgresql16）与 `ISOLATED_COPY.flag`。
- 不要把 `apps/my_app/start_hidden.ps1` 之类的生产启动脚本搬到这里；
  隔离启动只走 `ops/deploy/start_all.ps1`。
