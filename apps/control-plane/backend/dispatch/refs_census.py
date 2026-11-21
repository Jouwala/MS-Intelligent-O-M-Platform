"""调度 refs 普查与配额告警 CLI（#76）—— GitHub Actions 定时入口。

背景：GitHub 原生权威层（ADR-7）把认领/租约/范围锁/分派登记都落在
refs 上，其中 HWM 每次续租新增一条、只增不减。长期运行后 refs 会持续
累积，而此前没有任何统计与告警：数量失控的表现是 matching-refs 列表
变慢、分支列表噪音、配额风险。本 CLI 只做**只读普查**：

1. 分页全量列出 `refs/heads/` 下全部 ref（经 `list_matching_refs`，
   其分页已在 #76 修复——旧实现只取第一页会静默截断）；
2. 按家族计数（agent-hwm / agent-lease / agent-scope / agent-dispatch /
   agent-reap / dispatch），HWM 再按 issue 统计代次数量；
3. 任一阈值被突破时打印 `[census] ALERT ...` 并以退出码 2 结束，
   让定时 workflow 变红即告警；正常退出 0；配置/API 错误退出 1。

阈值是**预警线，不是 GitHub 的硬配额**（GitHub 未公布单仓 ref 上限）：
定在「列表成本与噪音开始显著、但离任何真实风险还远」的位置，可经
环境变量 / 命令行调整。清理动作不在本 CLI 内（误删会破坏 CAS 幂等）：
各家族的可删范围、保留期与清理入口见
`docs/workspace/github-automation.md` 第十一节；自动回收只有
lease-reaper（过期 lease + 孤儿 scope）这一条既有通道。

用法：
```bash
REPO=owner/name GH_TOKEN=<redacted> python apps/control-plane/backend/dispatch/refs_census.py
# 可选：--warn-total N / --warn-family N / --warn-hwm-per-issue N
# 环境变量：AGENT_REFS_WARN_TOTAL / AGENT_REFS_WARN_FAMILY / AGENT_REFS_WARN_HWM_PER_ISSUE
```
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

# 以脚本方式运行时（Actions 里的 `python apps/.../refs_census.py`）sys.path[0]
# 是 dispatch/，backend/ 不在路径上；放进去后统一按包导入（同 lease_reaper）。
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

try:  # 作为包导入（pytest: dispatch.refs_census）
    from .github_client import GitHubAPIError, UrllibGitHubClient
except ImportError:  # 作为脚本直接运行（__package__ 为空）
    from github_client import GitHubAPIError, UrllibGitHubClient  # type: ignore[no-redef]

#: 调度 ref 家族（前缀 → 展示名）。agent/task-* 是任务分支、不是调度 ref，
#: 单列展示但不计入调度总量告警，免得把正常开发分支算成配额压力。
SCHEDULER_FAMILIES: tuple[tuple[str, str], ...] = (
    ("refs/heads/agent-hwm/", "agent-hwm（认领高水位，只增不减）"),
    ("refs/heads/agent-lease/", "agent-lease（租约）"),
    ("refs/heads/agent-scope/", "agent-scope（范围锁）"),
    ("refs/heads/agent-dispatch/", "agent-dispatch（分派登记）"),
    ("refs/heads/agent-reap/", "agent-reap（回收认领记录）"),
    ("refs/heads/dispatch/", "dispatch（TASK-07 分派/QA 登记）"),
)
TASK_BRANCH_PREFIX = "refs/heads/agent/task-"

#: 预警阈值默认值（预警线，非硬配额；见模块 docstring）。
DEFAULT_WARN_TOTAL = 1000
DEFAULT_WARN_FAMILY = 500
DEFAULT_WARN_HWM_PER_ISSUE = 100

ENV_WARN_TOTAL = "AGENT_REFS_WARN_TOTAL"
ENV_WARN_FAMILY = "AGENT_REFS_WARN_FAMILY"
ENV_WARN_HWM_PER_ISSUE = "AGENT_REFS_WARN_HWM_PER_ISSUE"

_HWM_ISSUE_RE = re.compile(r"^refs/heads/agent-hwm/issue-(\d+)/")


class CensusError(RuntimeError):
    """配置缺失等可预期失败：打印原因 + 退出码 1，不抛栈。"""


def env_config(env: Optional[dict] = None) -> tuple[str, str, str]:
    """从 Actions 环境变量取 (repo, token, api_base)；缺失时抛清晰错误。"""
    values = os.environ if env is None else env
    repo = str(values.get("REPO") or values.get("GITHUB_REPOSITORY") or "").strip()
    if not repo or "/" not in repo:
        raise CensusError("缺少 REPO 环境变量（形如 owner/name）")
    token = str(values.get("GH_TOKEN") or values.get("GITHUB_TOKEN") or "").strip()
    if not token:
        raise CensusError("缺少 GH_TOKEN / GITHUB_TOKEN（需要 contents: read 权限）")
    api_base = str(values.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    return repo, token, api_base


def _threshold(env: dict, name: str, default: int) -> int:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise CensusError(f"环境变量 {name} 不是整数：{raw!r}") from None


def census_counts(ref_names: list[str]) -> tuple[dict[str, int], Counter, int]:
    """把 ref 名列表分成（家族计数, HWM 按 issue 计数, 任务分支数）。"""
    family_counts: dict[str, int] = {prefix: 0 for prefix, _ in SCHEDULER_FAMILIES}
    hwm_per_issue: Counter = Counter()
    task_branches = 0
    for name in ref_names:
        matched = False
        for prefix, _label in SCHEDULER_FAMILIES:
            if name.startswith(prefix):
                family_counts[prefix] += 1
                matched = True
                if prefix == "refs/heads/agent-hwm/":
                    m = _HWM_ISSUE_RE.match(name)
                    if m:
                        hwm_per_issue[int(m.group(1))] += 1
                break
        if not matched and name.startswith(TASK_BRANCH_PREFIX):
            task_branches += 1
    return family_counts, hwm_per_issue, task_branches


def build_report(
    ref_names: list[str],
    *,
    warn_total: int,
    warn_family: int,
    warn_hwm_per_issue: int,
) -> tuple[list[str], list[str]]:
    """生成（报告行, 告警行）；告警非空即应以退出码 2 结束。"""
    family_counts, hwm_per_issue, task_branches = census_counts(ref_names)
    scheduler_total = sum(family_counts.values())
    lines = [
        f"[census] refs 普查：heads 总计 {len(ref_names)}，"
        f"调度 ref 合计 {scheduler_total}，任务分支 {task_branches}",
    ]
    for prefix, label in SCHEDULER_FAMILIES:
        lines.append(f"[census]   {label}：{family_counts[prefix]}")
    if hwm_per_issue:
        top = hwm_per_issue.most_common(5)
        lines.append(
            "[census]   HWM 代次最多的 issue："
            + "，".join(f"#{issue}={count}" for issue, count in top)
        )
    alerts: list[str] = []
    if scheduler_total > warn_total:
        alerts.append(
            f"调度 ref 合计 {scheduler_total} 超过预警线 {warn_total}"
            "（清理策略见 github-automation.md 第十一节）"
        )
    for prefix, label in SCHEDULER_FAMILIES:
        if family_counts[prefix] > warn_family:
            alerts.append(f"{label} 数量 {family_counts[prefix]} 超过预警线 {warn_family}")
    for issue, count in hwm_per_issue.items():
        if count > warn_hwm_per_issue:
            alerts.append(
                f"issue #{issue} 的 HWM 代次 {count} 超过预警线 {warn_hwm_per_issue}"
                "（HWM 只增不减，属预期增长；持续异常增长时按第十一节人工核查）"
            )
    return lines, alerts


def _write_step_summary(lines: list[str], alerts: list[str], env: dict) -> None:
    summary_path = str(env.get("GITHUB_STEP_SUMMARY") or "").strip()
    if not summary_path:
        return
    rows = "\n".join(f"- {line.removeprefix('[census] ')}" for line in lines)
    alert_md = (
        "\n\n**告警**：\n" + "\n".join(f"- ⚠️ {a}" for a in alerts) if alerts else ""
    )
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write(f"## refs 普查（#76）\n\n{rows}{alert_md}\n")


def main(
    argv: Optional[list[str]] = None,
    *,
    env: Optional[dict] = None,
    client=None,
    log=print,
) -> int:
    parser = argparse.ArgumentParser(description="调度 refs 普查与配额告警（#76）")
    parser.add_argument("--warn-total", type=int, default=None, help="调度 ref 合计预警线")
    parser.add_argument("--warn-family", type=int, default=None, help="单家族预警线")
    parser.add_argument(
        "--warn-hwm-per-issue", type=int, default=None, help="单 issue HWM 代次预警线"
    )
    args = parser.parse_args(argv)

    values = os.environ if env is None else env
    try:
        warn_total = args.warn_total or _threshold(values, ENV_WARN_TOTAL, DEFAULT_WARN_TOTAL)
        warn_family = args.warn_family or _threshold(
            values, ENV_WARN_FAMILY, DEFAULT_WARN_FAMILY
        )
        warn_hwm = args.warn_hwm_per_issue or _threshold(
            values, ENV_WARN_HWM_PER_ISSUE, DEFAULT_WARN_HWM_PER_ISSUE
        )
        if client is None:
            repo, token, api_base = env_config(values)
            client = UrllibGitHubClient(repo=repo, token=token, base_url=api_base)
        items = client.list_matching_refs("heads")
    except (CensusError, GitHubAPIError) as exc:
        print(f"[census] 失败：{exc}", file=sys.stderr)
        return 1

    ref_names = [str(item.get("ref") or "") for item in items if isinstance(item, dict)]
    lines, alerts = build_report(
        ref_names,
        warn_total=warn_total,
        warn_family=warn_family,
        warn_hwm_per_issue=warn_hwm,
    )
    for line in lines:
        log(line)
    for alert in alerts:
        log(f"[census] ALERT {alert}")
    _write_step_summary(lines, alerts, values)
    return 2 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
