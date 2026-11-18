"""租约 / 孤儿范围锁回收 CLI（ADR-7 §3.7 / §八 PR-3）—— GitHub Actions cron 入口。

ADR-7 §零 要求「Reaper 抢过期租约也必须用 ref-CAS」：过期判定 + 创建
`agent-reap/issue-<N>/<token>/<reaper>` 回收认领 ref（201=唯一赢家）+ 删除过期 lease ref
由权威层核心的 `scheduler/ref_reap.py` 实现（PR-3 另一部分）。本文件只做 CLI 编排：

1. 调 `ref_reap.reap_expired_leases(...)` 回收过期租约（HWM 一律不删）；
2. 调 `ref_scope.cleanup_orphans(...)` 清理「所属任务无存活租约」的孤儿 scope ref；
3. 可选 `--fix-labels`：租约已过期且无完成审计标记时，把残留 `state:claimed` 收敛成
   `state:todo`（展示层对账，失败只告警，不影响权威互斥）；
4. 可选 `--claim-expiry`：认领后 4 小时无进展的任务发 `CLAIM_EXPIRED` 评论并把标签
   收敛回 `state:todo`（`claim_expiry.py`，lease-reaper workflow 每 5 分钟跑一次）；
5. 幂等、可重复执行；失败显式：缺 `REPO`/`GH_TOKEN`、ref_reap 未就绪、refs API 报错
   → stderr 打印原因 + 退出码非 0。

用法：
```bash
REPO=owner/name GH_TOKEN=... python apps/control-plane/backend/dispatch/lease_reaper.py
# 可选：--dry-run / --fix-labels / --claim-expiry / --ttl-seconds N / --reaper-instance <hex>
```
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Optional

# 以脚本方式运行时（Actions 里的 `python apps/.../lease_reaper.py`）sys.path[0]
# 是 dispatch/，调度器包不在路径上；把 backend/ 放进去后统一按标准包导入。
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from scheduler import ref_api  # noqa: E402
from scheduler.api import utcnow  # noqa: E402

from dispatch.claim_expiry import (  # noqa: E402
    DEFAULT_CLAIM_TTL,
    ENV_CLAIM_TTL_HOURS,
    expire_stale_claims,
)

try:  # 作为包导入（pytest: dispatch.lease_reaper）
    from .github_client import UrllibGitHubClient
except ImportError:  # 作为脚本直接运行（__package__ 为空）
    from github_client import UrllibGitHubClient  # type: ignore[no-redef]

DEFAULT_TTL = ref_api.DEFAULT_REF_LEASE_TTL
ENV_TTL_SECONDS = "AGENT_LEASE_TTL_SECONDS"
ENV_REAPER_INSTANCE = "AGENT_REAPER_INSTANCE"

#: 租约过期后标签应收敛到「可再派」；只动这一个状态标签，不碰 QA/合并等。
STATE_CLAIMED = "state:claimed"
STATE_TODO = "state:todo"

_ISSUE_IN_REF_RE = re.compile(r"/issue-(?P<issue>\d+)/")


class ReaperError(RuntimeError):
    """配置 / 依赖错误（退出码 2，与「回收执行失败」的退出码 1 区分）。"""


# ---------------------------------------------------------------------------
# 时间 / 环境
# ---------------------------------------------------------------------------


def _now_ms(clock) -> int:
    return int((clock or utcnow)().timestamp() * 1000)


def _ttl_ms(ttl) -> int:
    return ref_api.ttl_ms(ttl or DEFAULT_TTL)


def env_config(env: Optional[dict] = None) -> tuple[str, str, str]:
    """从 Actions 环境变量取 (repo, token, api_base)；缺失时抛清晰错误。"""
    values = os.environ if env is None else env
    repo = str(values.get("REPO") or values.get("GITHUB_REPOSITORY") or "").strip()
    if not repo or "/" not in repo:
        raise ReaperError("缺少 REPO 环境变量（形如 owner/name）")
    token = str(values.get("GH_TOKEN") or values.get("GITHUB_TOKEN") or "").strip()
    if not token:
        raise ReaperError("缺少 GH_TOKEN / GITHUB_TOKEN（需要 contents: write 权限）")
    api_base = str(values.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    return repo, token, api_base


# ---------------------------------------------------------------------------
# 1) 过期租约：委托权威层核心的 ref_reap（CAS 回收认领 + 删 lease ref）
# ---------------------------------------------------------------------------


def _load_ref_reap():
    """延迟加载 `scheduler/ref_reap.py`（PR-3 由权威层核心落地）。"""
    try:
        from scheduler import ref_reap  # noqa: PLC0415 - 可选依赖：并行落地
    except ImportError as exc:
        raise ReaperError(
            "ref 回收实现 scheduler/ref_reap.py 尚不可用（PR-3 由权威层核心提供）："
            f"{exc}"
        ) from exc
    return ref_reap


def _load_cleanup_orphans():
    """孤儿 scope 清理来自 `scheduler/ref_scope.py`（PR-4）；延迟导入保持 PR-3 可独立合入。

    PR-3 只依赖 PR-1/2。若 PR-4 尚未合入，这里**降级为跳过孤儿清理**并打印警告，
    而不是让 5 分钟一次的 cron Reaper 整体失败（否则 PR-3 与 PR-4 合并之间会出现
    持续红灯）；PR-4 合入后自动生效，无需改 CLI。
    """
    try:
        from scheduler.ref_scope import cleanup_orphans  # noqa: PLC0415
    except ImportError:
        return None
    return cleanup_orphans


def reap_expired_lease_refs(
    ref_store,
    *,
    now_ms: Optional[int] = None,
    ttl_ms: Optional[int] = None,
    reaper_instance: str = "",
    dry_run: bool = False,
    ref_reap=None,
) -> tuple[list[dict], tuple[str, ...]]:
    """调 ref_reap 回收过期租约；返回 (逐条结果 list[dict], 已删 lease ref 元组)。

    冻结接口（ADR-7 PR-3，Lead 2026-11-14 确认）：
    `reap_expired_leases(store, now_ms, ttl_ms, *, reaper_instance, dry_run=False)
    -> list[dict]`，dict 含 `issue_number, token, epoch_ms, instance_id, agent_id,
    ref, outcome`（outcome = "reaped"/"skipped"）。过期判定与 CAS 认领都在 ref_reap，
    CLI 不重复实现。
    """
    module = ref_reap if ref_reap is not None else _load_ref_reap()
    func = getattr(module, "reap_expired_leases", None)
    if not callable(func):
        raise ReaperError(
            "ref_reap.reap_expired_leases 不存在：PR-3 权威层实现需提供该入口"
        )
    results = func(
        ref_store,
        _now_ms(None) if now_ms is None else int(now_ms),
        _ttl_ms(None) if ttl_ms is None else int(ttl_ms),
        reaper_instance=reaper_instance,
        dry_run=dry_run,
    )
    rows = [dict(row) for row in (results or [])]
    reaped = tuple(
        str(row.get("ref") or "") for row in rows if row.get("outcome") == "reaped"
    )
    return rows, reaped


# ---------------------------------------------------------------------------
# 2) 可选：状态标签对账（失败只告警）
# ---------------------------------------------------------------------------


class LabelAdapter:
    """标签对账需要的三个动作；真实实现见 `DispatchLabelAdapter`。"""

    def list_comment_bodies(self, issue_number: int) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def get_labels(self, issue_number: int) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def set_labels(self, issue_number: int, labels: list[str]) -> None:  # pragma: no cover
        raise NotImplementedError


class DispatchLabelAdapter(LabelAdapter):
    """用 `dispatch/github_client.UrllibGitHubClient` 实现标签对账（标准库 urllib）。"""

    def __init__(self, client: UrllibGitHubClient) -> None:
        self.client = client

    def list_comment_bodies(self, issue_number: int) -> list[str]:
        return [
            str(item.get("body") or "")
            for item in self.client.list_issue_comments(issue_number)
        ]

    def get_labels(self, issue_number: int) -> list[str]:
        issue = self.client.get_issue(issue_number)
        return [
            str(item.get("name") or "")
            for item in (issue.get("labels") or [])
            if isinstance(item, dict)
        ]

    def set_labels(self, issue_number: int, labels: list[str]) -> None:
        current = self.get_labels(issue_number)
        to_add = [label for label in labels if label not in current]
        to_remove = [label for label in current if label not in labels]
        if to_add:
            self.client.add_issue_labels(issue_number, to_add)
        for label in to_remove:
            self.client.remove_issue_label(issue_number, label)


def repair_labels(adapter: LabelAdapter, issue_numbers, *, log=print) -> list[str]:
    """对「租约已过期」的 issue 收敛 state 标签；返回告警（不抛）。"""
    warnings: list[str] = []
    for issue in sorted({int(number) for number in issue_numbers}):
        try:
            # 有完成审计标记 ⇒ 任务其实已结束，标签由 mirror/人工负责，不动
            if ref_api.find_completion_marker(adapter.list_comment_bodies(issue)):
                continue
            labels = adapter.get_labels(issue)
            if STATE_CLAIMED not in labels:
                continue
            updated = [label for label in labels if label != STATE_CLAIMED]
            if STATE_TODO not in updated:
                updated.append(STATE_TODO)
            adapter.set_labels(issue, updated)
            log(f"[reaper] issue #{issue} 标签修正：{labels} → {updated}")
        except Exception as exc:  # noqa: BLE001 - 展示层对账失败只告警，不影响回收
            warnings.append(f"issue #{issue} 标签修正失败：{exc!r}")
    return warnings


# ---------------------------------------------------------------------------
# 3) 汇总入口
# ---------------------------------------------------------------------------


def _issue_from_ref(ref: str) -> Optional[int]:
    match = _ISSUE_IN_REF_RE.search(str(ref or ""))
    if match:
        return int(match.group("issue"))
    parsed = ref_api.parse_scope_ref(str(ref or ""))
    if parsed:
        return int(parsed["task_id"].split("-", 1)[1])
    return None


def reap(
    ref_store,
    *,
    label_adapter: Optional[LabelAdapter] = None,
    clock=None,
    ttl=None,
    dry_run: bool = False,
    fix_labels: bool = False,
    reaper_instance: str = "",
    ref_reap=None,
    log=print,
) -> dict:
    """执行一轮回收；返回报告字典（幂等：第二次运行各列表为空）。"""
    reaped_rows, reaped_refs = reap_expired_lease_refs(
        ref_store,
        now_ms=_now_ms(clock),
        ttl_ms=_ttl_ms(ttl),
        reaper_instance=reaper_instance,
        dry_run=dry_run,
        ref_reap=ref_reap,
    )
    cleanup_orphans = _load_cleanup_orphans()
    if cleanup_orphans is None:
        log(
            "[reaper] 警告：scheduler/ref_scope.py 未就绪（PR-4 未合入），本轮跳过孤儿范围锁清理"
        )
        orphans: list[str] = []
    else:
        orphans = cleanup_orphans(ref_store, clock=clock, ttl=ttl, dry_run=dry_run)

    # 标签对账只针对「本轮动过」的 issue（过期租约 + 孤儿 scope）
    affected: set[int] = set()
    for row in reaped_rows:
        if row.get("outcome") == "reaped" and row.get("issue_number") is not None:
            affected.add(int(row["issue_number"]))
    for ref in orphans:
        issue = _issue_from_ref(ref)
        if issue is not None:
            affected.add(issue)

    warnings: list[str] = []
    if fix_labels and label_adapter is not None and affected:
        warnings = repair_labels(label_adapter, affected, log=log)

    skipped = [row for row in reaped_rows if row.get("outcome") != "reaped"]
    summary = {
        "reaped_leases": tuple(reaped_refs),
        "skipped": tuple(skipped),
        "orphan_scopes": tuple(orphans),
        "orphan_scope_cleanup": "done" if cleanup_orphans is not None else "skipped",
        "label_warnings": warnings,
        "dry_run": dry_run,
    }
    log(
        f"[reaper] 完成：过期租约回收 {len(reaped_refs)} 条，"
        f"跳过 {len(skipped)} 条，孤儿范围锁 {len(orphans)} 条"
        + ("，标签告警 " + str(len(warnings)) + " 条" if warnings else "")
        + ("（dry-run 未实际删除）" if dry_run else "")
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_reaper_instance(values: dict) -> str:
    """Reaper 身份（写进 agent-reap ref）：优先环境变量，否则随机 hex。"""
    raw = str(values.get(ENV_REAPER_INSTANCE) or "").strip()
    return raw or uuid.uuid4().hex[:12]


def _build_client(repo: str, token: str, api_base: str) -> UrllibGitHubClient:
    return UrllibGitHubClient(repo=repo, token=token, base_url=api_base)


def _build_label_adapter(repo: str, token: str, api_base: str) -> LabelAdapter:
    return DispatchLabelAdapter(_build_client(repo, token, api_base))


def _claim_ttl(arg_value: Optional[float], values: dict) -> timedelta:
    """认领租约时长：命令行 > 环境变量 > 协议默认 4 小时；非正数即配置错误。"""
    raw = str(arg_value) if arg_value is not None else str(
        values.get(ENV_CLAIM_TTL_HOURS) or ""
    ).strip()
    if not raw:
        return DEFAULT_CLAIM_TTL
    try:
        hours = float(raw)
    except ValueError as exc:
        raise ReaperError(
            f"{ENV_CLAIM_TTL_HOURS} / --claim-ttl-hours 必须是正数，实际 {raw!r}"
        ) from exc
    if hours <= 0:
        raise ReaperError(
            f"{ENV_CLAIM_TTL_HOURS} / --claim-ttl-hours 必须是正数，实际 {raw!r}"
        )
    return timedelta(hours=hours)


def _write_github_output(name: str, value: str, values: dict | None = None) -> None:
    """把结果写进 $GITHUB_OUTPUT（供 workflow 后续步骤判断；本地运行无此变量时静默跳过）。"""
    env = values if values is not None else os.environ
    path = str(env.get("GITHUB_OUTPUT") or "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")
    except OSError as exc:
        # 写失败不影响回收本身，但要留一行日志：否则 reaped 输出为空，
        # 下游分派步静默跳过（"" > 0 为假），被释放的任务无声等到下一小时巡检。
        print(f"[reaper] 写 GITHUB_OUTPUT 失败（{name}={value}）：{exc}", flush=True)


def main(
    argv: Optional[list[str]] = None,
    *,
    env: Optional[dict] = None,
    ref_store=None,
    label_adapter: Optional[LabelAdapter] = None,
    ref_reap=None,
    github_client: Optional[UrllibGitHubClient] = None,
    log=print,
) -> int:
    parser = argparse.ArgumentParser(description="GitHub ref 权威层租约回收（ADR-7）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不实际删除")
    parser.add_argument(
        "--fix-labels", action="store_true", help="按过期租约修正 state 标签（失败只告警）"
    )
    parser.add_argument(
        "--claim-expiry",
        action="store_true",
        help="认领超时过期：state:claimed 超过租约无进展 → 发 CLAIM_EXPIRED、标签回 state:todo",
    )
    parser.add_argument(
        "--claim-ttl-hours",
        type=float,
        default=None,
        help=f"认领租约小时数（默认环境变量 {ENV_CLAIM_TTL_HOURS} 或 4）",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        help=f"租约 TTL 秒数（默认环境变量 {ENV_TTL_SECONDS} 或 {int(DEFAULT_TTL.total_seconds())}）",
    )
    parser.add_argument(
        "--reaper-instance",
        default="",
        help=f"Reaper 身份（写进 agent-reap ref；默认环境变量 {ENV_REAPER_INSTANCE} 或随机 hex）",
    )
    args = parser.parse_args(argv)

    values = os.environ if env is None else env
    try:
        repo, token, api_base = env_config(values)
    except ReaperError as exc:
        print(f"[reaper] 配置错误：{exc}", file=sys.stderr)
        return 2

    ttl = DEFAULT_TTL
    if args.ttl_seconds is not None:
        if args.ttl_seconds <= 0:
            print("[reaper] 配置错误：--ttl-seconds 必须是正整数", file=sys.stderr)
            return 2
        ttl = timedelta(seconds=args.ttl_seconds)
    else:
        raw_ttl = str(values.get(ENV_TTL_SECONDS) or "").strip()
        if raw_ttl:
            if not raw_ttl.isdigit() or int(raw_ttl) <= 0:
                print(
                    f"[reaper] 配置错误：{ENV_TTL_SECONDS} 必须是正整数，实际 {raw_ttl!r}",
                    file=sys.stderr,
                )
                return 2
            ttl = timedelta(seconds=int(raw_ttl))

    reaper_instance = args.reaper_instance or _default_reaper_instance(values)

    try:
        if ref_store is None:
            from scheduler.factory import make_ref_store  # noqa: PLC0415 - 延迟导入

            ref_store = make_ref_store(repo, token=token, api_base=api_base)
        if label_adapter is None and args.fix_labels:
            label_adapter = _build_label_adapter(repo, token, api_base)
        summary = reap(
            ref_store,
            label_adapter=label_adapter,
            ttl=ttl,
            dry_run=args.dry_run,
            fix_labels=args.fix_labels,
            reaper_instance=reaper_instance,
            ref_reap=ref_reap,
            log=log,
        )
        # 供 workflow 判断「本轮是否释放了任务」：>0 时触发一次自动分派。
        # 只写数字，不写 Issue 编号（编号在日志里已有）。
        reaped_count = len(summary.get("reaped_leases") or ()) + len(
            summary.get("orphan_scopes") or ()
        )
        _write_github_output("reaped", str(reaped_count), values)
        if args.claim_expiry:
            claim_ttl = _claim_ttl(args.claim_ttl_hours, values)
            client = github_client or _build_client(repo, token, api_base)
            expire_stale_claims(
                client, claim_ttl=claim_ttl, dry_run=args.dry_run, log=log
            )
    except (ReaperError, ValueError) as exc:
        print(f"[reaper] 配置错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 失败必须显式：非零退出 + 原因
        print(f"[reaper] 回收失败：{exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
