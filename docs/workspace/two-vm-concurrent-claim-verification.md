# 两台物理 VM 并发认领联调报告（#79）

- 日期：2026-11-15（UTC）
- 牵头：Muse 3（muse-3-linux 所在 VM）；参与：Muse 2（muse-2-linux 所在 VM）
- 被测对象：新包 `apps/agent-worker-linux`（ref-CAS 权威层，调度核心
  `apps/control-plane/backend/scheduler`），对**生产仓库**
  Jouwala/noc-soc-intelligent-platform 的验收 Issue **#97** 执行
  `worker_linux.py --claim-probe`。
- 背景：#73 的同类验收是在一台机器（MSNOC1）+ 一次性私有仓库上做的；
  本轮是两台物理 VM + 生产仓库的首次真实跨机认领。

## 时间线（全部为 2026-11-15 UTC，证据见 #97 评论与 refs）

| 时刻 | 事件 | 结果 |
| --- | --- | --- |
| 18:50:55 | muse-3-linux claim-probe #97 | claimed，token=1，HWM `agent-hwm/issue-97/00000001` + lease ref 生成 |
| ~19:00 | token=1 租约到期（TTL 600s） | lease ref 消失，HWM 保留 |
| 22:24:49 | muse-3-linux 再次 claim-probe | claimed，token=2 |
| ~22:34 | token=2 租约到期 | lease ref 消失 |
| 22:46:40 | muse-2-linux claim-probe（另一台 VM） | claimed，token=3（过期接管，fencing 递增） |
| 22:47:04 | muse-2-linux 主动释放 | 状态转 retryable，lease ref 即时消失 |
| 22:48:48 | muse-3-linux 再次 claim-probe | claimed，token=4，租约有效至约 22:58:48 |
| ~22:50 | muse-2-linux 在 token=4 租约有效期内 claim-probe | **LeaseHeld**（被拒，无新 ref 产生） |
| 22:52 | Muse 3 执行 `--cleanup-winner` 释放 token=4 | lease ref 清除；HWM 00000001–00000004 全部保留 |

## 判定

- **跨机互斥：PASS**。两台物理 VM 对同一 Issue 的认领严格互斥：租约
  有效期内异机认领被拒（LeaseHeld），同一时刻至多一个赢家；全部认领
  均经 HWM CAS 串行化，无重复赢家。
- **fencing：PASS**。token 随认领单调递增（1→2→3→4），过期接管只能
  拿到更高 token，旧持有者无法以旧 token 续命或回写。
- **租约回收：PASS**。两条路径都验证到：TTL 到期自动失效并可被异机
  接管（token=2→3）；持有者主动释放即时生效（token=3 释放后 24 秒内
  状态转 retryable、lease ref 消失）。
- **范围锁（scope lock）：NOT TESTED**。claim-probe 不走执行期文件
  范围认领，本轮未覆盖；需要双机常驻 `--once` 对同一带文件范围的
  任务做执行赛才能验，建议另开一轮联调补上。

## 备注

- 这是 ref-CAS 调度权威层第一次在生产仓库上产生真实调度 refs
  （此前 #76 普查时调度 refs 总数为 1）。
- 两侧鉴权均走各 VM 本地凭据通道（token 不落盘）；Muse 3 侧配置见
  联调记录（worker_id/instance=muse-3-linux，repo_root 为 main 的
  干净 checkout）。
- 验收 Issue #97 在报告入库后关闭；HWM refs 按设计保留为审计痕迹。
