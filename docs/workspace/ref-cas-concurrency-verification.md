# 真实 GitHub Actions 双 workflow 并发 ref-CAS 验证报告（#80）

- 日期：2026-11-15（UTC）
- 执行：Muse 3（经用户批准的隔离仓方案）
- 隔离仓：`Jouwala/ref-cas-concurrency-probe`（Private，一次性，验证后可删）
- 结论：**PASS**——真实 Actions 并发下，ref-CAS 仍是单一赢家。

## 为什么做这个验证

`docs/workspace/github-automation.md` 第十节长期挂着一项 NOT TESTED：
「真实 GitHub Actions 上的并发执行（两 workflow 同时跑）」。此前并发与
幂等只在本机用内存假 GitHub API 做过多线程 CAS 测试，真实 GitHub 的
`POST /git/refs` 创建-if-不存在语义在两条独立 workflow 真并发时是否
仍保证唯一赢家，没有实测证据。本验证补齐这一项。

## 方法

- 隔离仓放两条 workflow：`cas-probe-a`、`cas-probe-b`（除 side 标识外
  完全相同），各自一个独立的 Actions runner。
- 探针脚本 `cas_probe.py` 不复制调度器代码：运行时从主仓按固定 commit
  `d8cb2ff55b68aae0c37cda472c77046523d248c8` 拉取
  `apps/control-plane/backend/scheduler/` 的 `api.py` / `ref_api.py` /
  `github_refs.py`，直接调用生产类 `RestRefStore.create()`——测的就是
  权威层在用的代码路径（201=赢家；409/422 后 GET 消歧确认已存在=输家）。
- 对齐方式：两条 workflow 填同一个发令时刻 `fire_at=1794742088`
  （2026-11-15 11:28:08 UTC），双方 runner 各自等到该时刻才发起第 1 轮，
  之后每 20 秒一轮，共 5 轮；每轮竞争创建同一个 ref
  `refs/heads/cas-probe/round-<k>`，目标 SHA 为探针仓当时的 `github.sha`。
- 判定标准：每轮 A、B 两侧恰好一个 WIN、一个 LOSE，且对应
  `cas-probe/round-<k>` 分支存在；任一輪双 WIN / 双 LOSE / 报错即 FAIL。

## 结果

两条 run 均 Success：

| Run | 结论 | 链接 |
| --- | --- | --- |
| cas-probe-a | Success（4 分 18 秒） | https://github.com/Jouwala/ref-cas-concurrency-probe/actions/runs/43708834290 |
| cas-probe-b | Success（3 分 22 秒） | https://github.com/Jouwala/ref-cas-concurrency-probe/actions/runs/43708907614 |

逐轮结果（`at` 为发起时刻的 epoch 秒，两侧同轮完全一致，证明发令对齐
生效；`dt` 为该次 create 调用耗时秒）：

| 轮 | A 侧 | B 侧 | 赢家 |
| --- | --- | --- | --- |
| 1 | LOSE（at=1794742088.000, dt=1.054） | WIN（at=1794742088.000, dt=0.619） | B |
| 2 | WIN（at=1794742108.000, dt=0.615） | LOSE（at=1794742108.000, dt=1.078） | A |
| 3 | WIN（at=1794742128.000, dt=0.584） | LOSE（at=1794742128.000, dt=0.929） | A |
| 4 | LOSE（at=1794742148.000, dt=0.970） | WIN（at=1794742148.000, dt=0.527） | B |
| 5 | WIN（at=1794742168.000, dt=0.587） | LOSE（at=1794742168.000, dt=0.947） | A |

汇总：5/5 轮恰好一个赢家（A 胜 3 轮、B 胜 2 轮），无双赢、无双输、
无调用报错；探针仓分支列表中 `cas-probe/round-1` 至 `round-5` 全部
存在（github-actions[bot] 创建），与逐轮结果一致。

## 边界与说明

- 本验证覆盖的是**同一 ref 的并发创建**这一 CAS 原语（认领/分派登记的
  互斥根基）。更高层的协议行为（租约续期、fencing 代次、回收竞态）
  由调度器自身的离线测试与 `multi-worker-regression.md` 的回归结论
  覆盖，不在本验证范围。
- 发令对齐依赖 runner 时钟（GitHub 托管 runner 为 NTP 同步）；本轮
  两侧 `at` 逐秒完全一致，对齐误差小于日志精度（毫秒位均为 .000，
  因脚本按整秒目标时刻起跑）。
- 隔离仓与主仓完全隔离：未触碰主仓任何 ref；探针仓在报告入库后可
  整体删除，删除不影响本报告的证据链接之外的任何东西（run 链接随
  仓删除失效，逐轮数据已抄录于上表）。
