# 任务 #111：冒烟 pytest 基线：NUR 数据漂移 + 警告文案用例状态依赖

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/111
- 分支：`agent/task-111-pytest-baseline`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

## 任务说明
D:\02 冒烟 pytest 出现 3 项与 PR 改动无关的失败（PR #104、#106 均命中，3 failed / 939 passed / 8 skipped），阻塞纯文档 PR 合入。Muse 4 在 #108 调查结论（2026-11-16 11:40）：

1. **NUR 2 项稳定失败**（test_nur_exclude_semantics ×2）：两者同时失败与文件头 2026-10-06 快照（名单 46 / 全量 1107）算术上不相容，指向 D:\02 数据漂移（tt_exclude 名单或 eflow_tt 数据已变），而非代码回归。近期相关文件只有移动/重组/日志改造，无语义改动。
2. **警告文案用例状态依赖**（test_warning_text_distinguishes_failure_from_empty）：同一提交 10 分钟后重跑即通过；机理是 load_master_sites 成功时清除 master 错误登记（site_availability_service.py:419），结果取决于进程内缓存 warming 状态，属状态依赖型脆弱用例。

任务：核实并修复——（a）确认 D:\02 数据漂移根因（tt_exclude / eflow_tt），更新基线或数据；（b）消除警告文案用例的状态依赖（或改为确定性断言）；（c）更新冒烟 pytest 基线，使纯文档 PR 不再被无关失败阻塞。

未核实项：逐断言实际值只在 D:\02 本机 TMP\smoke_pytest_106_d5c3ff2.out，GitHub 上读不到，需在 D:\02 上取日志。

## 负责人
agent:muse-3

## 优先级
P0（阻塞 PR 合入与试运行冒烟）

## 文件修改范围
tests/test_nur_exclude_semantics.py、tests/test_swallowed_error_guards.py、相关产品模块与冒烟基线；不改业务逻辑口径。

## 依赖任务
无。

## 验收标准
- D:\02 冒烟 pytest 在 main 上全过（或失败项均有明确的已知问题记录）
- 纯文档 PR 不再被无关 pytest 失败阻塞
- PR 通过 CI + 冒烟，Muse 1 审核

## 所属分支
agent/task-111-pytest-baseline


## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
