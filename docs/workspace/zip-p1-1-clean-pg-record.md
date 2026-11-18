# P1-1 实测记录：空库 `alembic upgrade head` 与 `app` schema 归属

> 关闭 Phase 0 报告附录 B 未证明项第 1 项。实测日期 2026-11-18，Muse 4。

## 1. 待证问题

迁移 `0005_tt_exclude_table` 的注释称「PG 侧由 search_path 落到 app」
（`apps/my_app/alembic/versions/0005_tt_exclude_table.py:33`），即默认
迁移建出的表应落在 `app` schema。但迁移本身不带 schema 前缀，
`alembic/env.py` 不建 schema、也不设置 search_path（`alembic/env.py:31`
只 import 业务模块并解析 URL）。空库直接 `upgrade head` 是否成功、
表落到哪个 schema，此前未实测。

## 2. 实测环境

| 项目 | 值 |
| --- | --- |
| PostgreSQL | 16.15（Ubuntu 16.15-0ubuntu0.24.04.1，apt 安装） |
| 集群 | 全新 `initdb -E UTF8 --auth=trust`，临时目录，端口 15433 |
| alembic / SQLAlchemy / psycopg2-binary | 1.19.0 / 2.0.51 / 2.9.12（与 `requirements.lock.txt` 一致） |
| 迁移链 | 0001 → 0002 → 5415704359dc → 0003 → 0004 → 0005 → 0006，共 7 个版本 |
| 基线 | 新建空库只有 `public` schema；默认 `search_path = "$user", public` |

## 3. 实验一：空库默认设置直接 upgrade head

```sh
createdb myapp
MY_APP_DB_MODE=pg DATABASE_URL='postgresql://postgres@/myapp?host=/tmp/p11&port=15433' \
  alembic upgrade head
```

结果：**退出码 0，7 个迁移全部成功**。但：

```
 List of schemas
  Name  | Owner
--------+-------
 public | pg_database_owner     # 只有 public，没有 app

 table_schema | count
--------------+-------
 public       |    34           # 34 张表全部落在 public

 schemaname | tablename
------------+-----------------
 public     | alembic_version  # 版本表也在 public
```

结论：0005 注释的假设在空库上**不成立**。迁移不建 schema，
默认 search_path 下表全部落到 `public`，与「落到 app」的设计不符。
运行时代码每次连接都 `SET search_path TO app,eflow,…,public`
（`backend/database.py:394,400`），末尾的 `public` 会让它在无阴影时
仍找到这些表——属于碰巧能用，不是设计。

## 4. 实验二：先建 schema 再 upgrade head

```sh
createdb myapp2
psql -d myapp2 -c 'CREATE SCHEMA IF NOT EXISTS app;'
psql -d myapp2 -c 'ALTER DATABASE myapp2 SET search_path TO app, public;'
MY_APP_DB_MODE=pg DATABASE_URL='postgresql://postgres@/myapp2?host=/tmp/p11&port=15433' \
  alembic upgrade head
```

结果：**退出码 0**，34 张表与 `alembic_version` **全部落在 `app`**。
另以运行时 search_path（`app,eflow,…,public`）无前缀查询 `task_queue`
成功返回（0 行），确认落位正确。注意 `ALTER DATABASE … SET` 只对
**新会话**生效，alembic 的新连接能取到；同一 psql 会话内 `SHOW`
仍显示旧值，属正常现象。

两条语句各跑两遍均为退出码 0（第二遍仅一条 `schema "app" already
exists, skipping` 提示），确认幂等。

## 5. 落地：首次启动建 schema 步骤

已加入两个启动脚本的第 1 步（PG 初始化段），放在建库之后、每次启动都跑：

- Windows：`ops/deploy/start_all.ps1:435-438`（`CREATE SCHEMA` +
  `ALTER DATABASE … SET search_path`，查 `$LASTEXITCODE` 失败即抛；
  该段 `$ErrorActionPreference` 为 Continue，不查退出码会漏报）
- Linux：`ops/deploy/start.sh:396-397`（同款两条语句，失败即 `die`）

已有表不搬家：此前已在 `public` 建好表的库，加上这两条语句后，
后续迁移落到 `app`，存量表仍留在 `public`；是否迁移存量表不在本项范围。

## 6. 附带发现

1. **没有任何脚本跑 alembic**：全仓库 grep 不到 `alembic upgrade` 的
   脚本调用，只有文档提及。绿包首次启动只建空库，不建表；操作员须
   手动跑 `alembic upgrade head`（或用 `bundle_data.ps1` 灌快照）。
   是否由首次启动包装器代跑，需 Muse 1 定。
   【已关闭 2026-11-18】Muse 1 于 17:20Z 决定 D1：由启动包装器代跑；
   本轮已在 `start_all.ps1` 与 `start.sh` 落地（PG 步骤后、nginx 配置
   前，每次启动跑 `upgrade head`，幂等，失败即停）。
2. **`MY_APP_DB_OVERRIDE` 不能单独用**：`env.py` 文档称它可「指向测试
   PG」，但 `backend/database.py:76` 在 import 时就建引擎，pg 模式下
   要求 `DATABASE_URL` 已设置；只设 override 会在 import 处抛
   `DB_MODE=pg 但环境变量 DATABASE_URL 未设置`。实测改用 `DATABASE_URL`
   直指测试库。修法是引擎懒建，属业务代码改动，需 Muse 1 确认。
3. **其余 schema 自建**：`eflow` 等 schema 由搬运流程与服务在首次使用时
   `CREATE SCHEMA IF NOT EXISTS`（`backend/migrations/transfer_lib.py:544`、
   `backend/services/RANElementManagement/ran_element_management_service.py:144`、
   `backend/services/ms/ms_sla_dashboard.py:113`），不在首次启动步骤内；
   首次启动步骤只负责 alembic 目标 `app`。

## 7. 未验证

- Windows 便携 PG 上的真实启动路径（`start_all.ps1` 新语句只做了语法解析，
  未在 Windows 上实跑；待 P1-8）。
- `start.sh` 新语句只做了 `sh -n` 与 SQL 实测，未走完整夹具重跑；待 P1-8。
