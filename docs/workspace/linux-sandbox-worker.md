# Linux 沙箱 Worker（muse-1/2/3-linux）接入 GitHub ref-CAS

> 2026-11-15（Issue #73）。适用：三台 Linux 沙箱 VM 上的常驻 Worker。
> 包位置：`apps/agent-worker-linux/`（含 README、配置示例、systemd 安装、并发验收工具、离线测试）。

## 一、为什么换

原 `~/workspace/agent-worker-linux` 原型用 **Issue 评论 + 标签**认领（先读后写），
两台 Worker 并发时都能通过检查，不是原子锁。权威层已切到 GitHub 原生 ref-CAS
（PR #60~#65，SQLite 权威已删除）；本包把沙箱 Worker 接到同一套
`GitHubRefScheduler`：认领 = `POST /git/refs` 创建 HWM ref（服务端 201/422 裁决），
租约/fencing/范围锁/完成幂等全部以 GitHub refs/评论为权威，**不复制协议**。

## 二、部署要点（详细步骤看包内 README）

1. 三台 VM 各有一份**完整仓库 clone**，`git fetch && git checkout <目标 SHA>`；
2. 每台 VM 一份 JSON 配置：`worker_id` / `instance` **三台互不相同**
   （示例：`muse-1-linux` / `muse-2-linux` / `muse-3-linux`；禁止空值、禁止旧值
   `htch-runtime`）；`instance` 人类名会稳定映射为 ref 名里的十六进制 `ref_instance`
   （`--check` 同时打印两者）；
3. 认证：PAT（Contents + Issues 读写）经 `token_env`/`token_file` 注入，或
   `api_base` 指向本地 surrogate 网关；**token 不入库、不落盘、不打印**；
4. 安装：`cd <repo>/apps/agent-worker-linux && sudo ./install.sh`（幂等；首装生成
   `/etc/agent-worker-linux/config.json`，之后逐台改 `instance` 并重启）；
5. 自检：`python3 worker_linux.py --config ... --check`；
   `--once` 跑一个周期；`journalctl -u agent-worker-linux` 看日志。

原型的目录与旧 systemd 单元**不被修改**，停旧服务即可回滚（见 README 第八节）。

## 三、验收（Issue #73）

在任意一台 VM 上用两份不同 instance 的配置并发认领同一 Issue：

```bash
python3 verify_concurrent_claim.py --issue <N> \
  --config-a ~/cfg-a.json --config-b ~/cfg-b.json
```

期望：恰好 **1 个 `claimed` + 1 个 `LeaseHeld`**，并打印赢家的
`agent-hwm/issue-<N>/00000001` 与 `agent-lease/issue-<N>/...` ref；验收后用
`--cleanup-winner <winner.json>` 释放。GitHub 仓库 Branches / `git ls-remote` 可核对
HWM/lease ref；完成时只有一条 `ref-completion-id` 完成评论（无重复执行）。

三台 instance 互不相同：每台执行 `--check`，比较 JSON 的 `instance` 与
`ref_instance` 两个字段（后者不同即 ref 归属不同）。

## 四、本仓库侧验证记录（2026-11-15）

- 离线：`python -m pytest apps/agent-worker-linux/tests -q`（fake GitHub HTTP server，
  真实 TCP/服务端 ref 唯一性裁决）——并发认领恰好一赢一 `LeaseHeld`、成功完成评论+
  `state:in-review`+lease ref 删除、no-command→blocked、失败/超时/取消/scope 冲突。
- 真实 GitHub：在隔离私库 `Jouwala/noc-soc-ref-verify` 用两个进程并发认领同一测试
  Issue，结果与 refs 见 PR 描述 / `apps/agent-worker-linux/tests/evidence/`（如有）。
- CI：`.github/workflows/quality.yml` 的 `agent-tests`（Windows）与
  `agent-linux-process-tests`（Ubuntu）都会跑本包测试。

## 五、未验证 / 风险

- 真实 VM 的 systemd 安装与 PAT/surrogate 连通性需 Muse 1/2/3 在各自 VM 执行
  `--check`/`--once` 验证（本仓库侧只做了 `bash -n` 与本地双进程真实 API 验证）；
- instance 唯一性依赖逐 VM 配置；包内校验只能挡住空值/`htch-runtime`；
- lease ref 在认领期间可见、完成后删除；HWM ref 保留（token 单调），必要时由
  Reaper/人工清理策略处理。
