# 任务 #146：【ZIP交付 Phase 1】Muse 4：双平台 ZIP 构建、打包与 Release 自动化

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/146
- 分支：`agent/task-146-zip-phase1`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

## 任务说明

实施 ZIP 交付 Phase 0 报告（PR #106，`docs/workspace/zip-delivery-phase0-review.md`）§6.8 的 P1-1 至 P1-8，产出可重复构建的 Windows x64 与 Linux x64 ZIP 绿色部署包。Phase 0 报告已由 Muse 1 审核通过；#106 合入只差 D:\02 冒烟失败记录清除（环境原因，与报告内容无关），不阻塞本任务开工。Muse 5 的 Phase 0（#100）已合入。

## 负责人

负责人：agent:muse-4

## 优先级

优先级：P0

## 文件修改范围

- `ops/deploy/`（构建/打包脚本、Linux 启动/停止 `.sh` 编排器、首次启动脚本）
- `.github/workflows/release-build.yml`、`runtime-build.yml`（新建，或等价工作流）
- 启动脚本（P1-6 外部 config/data/logs 改造；P1-7 的 `db/app.db` 等路径环境变量覆盖如需改业务代码，先在评论里说明并等 Muse 1 确认）
- `docs/workspace/`（构建/发布相关文档）
- 不得改生产数据库、生产配置与 D:\01 相关文件；不得顺带改模板外的业务代码。

## 依赖任务

依赖：无（Phase 0 报告已由 Muse 1 审核通过，#99/#106 的合入是收尾手续，不阻塞开工）

## 验收标准

- [ ] P1-1：干净 PG 实测 initdb → createdb → alembic upgrade head，实测记录 + 首次启动脚本中的建 schema 步骤（如需）
- [ ] P1-2：称重表（4 项目 node_modules、venv/wheels、去 pgdata 后的运行时发行包）+ 是否拆包的决策
- [ ] P1-3：构建脚本（跨平台 Node/Python 实现：白名单组装 + 排除断言 + 凭据扫描 + 行尾/权限规范化 + build-info/SHA256 生成），在 windows-latest 与 ubuntu-latest 上跑通，产物可解压
- [ ] P1-4：Linux 启动/停止 `.sh` 编排器（对标 start_all.ps1 六步 + pid 文件 + 熔断），在 Linux 目标机上跑通隔离启动/停止全流程
- [ ] P1-5：release-build.yml + runtime-build.yml，打标签后 Release 出现双平台产物 + `.sha256`
- [ ] P1-6：启动脚本全面改用外部 con
## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
