"""TASK-06-B（Issue #49）静态契约 + 行为用例：QA 失败自动修复闭环。

被测对象是 `.github/workflows/auto-fix.yml`（GitHub Actions 工作流，
无法在本机直接跑通整链），故用三层验证：
1. 静态契约：触发、权限、安全阀、消息头、模板字段、源码顺序
   （幂等检查与 needs-replan 分支必须在建 Issue 之前）；
2. 行为用例：把工作流里「按根任务数标记」的 jq/grep/awk 管线原样
   搬到夹具数据上跑（无 jq 时 skip）；
3. 决策表：与工作流判定顺序一致的镜像（sha 已建→跳过；
   已有轮次 >=3→needs-replan 不再建；否则建第 existing+1 轮）。
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-fix.yml"

MARKER_RE = (
    r"auto-fix: for-issue-[0-9]+ root-issue-[0-9]+ "
    r"round-[0-9]+ sha-[0-9a-f]{40}"
)


@pytest.fixture(scope="module")
def wf() -> str:
    assert WORKFLOW.exists(), f"工作流文件不存在：{WORKFLOW}"
    return WORKFLOW.read_text(encoding="utf-8")


def test_trigger_and_guard(wf: str) -> None:
    """触发：issues labeled + workflow_dispatch；job 层只放行 qa-failed。"""
    assert "issues:" in wf and "types: [labeled]" in wf
    assert "workflow_dispatch" in wf and "issue_number" in wf
    assert "github.event.label.name == 'state:qa-failed'" in wf


def test_permissions_least_privilege(wf: str) -> None:
    """只给 issues 写权限，不给 contents 写（不改代码是安全阀之一）。"""
    assert "issues: write" in wf
    assert "contents: write" not in wf


def test_failure_signal_comes_from_qa_failed_comment(wf: str) -> None:
    """失败内容取自 #48 的 QA_FAILED 结构化评论，且在建 Issue 之前解析。"""
    assert 'test("agent-msg: QA_FAILED, task: #"' in wf
    assert "qa-result: pr-[0-9]+" in wf  # PR 编号取自 watcher 的结果标记
    pos_parse = wf.index("QA_JSON=")
    pos_create = wf.index('"$API/issues" --data-binary @-)')
    assert pos_parse < pos_create


def test_marker_is_the_counting_source(wf: str) -> None:
    """轮次计数只认修复 Issue 正文标记（GitHub 单点可读，不依赖本地文件）。"""
    assert (
        "auto-fix: for-issue-$ISSUE root-issue-$ROOT round-$ROUND sha-$HEAD_SHA" in wf
    )
    assert "/issues?state=all&per_page=100" in wf
    assert "root-issue-$ROOT" in wf  # 链式返工按根任务计数


def test_idempotency_checked_before_create(wf: str) -> None:
    """同一 root + 同一失败 HEAD SHA 已建过 → 跳过，且检查在建 Issue 前。"""
    pos_idem = wf.index("幂等跳过")
    pos_create = wf.index('"$API/issues" --data-binary @-)')
    assert pos_idem < pos_create
    assert 'grep -q "sha-$HEAD_SHA"' in wf


def test_replan_branch_before_create(wf: str) -> None:
    """第 4 次失败（已有 >=3 轮）：打 needs-replan + 评论后退出，不建 Issue。"""
    pos_gate = wf.index('"$EXISTING" -ge 3')
    pos_round = wf.index("ROUND=$((EXISTING + 1))")
    pos_create = wf.index('"$API/issues" --data-binary @-)')
    assert pos_gate < pos_round < pos_create
    replan_block = wf[pos_gate:pos_round]
    assert "needs-replan" in replan_block
    assert "exit 0" in replan_block
    # needs-replan 是终止标记：已带该标签时整体跳过
    assert "已带 needs-replan（终止标记），跳过" in wf


def test_agent_assignment_rules(wf: str) -> None:
    """回派原 Issue 上的任意 agent:muse-N 标签（muse-2/3/4/5）；
    取不到时默认派给 muse-2（正文注明请 Muse 1 复核），禁止创建
    无 agent 标签的修复 Issue（P0-3）。"""
    assert "grep -oE '^agent:muse-[0-9]+$'" in wf
    assert 'AGENT="muse-2"' in wf  # 无 agent 标签时的默认负责人
    assert "默认指派" in wf
    assert "请 Muse 1 复核" in wf
    assert '\\"agent:$AGENT\\"' in wf  # 修复 Issue 的 agent 标签来自判定结果
    assert "未派给任何人" not in wf  # 不再有无负责人分支
    assert "待 Muse 1 指派" not in wf


def test_fix_requested_message_fields(wf: str) -> None:
    """FIX_REQUESTED（协议第二节第 9 类）：新 Issue、报告链接、轮次齐全。"""
    assert "agent-msg: FIX_REQUESTED, task: #$ISSUE, from: auto-fix" in wf
    assert "- 新 Issue：#$FIX_NUMBER" in wf
    assert "- 失败报告：$QA_URL" in wf
    assert "修复轮次：第 $ROUND 次（上限 3）" in wf


