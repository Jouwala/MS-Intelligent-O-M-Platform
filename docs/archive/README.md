# docs/archive — 历史档案

> 只存档，不作为现状真相源。**不要把现状写回本目录的任何文件。**
> 现状以各子系统的 `docs/manual/` 与 `README.md` 为准。

## 目录内容

| 文件 | 是什么 | 什么时候读它 |
| --- | --- | --- |
| `PROJECT_DOCS.md` | 11 份旧文档合并稿，保留全部原始内容。§1 项目说明 / §7 MS SLA 任务 / §9 巡检约定已于原文并入手册，原文在此存根 | 需要追溯**历史决策、评估、争议与复盘**时 |
| `PROJECT_DOCS_ARCHIVE.md` | `PROJECT_DOCS.md` 已迁走部分的原文副本 | 同上 |
| `MS-Intelligent-O-M-Platform 副本仓库重构执行方案.md` | 2026-11-12/13 写给**旧 MS 副本仓库**（`Jouwala/MS-Intelligent-O-M-Platform`，运行目录 `D:\01_olbr_portal`）的第一轮重构方案，9 个阶段（阶段 0~8） | 需要追溯**当初为什么这么拆、哪些阶段做了、哪些没做**时 |

## 关于最后这份重构方案（2026-11-17 归档）

本仓库（`noc-soc-intelligent-platform`，工作区 `D:\02_olbr_portal`）是旧 MS 副本仓库的
**后继**，方案里的路径、端口与阶段划分**与本仓库不对应**，不要照着执行。文件头部已加
状态说明头，逐条列出方案各阶段在本仓库的落地情况与差异。

本仓库对应的现状文档是：

| 关心什么 | 看哪里 |
| --- | --- |
| 目录边界与归属 | `docs/architecture/module-map.md`、`docs/adr/0001-directory-boundaries.md` |
| 编辑区 / 运行副本隔离、生产边界 | `docs/adr/0002-copy-isolation.md` |
| 数据源读写归属 | `docs/architecture/data-source-registry.md` |
| 现状长文档 | `docs/workspace/`（8 份）、各子系统 `docs/manual/` |

## 往本目录放东西的规矩

1. 放进来的是**历史材料**，不是现状描述。现状写错了要改的是 `docs/manual/` /
   `docs/workspace/`，不是这里。
2. 文件头必须写清**它写于什么时候、针对哪个范围、与现状的差异**，让后来人一眼知道
   能不能照着做。
3. 不在归档区新建「现状索引」类文档 —— 那会变成第二个真相源。
4. 归档后不再维护内容；发现其中的事实与现状冲突时，在文件头的说明里补一条差异，
   不要改正文。
