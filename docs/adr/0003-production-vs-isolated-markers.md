# ADR 0003：角色标记（生产树 / 隔离副本）

- 状态：已接受
- 日期：2026-11-19
- 范围：`D:\02_olbr_portal` 的启停入口、质量检查与 git 路径钩子
- 取代：ADR 0002 里「02 永远是隔离副本」的前提（副本隔离本身仍适用于打包/冒烟副本）

## 背景

2026-11-13 定的规则是「02 = 隔离副本，生产在 01」（ADR 0002）。2026-11-18/19 切生产后，
`D:\02_olbr_portal` 接管了 80/443/7038/7090/9267/9268 与系统 PG 5432，01 退为回退目标。
但一批脚本仍按 11-13 的判断写：

- 根 `start.bat` / `stop.bat` 直接调 `start_all.ps1` / `stop_all.ps1`，而 `start_all.ps1`
  是**隔离启动器**：它会改 `apps\my_app\backend\.env`、起便携 PG 55432、往 8080/8443 起第二套；
- `stop_all.ps1` 会 `nginx -s quit`，但它跟踪的是 `TMP\portable-*.pid`——在生产树上等于
  「关掉线上入口、后端却还在跑」；
- 根目录同时存在 `ISOLATED_COPY.flag`（11-13）与 `PRODUCTION.flag`（11-19）：前者让
  `my_portal_ctl.ps1` 拒绝一切启停（于是 `ops/shortcuts` 的 1/2 号点了只打印 BLOCKED），
  后者只被 `post_sync_fix_paths.ps1` 用来决定 nginx 看门狗端口；
- `ops/quality/check_all.ps1` 第 1 步要求 `ISOLATED_COPY.flag` 存在，第 2 步断言 `.env`
  不是生产端口——两条都只在「隔离副本」这个前提成立时才有意义。

结果：同一套脚本在两种角色下互相当成对方的守卫，谁都不肯跑，或者按错误的角色跑。

## 决策

1. **根目录的两个标记互斥，且是唯一角色真相源。**

   | 标记 | 只出现在 | 端口 | 谁写 |
   | --- | --- | --- | --- |
   | `PRODUCTION.flag` | 生产树（当前 `D:\02_olbr_portal`） | 80/443/7038/7090/9267/9268，PG 5432 | 切生产时人工放 |
   | `ISOLATED_COPY.flag` | 便携副本 / 冒烟 runner | 8080/8443/7039/7091/9277，便携 PG 55432 | `provision-smoke-runner.ps1`、打包脚本 |

   两个都没有或两个都有 = 角色不明，**一律拒绝执行**，不猜、不降级。

2. **入口按标记分流，脚本各自再守一道。**
   - 根 `start.bat` / `stop.bat`：有 `PRODUCTION.flag` → `portal-ctl.ps1 -Action start|stop`；
     否则 → `start_all.ps1` / `stop_all.ps1`（保留，供副本使用）。
   - `start_all.ps1`：看到 `PRODUCTION.flag` 直接 throw（非 `-Production` 分支）；
   - `stop_all.ps1`：看到 `PRODUCTION.flag` 直接 throw；
   - `my_portal_ctl.ps1`：两种角色下都只允许 `0-status`（01_RPA 时代的老控制器不再驱动启停）；
   - `ops/quality/check_all.ps1`：步骤 1 接受二选一，步骤 2 只在隔离副本上跑（生产树打印
     SKIP + 原因）；
   - `post_sync_fix_paths.ps1`（git 钩子）：`PRODUCTION.flag` 存在时，nginx 看门狗端口写 80，
     不再按 `.env` 写回 8080。

3. **生产端口只有一个真相源**：`ops\deploy\portal-ctl.ps1` 的 `$Ports`
   （80/443/7038/7090/9267/9268）。根 `.env` 仍是隔离端口，**不是**生产配置的来源；
   `write_nginx_conf.ps1` 只用于副本/打包，生产上不要不带端口参数地跑它。

## 后果

- 正面：双击根 `start.bat` / `stop.bat`、或在生产树上误跑 `start_all.ps1` / `stop_all.ps1`，
  都会**明确报错**而不是悄悄起第二套 / 只关掉入口；`check_all.ps1` 在生产树上也能正常跑
  （除 .env 端口断言外）。
- 负面：生产树上直接用 `start_all.ps1` 的旧习惯不再工作，必须改走 `portal-ctl.ps1`；
  打包/冒烟副本要记得带 `ISOLATED_COPY.flag`（`provision-smoke-runner.ps1` 已自动写）。
- 校验点：
  - `start_all.ps1`（无参数）在生产树 → throw；
  - `stop_all.ps1` → throw；
  - `start.bat` → `[production]` + `portal-ctl.ps1 -Action start`；
  - `check_all.ps1` → 步骤 1 PASS（PRODUCTION.flag），步骤 2 SKIP（带原因）。

## 相关

- ADR 0002（副本隔离，前提已被本 ADR 取代）；`ops\deploy\README.md`「角色标记」一节；
  根 `AGENTS.md` 第二节 ⭐ 表；`docs\workspace\cutover-2026-11-20.md`。
