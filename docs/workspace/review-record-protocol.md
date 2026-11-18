# 独立代码审核记录协议（Phase 2 P2-1，Issue #131）

> 状态：2026-11-18 起执行。替代 PR #118 的 `reviewed` 标签关口（标签无 SHA
> 绑定、无身份校验、可自贴，审计结论：不能作为审核凭证；#118 在双门禁
> 落地后关闭）。
>
> 本协议只定义**记录格式与判定规则**。门禁接线（auto-merge 双门禁 +
> PR 开启时通知）见 Issue #132。

## 一、为什么需要这份协议

2026-11-17 审计确认：`main` 的合并链路没有任何代码审核——PR #109、
#110、#112、#113、#114、#115、#117 全部只凭 CI 全绿 + 冒烟通过就合入，
CI 通过不等于代码没问题。用户指令（2026-11-17）：“审核是不可避免的、
不可缺失的”。

审核记录必须同时满足三条，缺一不可：

1. **SHA 绑定**：审核结论绑定到某一个具体的 head SHA；作者推新提交后，
   旧审核自动失效，必须重新审。
2. **可机读**：结论以机器可读标记写进 PR 评论，auto-merge 在合并前独立
   复核，不依赖任何人在场。
3. **可验证**：任何人按本文件都能复核“这条通过结论对应哪个 SHA、谁审的、
   什么结论”，不需要信任口头说法。

## 二、记录格式

审核者在 PR 下发一条评论，**首行必须是机器可读标记**（HTML 注释），
第二行起是人可读正文（结论、发现、复核证据）。

```
<!-- review-result: pr-<N> sha-<40 位小写十六进制 SHA> reviewer-<审核者> verdict=passed|changes-requested|blocked -->
```

字段：

| 字段 | 说明 |
| --- | --- |
| `pr-<N>` | PR 编号，必须等于本 PR |
| `sha-<40>` | 被审核的 head SHA（完整 40 位） |
| `reviewer-<名>` | 审核者标识，只允许字母/数字/`_`/`-`/`.`，如 `muse-1`、`muse-4` |
| `verdict=` | 三选一（见第三节） |

人可读正文写：结论、逐条发现（带 `文件:行号` 证据）、复核过的测试命令
与结果、CI 状态。不写空话。

示例（通过）：

```
<!-- review-result: pr-127 sha-dfbec07... reviewer-muse-1 verdict=passed -->

## 独立代码审核：通过

- 审核对象：head dfbec07，单 commit
- 复核：dispatch/tests/test_auto_dispatch.py 37 passed；dispatch/tests/ 107 passed
- 结论：阻塞问题已修，无新增问题。
```

示例（要求改动）：

```
<!-- review-result: pr-127 sha-6ce6b02... reviewer-muse-1 verdict=changes-requested -->

## 独立代码审核：要求改动

- 发现 1（阻塞）：Issue 事件分派评论误写“PR #0 合并后”
  （auto_dispatch.py:269-280，build_assignment_comment 无 issues 分支）
  ……
```

## 三、结论（verdict）

| verdict | 含义 | 对合并的影响 |
| --- | --- | --- |
| `passed` | 代码可合入（无阻塞发现；非阻塞建议可留待后续） | 满足审核门禁（还须 sha 等于当前 head） |
| `changes-requested` | 有阻塞发现，作者须改 | 阻断合并；作者改完推新提交后须重新审核 |
| `blocked` | 审核者判断此 PR 不应继续（如涉及生产库、需用户决定） | 阻断合并；须 Muse 1 或用户处理后才能重审 |

## 四、判定规则（唯一真相源）

实现见 `apps/control-plane/backend/dispatch/review_gate.py`，
测试见 `apps/control-plane/backend/dispatch/tests/test_review_gate.py`。
规则：

1. **只看最后一条标记**：按评论时间顺序取 PR 下最后一条 review-result
   标记，之前的所有标记一律作废。
2. **三条件同时成立才放行**：标记的 pr 等于本 PR；verdict 是 `passed`；
   标记的 sha 等于 PR 当前 head SHA。
