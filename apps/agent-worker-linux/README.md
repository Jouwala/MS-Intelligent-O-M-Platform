# apps/agent-worker-linux —— Linux 沙箱 Worker（GitHub ref-CAS 权威层）

面向三台 Linux 沙箱 VM（`muse-1-linux` / `muse-2-linux` / `muse-3-linux`）的常驻
Worker 新包，替换 `~/workspace/agent-worker-linux` 原型（原型用评论/标签认领，
非原子）。本包**不复制**协议实现：认领/租约/fencing/范围锁/完成幂等全部复用
仓库已合入的 GitHub 原生权威层 `apps/control-plane/backend/scheduler/`（ADR-7）。

> Issue #73 验收口径：两台 Worker 并发认领同一任务只能一个赢家（ref-CAS
> 201/422），另一台 `LeaseHeld`；三台 instance 各不相同；GitHub 上可见
> `agent-hwm/issue-N/...` 与 `agent-lease/issue-N/...` ref。

## 一、依赖与包结构

- 目标机：Linux + Python 3.10+（**仅标准库**，无第三方依赖）；
- 必须有一份**完整仓库 clone**（本包在 `<repo>/apps/agent-worker-linux/`）：
  - `apps/control-plane/backend/scheduler/factory.py` → `make_scheduler` →
    `GitHubRefScheduler`（ref-CAS 权威层）；
  - `apps/control-plane/backend/adapters/github_issue.py` →
    `RestGitHubTransport`（评论/标签/Issue 分页）、`parse_file_scope`；
  - `apps/agent-worker/` → `executor`（POSIX 进程组/超时/取消/#78 环境白名单）、
    `command_parse`、`heartbeat`、`cancellation`、`hostinfo`（经 `_bootstrap` 加载）；
- 本包文件：

| 文件 | 职责 |
| --- | --- |
| `linux_config.py` | JSON 配置加载/校验/别名兼容/认证解析（token 不打印） |
| `runtime_loader.py` | 从 `repo_root` 按路径加载共享模块（不复制实现） |
| `linux_worker_core.py` | 编排：发现 → claim → transition → scope → 执行 → complete/失败/取消 → 清理 |
| `worker_linux.py` | CLI：常驻 / `--once` / `--check` / `--claim-probe` |
| `verify_concurrent_claim.py` | 并发认领验收：恰好一赢一 `LeaseHeld`；`--cleanup-winner` 收尾 |
| `config.example.json` | 三台 VM 配置示例（JSON 数组；实际 `config.json` 是其中一条） |
| `install.sh` / `agent-worker-linux.service` | systemd 部署（幂等安装，打印回滚） |
| `tests/` | 离线 pytest（fake GitHub HTTP server，不访问真实 GitHub） |

## 二、逐 VM 配置（JSON 字段）

`config.example.json` 是**三台示例组成的数组**；每台机器把其中对应的一条保存为
`/etc/agent-worker-linux/config.json`（`json` 不支持注释，字段说明看下表）。

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `worker_id` | 是 | — | 逻辑身份，用于 `agent:<worker_id>` 标签与 ref 名 agent 段；只允许小写字母/数字/连字符。兼容旧名 `agent` / `agent_id` |
| `instance` | 否 | `worker_id` | **每台 VM 必须唯一**；禁止空值、禁止原型撞车的 `htch-runtime`。兼容旧名 `instance_id` |
| `repo_root` | 是 | `NOC_SOC_REPO_ROOT` | 仓库 clone 绝对路径；必须包含 scheduler/factory.py |
| `repo` | 否 | 从 `git remote origin` 推导 | `owner/name`；推导失败时须显式设置 |
| `api_base` | 否 | `https://api.github.com` | 指向本地 surrogate 网关时填网关地址 |
| `token` | 否 | — | 显式 token（优先级最高；不建议写进文件） |
| `token_env` | 否 | `GITHUB_TOKEN` | 从环境变量取 token |
| `token_file` | 否 | — | 从文件读 token（首行）；读后不落盘、不打印 |
| `agent_label` | 否 | `agent:<worker_id>` | 任务发现标签（与 dispatch 约定一致） |
| `poll_seconds` | 否 | `30` | 轮询间隔 |
| `lease_ttl_seconds` | 否 | `600` | 任务租约 TTL |
| `renew_interval_seconds` | 否 | `150` | 执行期续租间隔（必须 < TTL） |
| `executor_timeout_seconds` | 否 | `1800` | 单任务执行超时 |
| `workdir` | 否 | `~/workspace` | 任务执行目录（`per_task_dir=false` 时共用） |
| `per_task_dir` | 否 | `false` | `true` 时每任务独立目录 `<state_dir>/tasks/issue-N` |
| `state_dir` | 否 | `<workdir>/agent-worker-linux-state` | 心跳/日志/每任务目录的根 |
| `log_file` | 否 | — | 追加日志文件（人类日志同时写 stderr） |
| `registry_issue` | 否 | `null` | Worker 注册表 Issue；不填则注册表仅本地展示 |

