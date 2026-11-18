# P1-2 实测记录：称重表、分包决策与运行时清单核实

> 关闭 Phase 0 报告 §6.8 P1-2 的实测部分。实测日期 2026-11-18，Muse 4。
> 称重命令与 `.github/workflows/release-build.yml` 完全一致（`npm ci`、
> `npm run build`、`pip download -r requirements.lock.txt`），在 Muse VM
>（Ubuntu 24.04，Node 24.22.1，Python 3.12.3）上跑。

## 1. 称重表（应用 ZIP 组件）

| 组件 | 体积 | 文件数 | 备注 |
| --- | --- | --- | --- |
| my_app 前端 node_modules | 280M | 25,813 | `npm ci` exit 0 |
| 门户 node_modules | 137M | 8,516 | `npm ci` exit 0 |
| whatsapp-kb-bot node_modules | 72M | 2,167 | `npm ci` exit 0 |
| whatsapp_analysis node_modules | 39M | 1,094 | `npm ci` exit 0 |
| **node_modules 合计** | **528M** | **37,590** | |
| my_app 构建产物 `_fe_dist` | 21M | 144 | `npm run build` exit 0 |
| 门户构建产物 `dist` | 1.7M | 22 | `npm run build` exit 0（含 i18n 守卫） |
| wheels/（lock 文件下载） | 183M | 89 | **全部是 .whl，0 个 sdist** |
| 源码树（其余白名单文件） | 约 17M | 约 1,800 | source 模式实测口径 |
| venv 安装后体积（仅参考，不进包） | 697M | — | 目标机磁盘占用参考 |

## 2. 全模式构建实测

用称重产物在隔离副本上跑 `build-zip.mjs --mode full --platform linux-x64`：

- **退出码 0**，产物 `noc-soc-platform-dev-weighing-linux-x64.zip`
- **370.4 MiB**（388,344,092 字节），**42,107 个条目**，`testzip` 干净
- SHA256：`33c80794500cf652bf1b3b5de2133760344508dbbcedebba70d52104c33d61f0`
- build-info 合法（commit 为副本的 6abb463，alembic_head 为 0006）

## 3. 分包决策：不分包

370 MiB 只有 GitHub Release 单文件 2 GiB 上限的约 18%，双 ZIP 模型
（应用 ZIP + 运行时 ZIP）已足够，**不再拆分应用 ZIP**。node_modules
占大头但它是运行必需品，拆出去只会多一份下载与校验负担。

## 4. 运行时清单核实状态

9 个 null 字段中已填 3 个，剩余 6 个全部是 Linux 侧（保持 null，
工作流 fail-closed，不会被绕过）：

| 条目 | 状态 | 核实方式 |
| --- | --- | --- |
| win-x64 nginx sha256 | ✅ 已填 `db8c7a52…` | PGP 验签通过（签名者 Sergey Kandaurov，指纹 `D6786CE303D9A9022998DC6CC8464D549AF75C0A`，公钥取自 Ubuntu 官方 nginx 1.28.0 源码包的 `debian/upstream/signing-key.asc` 独立交叉确认）；哈希由验签通过的同一下载字节算得 |
| win-x64 postgresql url + sha256 | ✅ 已填，版本 16.16 | EDB 官方 `.sha256` 文件取哈希，下载字节（378,078,504 字节）SHA256 一致；16.16 为 EDB 官方二进制页当前 16.x |
| win-x64 node / python | ✅ 早已填 | Node 由 SHASUMS256.txt，Python 由 MD5 交叉核对（P1-3/P1-5） |
| linux-x64 nginx | ⏸ 留空待实施 | **无官方二进制包**。决定：由 runtime-build.yml 从官方源码包编译（待实施）；源码包 SHA256 `c6b5c6b0…` 已 PGP 验签核实，记于清单 note |
| linux-x64 postgresql | ⏸ 留空待实施 | **决定用 PGDG 官方 APT 仓库**（用户 2026-11-18 定）：runtime-build.yml 的 Linux 作业下载 `postgresql-16` 的 .deb 并解包（待实施）；仓库元数据 GPG 签名，实施时从签名索引钉死 .deb 哈希；保持 null，工作流 fail-closed |
| linux-x64 python | ⏸ 留空待决策 | **python.org 无 Linux 二进制**。候选：python-build-standalone（astral-sh，第三方，需信任决策）/ 源码编译 / 系统包，见 §6 |

