# 任务 #173：[修复] runtime-build dry_run win-x64 失败：jq 输出 CRLF 导致 NAME 带 \r，URL 查成 null

- Issue：https://github.com/Jouwala/noc-soc-intelligent-platform/issues/173
- 分支：`agent/task-173-runtime-build-jq-crlf`
- 状态：已认领（state:claimed），脚手架由 issue-to-pr 工作流自动生成

## 目标

## 任务说明
runtime-build 首次 dry_run（run #1，44518680440，2026-11-19 11:52，main 931c1fd）失败：win-x64 在“下载 + 验哈希 + 组装”步骤 exit 6，无 artifact；linux-x64 因矩阵 fail-fast 在 nginx 编译步被取消（其下载步已通过）。

## 根因（已定位，有日志证据）
win-x64 日志（job 135541318455）：
```
下载 nginx
：null
curl: (6) Could not resolve host: null
```
同一 echo 在 linux job（135541318007）是一行带真 URL：`下载 node：https://nodejs.org/dist/...`。

机制：Windows runner 的 jq（Chocolatey）输出带 CRLF；Git Bash 里 `for NAME in $(jq -r .platforms[$p] | keys[])` 做词拆分时 `\r` 不在 IFS 里，于是 NAME 变成 `nginx\r`，随后所有 `--arg n "$NAME"` 的 jq 查询都查不到键，`.url` 返回 JSON null，`jq -r` 印出字面 `null`，curl 拿 `null` 当 host，exit 6。清单本身无错（main 931c1fd 的 runtime-manifest.json 里 win-x64 nginx url 是真地址，清单完整性门禁也通过了）。

## 负责人
agent:muse-4

## 优先级
P0

## 文件修改范围
- `.github/workflows/runtime-build.yml`：下载循环及所有 jq 取值处去掉 `\r`，例如 `$(jq -r ... | tr -d 
)`，覆盖 keys 列表、URL、SHA、DIR、STRIP；`version=$(jq -r .version ...)` 的 GITHUB_OUTPUT 也同样处理（否则版本号带 `\r`，后续打包命名会被污染）。门禁步的 MISSING 取值顺带处理。
- 不要改清单内容，不要改下载源。

## 依赖任务
无

## 验收标准
- 在修复分支上 dispatch runtime-build（dry_run=true，tag 留空），win-x64 与 linux-x64 两个任务都跑到 upload-artifact，产出两个平台的 artifact，结论 success。
- 修复只动工作流的取值/清洗逻辑，不改构建产物布局。
- 走 PR 到 main，CI 绿。

## 所属分支
agent/task-173-runtime-build-jq-crlf


## 已完成

- [x] 认领任务、建分支与草稿 PR（自动化）

## 未完成

- [ ] 按 Issue 验收标准实现
- [ ] agent-done-check 自检并把命令与结果贴到 Issue 评论

## 下一步

在本分支提交代码，草稿 PR 转正式后按 agent-protocol 发 TASK_COMPLETED / PR_CREATED 评论。