**instance 与 ref 名的关系**：`scheduler/ref_api.py` 的 `lease_ref` 要求 instance
段是十六进制，而人类可读的 `instance`（如 `muse-1-linux`）不满足。Worker 因此把
`instance` 稳定映射为 `ref_instance = sha256(instance)[:16]`（同机同值、不同
instance 不同 ref）；`--check` / `--claim-probe` 的 JSON 会同时给出两者。
`worker_id` 必须满足 ref 名的 agent 约束（小写 `[a-z0-9][a-z0-9-]*`）。

## 三、部署前提

1. 三台 VM 各自已有仓库 clone，并切到要运行的提交（`git fetch && git checkout <sha>`）；
2. 每台 VM 有 GitHub PAT：**Contents + Issues 读写**（refs 需要 Contents 写权限）；
   或配置 `api_base` 指向本地 surrogate 网关（可空 token；`urllib` 默认走
   `HTTP(S)_PROXY` 环境变量，surrogate 在代理后也适用）；
3. 任务执行环境变量由 `apps/agent-worker/env_filter.py` 白名单过滤（#78），
   Worker 自己的 Secret 不会传入任务命令。

## 四、安装（每台 VM，逐台执行）

```bash
# 1) 取包（与仓库同一提交）
cd <repo>/apps/agent-worker-linux

# 2) 首次安装：生成配置并装 systemd 单元（默认会启动）
sudo ./install.sh
#    首次会提示改 instance；请编辑 /etc/agent-worker-linux/config.json
sudo editor /etc/agent-worker-linux/config.json

# 3) 配置自检（不启动服务）：refs API 连通性 + resolved identity/auth 模式
python3 worker_linux.py --config /etc/agent-worker-linux/config.json --check

# 4) 重启生效
sudo systemctl restart agent-worker-linux
journalctl -u agent-worker-linux -n 50 --no-pager
```

`install.sh` 可重复执行（幂等）：已有单元会先备份为
`/etc/systemd/system/agent-worker-linux.service.bak.<时间戳>`，再原子替换并
`daemon-reload` + `enable` + `restart`。`--no-start` 只安装不启动；
`--config PATH` 可指定配置文件位置。

## 五、迁移（从原型切到新包）

1. **备份**：原型目录 `~/workspace/agent-worker-linux`、原型配置/环境变量/旧
   systemd 单元；本包安装过程**不删除、不修改**原型目录，可随时切回；
2. 停旧服务（按原型实际单元名，例如 `sudo systemctl disable --now <原型服务>`）；
3. 按第四节安装新包，逐台确认 `instance` 三者互不相同；
4. `--check` 通过后 `--once` 实跑一次：`python3 worker_linux.py --config ... --once`
   （stdout 输出单行 JSON，如 `{"outcome":"idle"}` / `{"outcome":"completed",...}`）；
5. 旧服务保留（只 disable），观察一轮后再删除旧目录；回滚步骤见第八节。

## 六、验收（多 VM 门槛）

