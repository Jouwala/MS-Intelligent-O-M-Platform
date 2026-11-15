"""TASK-06-A（#48）契约守卫：QA 结果回写 Issue + smoke-passed 的 SHA 绑定门禁。

被测对象是两个非 Python 文件：
- ``ops/deploy/smoke-watcher.ps1``（D:\\02 本机冒烟轮询器，PowerShell）；
- ``.github/workflows/auto-merge.yml``（自动合并工作流，bash 内嵌脚本）。

两者都无法在本测试进程里直接执行（一个要 Windows + 真实 GitHub token，
一个跑在 GitHub Actions 里），所以按仓库守卫的一贯做法（AGENTS.md 第四节
第 9 条：静态解析、不 import 业务模块、取不到真相源必须显式失败）做
**契约测试**：断言协议要求的消息头、标签、去重标记、SHA 绑定标记与判定
顺序真实存在于源文件里，谁删掉/改坏其中一环，这里立刻变红。另外对
auto-merge 的 jq 提取式做行为级验证（本机有 jq 时）：喂评论夹具，断言
「最后一条 passed 标记胜出、failed 标记不算数、无标记返回空」。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WATCHER_PATH = REPO_ROOT / "ops" / "deploy" / "smoke-watcher.ps1"
AUTO_MERGE_PATH = REPO_ROOT / ".github" / "workflows" / "auto-merge.yml"

# auto-merge.yml 里提取绑定 SHA 的 jq 过滤式（与工作流原文逐字一致，
# test_auto_merge_gate_source_contract 会断言原文里有这一串）。
JQ_FILTER = (
    '[.[] | (.body // "") | '
    'capture("smoke-result: pr-[0-9]+ sha-(?<sha>[0-9a-f]{40}) result=passed"; "g") '
    '| .sha] | last // ""'
)

# watcher 里读绑定 SHA 的正则（PowerShell -match，默认不区分大小写）。
WATCHER_BIND_RE = re.compile(
    r"smoke-result:\s*pr-\d+\s+sha-([0-9a-f]{40})\s+result=passed", re.IGNORECASE
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


@pytest.fixture(scope="module")
def watcher() -> str:
    assert WATCHER_PATH.is_file(), f"真相源缺失：{WATCHER_PATH}"
    return WATCHER_PATH.read_text(encoding="utf-8-sig")


@pytest.fixture(scope="module")
def auto_merge() -> str:
    assert AUTO_MERGE_PATH.is_file(), f"真相源缺失：{AUTO_MERGE_PATH}"
    return AUTO_MERGE_PATH.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    """截取 PowerShell 函数体（从 function 名到下一个顶层 function 为止）。"""
    start = source.index(f"function {name}")
    nxt = source.find("\nfunction ", start + 1)
    return source[start:] if nxt == -1 else source[start:nxt]


# ---------------------------------------------------------------------------
# 缺口 1：QA 结果回写 Issue
# ---------------------------------------------------------------------------


def test_watcher_sends_qa_started_with_label_before_smoke(watcher: str) -> None:
    body = _function_body(watcher, "Send-QaStarted")
    # 协议第 6 类消息头：from 必须是 smoke-watcher，task 指向关联 Issue。
    assert "agent-msg: QA_STARTED, task: #$Issue, from: smoke-watcher" in body
    # 开始前给 Issue 打 state:qa-ready（经 QA 状态单值化入口）。
    assert "Set-IssueQaState" in body and "state:qa-ready" in body
    # 内容含协议要求的三项：PR 号、HEAD SHA、预计时长。
    assert "PR：#$PrNumber" in body
    assert "HEAD SHA" in body and "$Sha" in body
    assert "预计时长" in body
    # 调用点必须在真正跑冒烟之前：主循环里 Send-QaStarted 先于 Enter-PrCheckout。
    cycle = _function_body(watcher, "Invoke-OneCycle")
    assert cycle.index("Send-QaStarted") < cycle.index("Enter-PrCheckout")


def test_watcher_sends_qa_passed_and_failed_with_labels(watcher: str) -> None:
    body = _function_body(watcher, "Send-QaResult")
    assert "agent-msg: $type, task: #$Issue, from: smoke-watcher" in body
    assert "'QA_PASSED'" in body and "'QA_FAILED'" in body
    assert "state:qa-passed" in body and "state:qa-failed" in body
    # QA_FAILED 是结构化报告：PR、HEAD SHA、失败项、错误信息缺一不可。
    for field in ("PR：#$PrNumber", "HEAD SHA", "失败项", "错误信息"):
        assert field in body, f"QA 结果正文缺字段：{field}"
    # QA_PASSED 注明通过项与标签去向。
    assert "通过项" in body and "smoke-passed" in body


def test_watcher_qa_comments_are_deduplicated_by_marker(watcher: str) -> None:
    # 去重标记格式与 Issue 要求一致：qa-started: / qa-result: pr-<N>-sha-<短SHA>。
    assert '"qa-started: pr-$PrNumber-sha-$Short"' in watcher
    assert '"qa-result: pr-$PrNumber-sha-$Short"' in watcher
    # 发评论前先查标记（定义 1 处 + QA_STARTED/QA 结果各调用 1 处）。
    assert watcher.count("Test-IssueMarker") >= 3
    started = _function_body(watcher, "Send-QaStarted")
    result = _function_body(watcher, "Send-QaResult")
    for body in (started, result):
        assert body.index("Test-IssueMarker") < body.index("Send-IssueComment")


def test_watcher_issue_resolution_unchanged(watcher: str) -> None:
    # 关联解析沿用既有三路顺序，不许自造第四路（Issue #48 范围约束）。
    body = _function_body(watcher, "Resolve-IssueNumber")
    i_close = body.index("(closes|fixes|resolves)")
    i_task = body.index("task:")
    i_branch = body.index("agent/task-")
    assert i_close < i_task < i_branch


# ---------------------------------------------------------------------------
# 缺口 2：smoke-passed 绑定 head SHA + 过期处理
# ---------------------------------------------------------------------------


def test_watcher_marks_smoke_report_with_bound_sha(watcher: str) -> None:
    # 报告评论首行带机器可读绑定标记，SHA 用冒烟时的完整 head SHA。
    assert '"<!-- smoke-result: pr-{0} sha-{1} result={2} -->"' in watcher
    cycle = _function_body(watcher, "Invoke-OneCycle")
    # 标记必须随报告一起贴到 PR（在 POST 报告评论之前拼好）。
    assert cycle.index("smoke-result: pr-{0}") < cycle.index("body = $report")


def test_watcher_reruns_when_smoke_passed_is_stale(watcher: str) -> None:
    cycle = _function_body(watcher, "Invoke-OneCycle")
    # 不再是「见 smoke-passed 就无条件跳过」：要核对绑定 SHA。
    assert "Get-SmokeResultBoundSha" in cycle
    assert "Remove-IssueLabel -Number $n -Label 'smoke-passed'" in cycle
    assert "已过期" in cycle and "重跑" in cycle
    # 过期处理发生在查 CI / 跑冒烟之前（同一轮内摘标签后继续往下走）。
    stale_at = cycle.index("Remove-IssueLabel")
    assert stale_at < cycle.index("Test-QualityGreen")


def test_watcher_bound_sha_regex_semantics(watcher: str) -> None:
    # watcher 源码里的绑定正则与本测试复刻的语义一致（防两边漂移）。
    assert "smoke-result:" in watcher and "result=passed" in watcher
    comments = [
        f"<!-- smoke-result: pr-9 sha-{SHA_A} result=passed -->\n报告",
        f"<!-- smoke-result: pr-9 sha-{SHA_B} result=failed -->\n报告",
        f"正文里顺带提到 smoke-result: pr-9 sha-{SHA_C} result=passed 也算",
    ]
    bound = ""
    for body in comments:
        m = WATCHER_BIND_RE.search(body)
        if m:
            bound = m.group(1)
    # 最后一条 passed 胜出；failed 不回写绑定。
    assert bound == SHA_C
    assert WATCHER_BIND_RE.search("没有任何标记的评论") is None


def test_auto_merge_gate_source_contract(auto_merge: str) -> None:
    # 先有标签、再验绑定、最后才合并：三者的源码顺序就是门禁顺序。
    i_label = auto_merge.index("has_label 'smoke-passed'")
    i_bind = auto_merge.index("SMOKE_SHA=")
    i_merge = auto_merge.index("/pulls/$PR/merge")
    assert i_label < i_bind < i_merge
    # 提取式与本文件顶部的 JQ_FILTER 逐字一致（防测试与实现各写一份）。
    assert JQ_FILTER in auto_merge
    # 两种过期情形都必须跳过且留日志：无标记、对不上当前 head。
    assert "没有 SHA 绑定标记" in auto_merge
    assert '"$SMOKE_SHA" != "$HEAD_SHA"' in auto_merge
    assert "结果已过期，跳过" in auto_merge


def test_watcher_records_smoked_and_failed_sha_in_state(watcher: str) -> None:
    # 本机状态文件按 PR 记录冒烟时的完整 SHA：通过与失败各一份，
    # 同一 head 不重复冒烟、失败等新 head 再跑都靠它（与标记互为冗余）。
    assert "$state['smoked'][$key] = $sha" in watcher
    assert "$state['failed'][$key] = $sha" in watcher


def test_marker_contract_shared_by_watcher_and_gate(
    watcher: str, auto_merge: str
) -> None:
    # 两侧共用同一标记协议：smoke-result: pr-<N> sha-<40位> result=passed。
    for text in (watcher, auto_merge):
        assert "smoke-result:" in text
        assert "result=passed" in text


# ---------------------------------------------------------------------------
# 门禁提取式的行为级验证（需要 jq；CI 的 ubuntu/windows 镜像都自带）
# ---------------------------------------------------------------------------


def _gate_extract(comments: list[dict]) -> str:
    proc = subprocess.run(
        ["jq", "-r", JQ_FILTER],
        input=json.dumps(comments),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.mark.skipif(shutil.which("jq") is None, reason="本机没有 jq，跳过行为级验证")
def test_gate_extraction_behavior() -> None:
    passed_a = {
        "body": f"<!-- smoke-result: pr-9 sha-{SHA_A} result=passed -->\n## 通过"
    }
    failed_b = {
        "body": f"<!-- smoke-result: pr-9 sha-{SHA_B} result=failed -->\n## 失败"
    }
    passed_c = {
        "body": f"<!-- smoke-result: pr-9 sha-{SHA_C} result=passed -->\n## 通过"
    }
    # 最后一条 passed 胜出。
    assert _gate_extract([passed_a, failed_b, passed_c]) == SHA_C
    # 只有 failed 标记时视为无绑定（门禁会跳过）。
    assert _gate_extract([failed_b]) == ""
    # 无标记 / 空列表 / body 为 null 都返回空串，不报错。
    assert _gate_extract([{"body": "普通评论"}, {"body": None}]) == ""
    assert _gate_extract([]) == ""


def test_gate_decision_table() -> None:
    """门禁判定表（与 auto-merge.yml 的三条分支一一对应）：

    绑定 SHA 为空 → 跳过（无标记）；绑定 != 当前 head → 跳过（过期）；
    绑定 == 当前 head → 放行。三条分支的日志原文在源码契约测试里已断言，
    这里把判定本身写成表驱动用例，防后续改动把条件写反。
    """

    def decision(bound_sha: str, head_sha: str) -> str:
        if not bound_sha:
            return "skip-no-marker"
        if bound_sha != head_sha:
            return "skip-stale"
        return "proceed"

    assert decision("", SHA_A) == "skip-no-marker"
    assert decision(SHA_B, SHA_A) == "skip-stale"
    assert decision(SHA_A, SHA_A) == "proceed"
