# 任务 #121：【P0】修 3 处生产定时任务导入错误 + paramiko 补进 lock（依赖审计）

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/121
- 分支：`agent/task-121-import-fixes`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

## 任务说明
依赖深度审计（main @ f442fd0）确认 3 处导入错误会在切生产后触发，全部命中定时任务；另有 1 处清单缺失。修完前切生产有风险。

## 负责人
agent:muse-2

## 优先级
P0（切生产前必须修）

## 改动
1. `apps/my_app/backend/services/site/site_outage_session.py:208`：`from _playwright_utils import safe_goto` → `from backend.services._playwright_utils import safe_goto`（与同目录 `site_outage_service.py:23` 一致）。影响任务 `site_outage_screenshot`（cron 0 1 * * *）：会话复用路径抛 ImportError，截图失败。
2. `apps/my_app/backend/services/remote/remote_ftp.py:117`：`from .atomic_write import ...` + 裸导入回退双双失败 → `from backend.services.atomic_write import atomic_replace_file, AtomicReplaceFallback`。影响任务 `kpi_ran_availability`（RAN Availability 日报邮件）：FTP 下载步骤 ImportError，邮件发不出。
3. `apps/my_app/backend/services/sp/get_sp_table.py:426`：同上，改绝对导入 `from backend.services.atomic_write import atomic_replace_file`。影响任务 SP 数据同步：每张表都记 status:error。
4. `apps/my_app/requirements.lock.txt`：补 `paramiko`（requirements.txt:34 有，lock 无；新 venv 按 lock 建会缺 paramiko，SP 同步与 sp_dashboard 起不来）。从现网环境重新 freeze 最稳妥。

顺手（低风险）：`apps/whatsapp-kb-bot/package.json` dependencies 加 `pino`（src/notify.js:48、src/whatsapp.js:31 直接 import，未声明，靠 baileys 传递依赖）。

## 文件修改范围
上述 4 个文件（+ package.json）。不改业务逻辑、不改启动链。

## 验收标准
- 三处改绝对导入后，`site_outage_screenshot`、`kpi_ran_availability`、SP 数据同步在 D:\02 手动触发不再报 ImportError。
- lock 文件含 paramiko；`pip install -r requirements.lock.txt` 建出的环境能 `import paramiko`。
- CI 全绿 + 冒烟通过（3 个已知失败除外）。

## �
## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