## 5. 称重中发现的两个问题（已修）

### 5.1 凭据扫描误报，full 模式构建失败

首次全模式构建退出码 2：凭据扫描命中 8 处，全在
`node_modules/dotenv/README*.md` 里的**示例**私钥块。扫描规则对
node_modules 仍查「私钥块」一条，导致第三方包文档误报。

修复（`ops/deploy/lib/zip-package.mjs` 的 `scanCredentials`）：
**node_modules 不再参与凭据扫描**——第三方包由 lockfile 复现，
文档示例不是项目泄漏；供应链安全靠 lockfile + npm audit。
dist/_fe_dist 仍按宽松规则扫描（自有代码的构建产物）。
新增回归测试（`zip-package.test.mjs`）：node_modules 内示例密钥
不误报、自有文件仍拦截。回归测试 9/9 通过。

### 5.2 EDB 的 PG zip 捆绑 pgAdmin，运行时虚胖 85%

PG zip 解压后 933M，其中 `pgAdmin 4/` 占 779M，`doc/` 16M，
`StackBuilder/` 0.75M——运行时都不需要。已在
`.github/workflows/runtime-build.yml` 增加裁剪步骤（仅 win-x64）：
删三者后断言 `initdb/pg_ctl/psql/createdb.exe` 仍在。实测裁剪后
PG 为 **137M**（bin 74M + lib 27M + share 25M + include 13M；
include 保留是为将来可能在运行时上编扩展留余地）。

## 6. Linux 侧三项待决策（对比表）

| 组件 | 方案 A（推荐） | 方案 B | 方案 C |
| --- | --- | --- | --- |
| Python | python-build-standalone（astral-sh，uv 同款可重定位发行版，glibc 2.17+，`x86_64-unknown-linux-gnu` + `install_only`）；需信任第三方构建，哈希在决策后钉死 | python.org 源码编译（官方源码，PGP 可验；CI 构建重，约数十分钟） | 目标机系统包（破坏自包含模型，离线目标机不可用） |
| PostgreSQL | ✅ **已定：PGDG 官方 APT 仓库**（用户 2026-11-18 定）：`postgresql-16` .deb，仓库元数据 GPG 签名；runtime-build.yml 的 Linux 作业下载并解包（待实施），实施时从签名后的 Packages 索引钉死 .deb SHA256 | 官方源码编译（备选：源码包 PGP 可验，CI 构建重） | 系统包 / 第三方 repack（不推荐：破坏自包含 / 非官方） |
| nginx | 官方源码编译（源码包哈希已验签；`--prefix` + `-p/-c` 运行时重定位，start.sh 已用 `-p/-c`；需 `--with-http_ssl_module` 及以上） | 系统包（破坏自包含；且版本不受控） | nginx.org .deb/.rpm 提取（发行版绑定） |

PostgreSQL 一项用户已定（PGDG，见上表）；Python 与 nginx 两项待 Muse 1 定；定后实施 runtime-build.yml 的 Linux 构建步骤，
届时再填清单——**填之前 Linux 运行时构建保持 fail-closed**。

## 7. 运行时 ZIP 体积估算

- Windows：Python 11M + Node 36M + PG 裁剪后约 137M（未压缩）+ nginx 2.1M，
  压缩后估计约 100–120M（待首次 runtime-build dispatch 实测）。
- Linux：三项来源未定，无法估算。

## 8. 未验证 / 待跑

- runtime-build dispatch（win-x64，dry_run）：清单已齐，可跑，待上传后触发。
- Linux 全模式构建：本轮只在 Linux 上跑了构建脚本，未在 Windows runner
  上验证（CI 矩阵覆盖）。
- 称重在单机上跑，`npm ci` 结果受 lockfile 钉死，可复现；门户构建
  4.19 秒、my_app 构建未单计时。