3. **新提交自动失效**：作者推新提交后 head 变化，旧 passed 标记的 sha
   对不上，不放行——不需要任何人手动摘标记。
4. **改判覆盖**：审核者事后补一条 `changes-requested` / `blocked`，最后
   一条标记说了算，旧 passed 失效。
5. **无记录 = 未审**：一条标记都没有，门禁不通过。
6. **格式不对的标记不算数**：解析不到的标记直接跳过，不抛错、不放行。

## 五、谁审、谁不能审

- **审核者**：现阶段由 Muse 1（总指挥）执行独立审核，可委派只读审核
  subagent 复核代码与测试；Muse 4 转为专职审核 Worker 是 Phase 2 P2-2
  的工作（需先补机器与 PAT 登记）。
- **作者不能审自己的代码**：审核者与 PR 作者必须是不同 Agent。当前三
  个 Muse 共用同一个 GitHub 身份（Jouwala），git 作者无法区分，因此以
  PR 正文的「执行者标识」与分支归属判定作者身份。
- **审核记录的发布**：审核通过后，由 Muse 1 把标记评论贴到 PR 上
  （`reviewer-muse-1`）；`needs-review` 标签同时摘掉（见 #132）。

## 六、身份校验的已知局限（诚实记录，不假装已解决）

三名 Muse 共用同一个 GitHub 账号，GitHub 侧看到的评论作者永远是
Jouwala——**仅凭 GitHub 评论无法区分“谁贴的标记”**。因此现阶段的
保证是程序性的，不是密码学意义上的：

- 标记只由 Muse 1 在完成一次独立审核（只读 subagent 复核 diff 与测试）
  后发布；
- 标记本身 SHA 绑定、可复核：任何人都能按第四节重算“这条 passed 对应
  哪个 head”；
- 作者自贴 passed 标记在技术上无法阻止——防线是流程（PR 正文执行者
  标识 + 审核记录的 reviewer 字段 + Muse 1 的复核），不是权限。

真正的身份隔离需要独立的审核者凭据（专用 PAT / 专用 GitHub App /
独立账号），这是 P2-2 的内容，需用户侧决策（审计报告第十节决策 3）。
**在 P2-2 落地前，不要对外宣称审核身份已通过技术手段验证。**

## 七、与 QA 门禁的关系（双门禁）

- **QA 门禁**（已有）：CI 全绿 + `smoke-passed` 标签 + smoke-result 标记
  绑定当前 head。回答“跑起来对不对”。
- **审核门禁**（本协议）：review-result 标记 passed + 绑定当前 head。
  回答“代码写得对不对”。
- 两者针对**同一 SHA**，缺一不可，互不替代。接线见 Issue #132。

## 八、协议消息类型（补 agent-protocol.md）

在 `docs/workspace/agent-protocol.md` 第二节 12 类消息后追加：

| # | 消息类型 | 机器可读头 | 谁发 | 人可读正文写什么 |
| --- | --- | --- | --- | --- |
| 13 | REVIEW_PASSED | `<!-- review-result: pr-<N> sha-<40> reviewer-<名> verdict=passed -->` | Muse 1（独立审核） | 审核对象 head、复核的测试命令与结果、结论 |
| 14 | REVIEW_FAILED | `<!-- review-result: pr-<N> sha-<40> reviewer-<名> verdict=changes-requested -->` | Muse 1（独立审核） | 逐条阻塞发现（文件:行号 + 证据）与要求的修复 |
| 15 | REVIEW_BLOCKED | `<!-- review-result: pr-<N> sha-<40> reviewer-<名> verdict=blocked -->` | Muse 1（独立审核） | 阻断原因（如涉生产库、须用户决定）与下一步 |

状态机补充：`state:in-review` 是“PR 已开、待独立审核”的状态；审核
通过后 Issue 仍保持 `state:in-review` 直到合入（合入后
pr-merged-writeback 打 `state:merged`）。QA 标签（qa-ready /
qa-passed / qa-failed）与审核状态并行存在，互不覆盖。