**A. 并发认领（在任意一台 VM 上，用两份不同 instance 的配置）**

```bash
python3 verify_concurrent_claim.py --issue <N> \
  --config-a ~/cfg-muse-1.json --config-b ~/cfg-muse-2.json
# 期望 stdout 结尾：[verify] PASS：恰好一赢一 LeaseHeld；并打印赢家的
#   agent-hwm/issue-<N>/00000001 与 agent-lease/... ref 名

# 验收完清理赢家租约（避免占用任务）：
python3 verify_concurrent_claim.py --cleanup-winner /tmp/claim-verify-xxx/a.json
```

也可以手工单测一台：

```bash
python3 worker_linux.py --config ~/cfg-muse-1.json --claim-probe --issue <N> \
  --result-file /tmp/probe-a.json
# stdout 单行 JSON：{ok,result,worker_id,instance,attempt,token,hwm_refs,
#                    lease_refs,refs_error,ref_instance,issue,config_path,...}
```

**B. GitHub 上确认 ref**：仓库 → Branches 或 `git ls-remote origin 'refs/heads/agent-hwm/*'`，
应看到 `agent-hwm/issue-<N>/00000001`；认领期间还有一条 `agent-lease/issue-<N>/...`；
任务完成后 lease ref 被删除，`state:*` 标签由权威层收敛（in-review / blocked / todo）。

**C. 无重复执行**：并发后检查 Issue 只有一条 `TASK_COMPLETED` 评论（机器头
`<!-- ref-completion-id: ... -->`），输家只有日志 `LeaseHeld`。

## 七、安全

- **token 不入库、不落盘、不打印**：配置里可只写 `token_env`/`token_file`；
  所有日志与 `--check`/`--claim-probe` 输出只含 `auth_mode`（如
  `env:GITHUB_TOKEN`），不含 token 值；`install.sh` 生成的配置文件权限 `600`；
- 任务命令环境白名单由仓库 `apps/agent-worker/env_filter.py`（#78）保证，
  Worker 自身环境变量（PAT、代理口令等）不会进入任务子进程；
- 任务命令以 argv 列表执行、永不 `shell=True`（`command_parse` + `executor`）；
- systemd 单元启用 `NoNewPrivileges=true`、`PrivateTmp=true`，并把可写路径
  限制到 `state_dir` 与 `workdir`。

## 八、故障排查 / 回滚

| 症状 | 排查 |
| --- | --- |
| 启动即退出 | `journalctl -u agent-worker-linux -n 100`；`--check` 看 `[config-error]` |
| `--check` 失败 | `error` 字段；确认 `repo`/`api_base`/PAT Contents 权限/网络代理 |
| 一直是 idle | Issue 是否有 `agent:<worker_id>` + `state:todo` 标签；`--claim-probe` 看 `LeaseHeld`（别人持有）|
| 认领成功但不执行 | 日志 `scope conflict`（范围锁被占）或 `no command section`（缺「执行命令」节）|
| 反复 LeaseHeld | 上一轮赢家失联；等租约 TTL 过期或由 Reaper 回收后重试 |
| 任务失败 | 日志 `executor-timeout` / `execution-failed` / `process-tree-leaked`；GitHub 标签 `state:blocked` |

**回滚**：

```bash
sudo systemctl disable --now agent-worker-linux
sudo rm -f /etc/systemd/system/agent-worker-linux.service
sudo systemctl daemon-reload
# 恢复原型服务（旧目录与旧配置都在，未被执行 install.sh 修改）
sudo systemctl enable --now <原型服务名>
```

## 九、离线自测

```bash
python -m pytest apps/agent-worker-linux/tests -q
```

测试用 `apps/control-plane/backend/tests/fake_github_http.py` 的本地 fake GitHub
（真实 TCP + 服务端 ref 唯一性裁决，不访问真实 GitHub）；覆盖配置校验/别名/
保留 instance、并发认领恰好一赢家、no-command→blocked、成功命令→完成评论+
`state:in-review`+lease ref 删除、`--claim-probe`/`--check` CLI JSON 与 refs。