def test_fix_issue_body_follows_template(wf: str) -> None:
    """修复 Issue 正文按 agent-task.md 模板八节填齐，含依赖与分支字段。"""
    for section in (
        "## 任务说明",
        "## 负责人",
        "## 优先级",
        "## 文件修改范围",
        "## 依赖任务",
        "## 验收标准",
        "## 所属分支",
        "## 当前状态",
    ):
        assert section in wf, f"修复 Issue 正文缺模板小节：{section}"
    assert "依赖：depends on #$ISSUE" in wf
    assert "由 issue-to-pr 按本 Issue 编号自动创建" in wf  # 分支不再写死编号
    assert "agent/task-49-fix-$ISSUE" not in wf  # 旧的写死分支名已删除
    assert 'LABELS_JSON="[\\"state:todo\\", \\"agent:$AGENT\\"]"' in wf  # 修复 Issue 一定带 state:todo + agent 标签
    # 失败项逐条转成可打勾的验收项
    assert '"- [ ] 修复：$item"' in wf


def test_run_block_bash_syntax(wf: str) -> None:
    """抽出 run: | 块做 bash -n 语法检查（无 bash 时 skip）。"""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("无 bash，跳过语法检查")
    lines = wf.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.rstrip().endswith("run: |"))
    indent = len(lines[start]) - len(lines[start].lstrip())
    block: list[str] = []
    for ln in lines[start + 1 :]:
        if ln.strip() == "":
            block.append("")
            continue
        cur = len(ln) - len(ln.lstrip())
        if cur <= indent:
            break
        block.append(ln[indent + 2 :])
    script = "\n".join(block)
    # 输入按 UTF-8 字节传：Windows 的 text 模式走 cp1252，
    # 脚本里的中文会编码失败（CI 实测 UnicodeEncodeError）。
    proc = subprocess.run(
        [bash, "-n"], input=script.encode("utf-8"), capture_output=True, check=False
    )
    assert proc.returncode == 0, (
        f"bash -n 失败：{proc.stderr.decode('utf-8', errors='replace')}"
    )


_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40

FIXTURE_ISSUES = [
    # 根任务 48 的两轮修复（不同失败 HEAD；第二轮的直属任务已是修复 Issue）
    {
        "body": f"<!-- auto-fix: for-issue-48 root-issue-48 round-1 "
        f"sha-{_SHA_A} -->\n## 任务说明"
    },
    {"body": f"<!-- auto-fix: for-issue-90 root-issue-48 round-2 sha-{_SHA_B} -->"},
    # 另一个根任务的修复
    {"body": f"<!-- auto-fix: for-issue-60 root-issue-60 round-1 sha-{_SHA_C} -->"},
    {"body": None},  # 正文为空的 Issue
    {"body": "普通 Issue，没有 auto-fix 标记"},  # 无标记
]


def _count_root_markers(issues: list[dict], root: int) -> int:
    """与工作流一致的计数管线：jq 取正文 → grep 抽标记 → awk 按根过滤。"""
    import json

    jq = subprocess.run(
        ["jq", "-r", '.[].body // ""'],
        input=json.dumps(issues).encode("utf-8"),
        capture_output=True,
        check=True,
    )
    grep = subprocess.run(
        ["grep", "-oE", MARKER_RE],
        check=False,
        input=jq.stdout,
        capture_output=True,
    )
    awk = subprocess.run(
        [
            "awk",
            "-v",
            f"r=root-issue-{root}",
            "{for (i = 1; i <= NF; i++) if ($i == r) print}",
        ],
        input=grep.stdout,
        check=False,
        capture_output=True,
    )
    out = awk.stdout.decode("utf-8", errors="replace")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return len(lines)


@pytest.mark.skipif(not shutil.which("jq"), reason="无 jq，跳过计数行为测试")
def test_marker_counting_behavior() -> None:
    """计数按根任务聚合：链式返工（for-issue 不同、root 相同）算同一任务。"""
    assert _count_root_markers(FIXTURE_ISSUES, 48) == 2
    assert _count_root_markers(FIXTURE_ISSUES, 60) == 1
    assert _count_root_markers(FIXTURE_ISSUES, 99) == 0


def decide(existing: int, sha_seen: bool) -> tuple[str, int | None]:
    """工作流判定顺序的镜像：幂等 → 上限 → 建下一轮。"""
    if sha_seen:
        return ("skip", None)
    if existing >= 3:
        return ("replan", None)
    return ("create", existing + 1)


def test_decision_table() -> None:
    assert decide(0, False) == ("create", 1)
    assert decide(1, False) == ("create", 2)
    assert decide(2, False) == ("create", 3)
    assert decide(3, False) == ("replan", None)
    assert decide(5, False) == ("replan", None)
    # 幂等优先于上限：同一失败 HEAD 重复触发永远先跳过
    assert decide(0, True) == ("skip", None)
    assert decide(3, True) == ("skip", None)


def test_no_forbidden_actions(wf: str) -> None:
    """安全阀：工作流不改代码、不合并、不关闭原 Issue。"""
    assert "/merge" not in wf
    assert '"state": "closed"' not in wf and '\'{"state": "closed"}\'' not in wf
    assert "actions/checkout" not in wf
    # 正则常量与工作流里的标记格式保持一致（防两边漂移）
    assert re.search(
        MARKER_RE, "auto-fix: for-issue-1 root-issue-1 round-1 sha-" + "a" * 40
    )
