"""refs 普查与配额告警验收（#76）。

覆盖三件事：
1. `list_matching_refs` 分页全量——超过一页（100 条）的 ref 不被截断
   （旧实现只取第一页是本任务要修的根因，假 API 已按真实 GitHub 分页）；
2. 普查按家族计数、HWM 按 issue 统计代次、任务分支不计入调度总量；
3. 阈值突破时退出码 2 并给出 ALERT，正常时退出 0；缺配置退出 1。
"""

from __future__ import annotations

from dispatch import refs_census
from dispatch.tests.fake_github import FakeGitHub

SHA = "a" * 40


def _seed(fake: FakeGitHub, name: str, count: int, start: int = 1) -> None:
    for i in range(start, start + count):
        fake.refs[f"refs/heads/{name}/{i:08d}"] = SHA


def test_list_matching_refs_paginates_beyond_first_page(fake: FakeGitHub) -> None:
    _seed(fake, "agent-hwm/issue-7", 150)
    items = fake.list_matching_refs("heads/agent-hwm")
    assert len(items) == 150  # 旧实现单页最多 100（真实 GitHub 默认仅 30）


def test_census_counts_families_and_hwm_generations(fake: FakeGitHub) -> None:
    _seed(fake, "agent-hwm/issue-7", 3)
    _seed(fake, "agent-hwm/issue-9", 2)
    _seed(fake, "agent-lease/issue-7/x", 4)
    _seed(fake, "dispatch/pr-merged", 5)
    fake.refs["refs/heads/agent/task-76-refs-census"] = SHA
    fake.refs["refs/heads/feat/something"] = SHA

    lines: list[str] = []
    code = refs_census.main([], env={}, client=fake, log=lines.append)
    assert code == 0
    text = "\n".join(lines)
    assert "调度 ref 合计 14" in text  # 3+2+4+5，任务分支与普通分支不计入
    assert "任务分支 1" in text
    assert "#7=3" in text and "#9=2" in text


def test_census_alerts_and_exit_2_over_threshold(fake: FakeGitHub) -> None:
    _seed(fake, "agent-hwm/issue-7", 12)
    lines: list[str] = []
    code = refs_census.main(
        ["--warn-total", "10", "--warn-hwm-per-issue", "5"],
        env={},
        client=fake,
        log=lines.append,
    )
    assert code == 2
    text = "\n".join(lines)
    assert "ALERT" in text and "合计 12 超过预警线 10" in text
    assert "issue #7 的 HWM 代次 12 超过预警线 5" in text


def test_census_missing_config_exit_1() -> None:
    assert refs_census.main([], env={}, client=None, log=lambda _line: None) == 1
