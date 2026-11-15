"""真实 GitHub 多 checkout 验证（manual driver；默认不被 pytest 收集）。

owner 2026-11-14 点名验收项：在 fake server 之外，证明两个独立 checkout/进程通过
真实 GitHub API（隔离私有仓库）竞争时，ref-CAS 权威层仍然「同一任务只有一个有效
执行者」。安全：令牌只从 `GITHUB_TOKEN` 环境变量读（绝不打印/落盘）；只操作
`--repo` 指定仓库；结束删除本脚本创建的全部 ref 并删除/关闭测试 Issue。

`driver` 从 `CHECKOUT_A`/`CHECKOUT_B` 各起子进程（各自 cwd + PYTHONPATH，运行该
checkout 里的 `real_github_ref_worker.py`），编排场景 a–f，再用真实 REST API 读取
refs/评论/标签断言，证据写 `--log`。

用法::

    $env:GITHUB_TOKEN = (gh auth token)
    python real_github_ref_verify.py driver --repo Jouwala/noc-soc-ref-verify \\
        --log docs/workspace/multi-worker-regression/evidence/real-github-multivm-verify.log

退出码：全部通过 0；断言失败 1；环境不满足 2。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error, urllib.parse, urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api.github.com"
DEFAULT_REPO = "Jouwala/noc-soc-ref-verify"
PREFIX_HWM, PREFIX_LEASE, PREFIX_SCOPE, PREFIX_REAP = (
    "refs/heads/agent-hwm/", "refs/heads/agent-lease/",
    "refs/heads/agent-scope/", "refs/heads/agent-reap/",
)
_LEASE_RE = re.compile(
    r"^refs/heads/agent-lease/issue-(?P<issue>\d+)/(?P<token>\d{8})/"
    r"(?P<epoch>\d{13})/(?P<agent>[a-z0-9-]+)/(?P<instance>[a-f0-9]+)/(?P<attempt>[a-f0-9]+)$"
)


class Report:
    """证据日志：只记 ref 名/评论摘要/断言，不记 token 与鉴权头。"""

    def __init__(self, log_path: Path) -> None:
        self.lines: list[str] = []
        self.path, self.failures = log_path, []

    def log(self, message: str) -> None:
        line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {message}"
        self.lines.append(line)
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # Windows 控制台可能是 cp1252：日志文件仍保留中文
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.log(f"CHECK {name}: {'PASS' if ok else 'FAIL'} {detail}")
        if not ok:
            self.failures.append(name)

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        header = [
            "# 真实 GitHub 多 checkout 验证日志（敏感信息已排除：无 token / 无鉴权头）",
            f"# repo={os.environ.get('VERIFY_REPO', DEFAULT_REPO)} at={stamp}",
            f"# checkoutA={os.environ.get('CHECKOUT_A', 'D:/tmp/verify-a')} "
            f"checkoutB={os.environ.get('CHECKOUT_B', 'D:/tmp/verify-b')}",
            "",
        ]
        self.path.write_text("\n".join(header + self.lines) + "\n", encoding="utf-8")


class Gh:
    """最小 REST 客户端（driver 侧独立读取真实状态）。"""

    def __init__(self, repo: str, token: str) -> None:
        self.repo, self.token = repo, token

    def _open(self, method: str, path: str, payload=None):
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()

    def call(self, method: str, path: str, payload=None):
        status, _h, body = self._open(method, path, payload)
        text = body.decode("utf-8", "replace") if body else ""
        if not 200 <= status < 300:
            raise RuntimeError(f"GitHub API {method} {path} -> HTTP {status}: {text[:240]}")
        return json.loads(text) if text.strip() else None

    def paginate(self, path: str) -> list[dict]:
        url, items, seen = f"{API_BASE}{path}", [], set()
        while url:
            status, headers, body = self._open("GET", url)
            if not 200 <= status < 300:
                raise RuntimeError(f"GitHub 分页 GET {url} -> HTTP {status}")
            for item in json.loads(body.decode("utf-8")) if body.strip() else []:
                key = item.get("id") or item.get("ref")
                if key not in seen:
                    seen.add(key)
                    items.append(item)
            link = next((v for k, v in headers.items() if k.lower() == "link"), "")
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = match.group(1) if match else ""
        return items

    def ensure_label(self, name: str) -> None:
        status, _h, _b = self._open("POST", f"/repos/{self.repo}/labels",
                                    {"name": name, "color": "ededed"})
        if status not in (201, 422):  # 422 = 已存在
            raise RuntimeError(f"创建标签 {name} 失败：HTTP {status}")

    def create_issue(self, title: str, body: str = "") -> dict:
        return self.call("POST", f"/repos/{self.repo}/issues", {"title": title, "body": body})

    def comments(self, number: int) -> list[dict]:
        return self.paginate(f"/repos/{self.repo}/issues/{number}/comments?per_page=100")

    def labels(self, number: int) -> list[str]:
        issue = self.call("GET", f"/repos/{self.repo}/issues/{number}") or {}
        return [label["name"] for label in issue.get("labels", [])]

    def refs(self, prefix: str) -> list[str]:
        param = prefix[len("refs/"):] if prefix.startswith("refs/") else prefix
        items = self.paginate(
            f"/repos/{self.repo}/git/matching-refs/{urllib.parse.quote(param, safe='/')}")
        return [item["ref"] for item in items if item.get("ref", "").startswith(prefix)]

    def delete_ref(self, ref: str) -> bool:
        param = ref[len("refs/"):] if ref.startswith("refs/") else ref
        status, _h, _b = self._open(
            "DELETE", f"/repos/{self.repo}/git/refs/{urllib.parse.quote(param, safe='/')}")
        return status in (204, 200, 404, 422)

    def delete_issue(self, issue: dict) -> str:
        """优先 GraphQL deleteIssue（admin）；失败退化为关闭 Issue。"""
        node_id = issue.get("node_id")
        if node_id:
            status, _h, body = self._open(
                "POST", "/graphql",
                {"query": "mutation($id:ID!){deleteIssue(input:{issueId:$id}){clientMutationId}}",
                 "variables": {"id": node_id}})
            if 200 <= status < 300 and '"errors"' not in body.decode("utf-8", "replace"):
                return "deleted"
        status, _h, _b = self._open(
            "PATCH", f"/repos/{self.repo}/issues/{issue['number']}",
            {"state": "closed", "state_reason": "not_planned"})
        return "closed" if 200 <= status < 300 else f"close-failed-{status}"


# -- driver：子进程与 refs 断言工具 ------------------------------------------
def _worker_argv(script: Path, op: str, role: str, **kwargs) -> list[str]:
    argv = [sys.executable, str(script), "--op", op, "--role", role]
    for key, value in kwargs.items():
        if value is not None:
            argv.extend([f"--{key}", str(value)])
    return argv


def _child_env(checkout: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(checkout / "apps" / "control-plane" / "backend")
    env["PYTHONIOENCODING"] = "utf-8"
    env["VERIFY_OWNER"] = os.environ.get("VERIFY_OWNER", DEFAULT_REPO.split("/")[0])
    env["VERIFY_NAME"] = os.environ.get("VERIFY_NAME", DEFAULT_REPO.split("/")[1])
    return env


def _parse_result(stdout: str) -> dict:
    lines = [line for line in (stdout or "").splitlines() if line.startswith("RESULT:")]
    return json.loads(lines[-1][len("RESULT:"):]) if lines else {"ok": False, "error": "no-result"}


def _spawn(report: Report, checkout: Path, argv: list[str]) -> dict:
    report.log(f"SPAWN {checkout.name}: {' '.join(argv[2:])}")
    proc = subprocess.run(argv, cwd=str(checkout), env=_child_env(checkout),
                          capture_output=True, text=True, timeout=300)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT:"):
            report.log(f"  {line}")
    if proc.returncode != 0:
        report.log(f"  STDERR {proc.stderr[-400:]}")
        return {"ok": False, "error": f"exit-{proc.returncode}"}
    return _parse_result(proc.stdout)


def _spawn_pair(report, checkout_a: Path, argv_a: list[str],
                checkout_b: Path, argv_b: list[str], start_at: str) -> tuple[dict, dict]:
    """两个 checkout 的子进程**同时**启动（用同一 start-at 对齐操作时刻）。"""
    procs = []
    for checkout, argv in ((checkout_a, argv_a), (checkout_b, argv_b)):
        report.log(f"SPAWN(concurrent) {checkout.name}: {' '.join(argv[2:])} --start-at {start_at}")
        procs.append(subprocess.Popen(
            argv + ["--start-at", start_at], cwd=str(checkout), env=_child_env(checkout),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    results = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=300)
        for line in (stdout or "").splitlines():
            if line.startswith("RESULT:"):
                report.log(f"  {line}")
        if proc.returncode != 0:
            report.log(f"  STDERR {stderr[-400:]}")
        results.append(_parse_result(stdout))
    return results[0], results[1]


def _lease_refs(gh: Gh, issue: int) -> list[dict]:
    parsed = []
    for ref in gh.refs(PREFIX_LEASE + f"issue-{issue}/"):
        match = _LEASE_RE.match(ref)
        if match:
            parsed.append({"ref": ref, "token": int(match.group("token")),
                           "epoch": int(match.group("epoch"))})
    return parsed


def _hwm_tokens(gh: Gh, issue: int) -> list[int]:
    return sorted(int(m.group(1)) for ref in gh.refs(PREFIX_HWM + f"issue-{issue}/")
                  if (m := re.search(r"/(\d{8})$", ref)))


# -- driver：场景 a–f --------------------------------------------------------
def run_scenarios(report: Report, gh: Gh, checkout_a: Path, checkout_b: Path,
                  script_a: Path, script_b: Path, run_id: str, issues: list[dict]) -> None:
    # issues ? driver_main ??:??????,finally ?????????? Issue

    def new_issue(tag: str) -> dict:
        issue = gh.create_issue(
            f"[ref-verify] {tag} {run_id}",
            "自动化真实 GitHub 多 checkout 验证用 Issue。验证结束会删除/关闭。")
        issues.append(issue)
        report.log(f"ISSUE {tag} #{issue['number']}")
        return issue

    # a) 两 checkout 并发 claim 同一 issue：恰好 1 个成功
    issue_a = new_issue("concurrent-claim")
    result_a, result_b = _spawn_pair(
        report, checkout_a,
        _worker_argv(script_a, "claim", "a", issue=issue_a["number"], ttl=600),
        checkout_b,
        _worker_argv(script_b, "claim", "b", issue=issue_a["number"], ttl=600),
        str(time.time() + 2.0))
    winners = [role for role, res in (("a", result_a), ("b", result_b)) if res.get("ok")]
    report.check("a1 并发认领恰好一个成功", len(winners) == 1,
                 f"winners={winners} errors={{a:{result_a.get('error')},b:{result_b.get('error')}}}")
    loser_result = result_b if winners == ["a"] else result_a
    report.check("a2 失败者是 LeaseHeld", loser_result.get("error") == "LeaseHeld",
                 f"error={loser_result.get('error')}")
    refs_a = _lease_refs(gh, issue_a["number"])
    report.check("a3 真实 refs 只 1 条存活租约", len(refs_a) == 1, f"lease_refs={refs_a}")
    report.check("a4 HWM 恰好 token=1", _hwm_tokens(gh, issue_a["number"]) == [1],
                 f"hwm={_hwm_tokens(gh, issue_a['number'])}")
    winner_role = winners[0] if winners else "a"
    winner = result_a if winner_role == "a" else result_b
    winner_script = script_a if winner_role == "a" else script_b
    winner_checkout = checkout_a if winner_role == "a" else checkout_b
    token, attempt = int(winner["token"]), winner["attempt"]

    # b) 旧 token 的 renew / complete 必须 StaleFencing
    renewed = _spawn(report, winner_checkout, _worker_argv(
        winner_script, "renew", winner_role, issue=issue_a["number"],
        attempt=attempt, token=token))
    report.check("b1 赢家续租推进 token",
                 bool(renewed.get("ok")) and int(renewed.get("token", 0)) == token + 1,
                 f"renew={renewed}")
    stale_renew = _spawn(report, winner_checkout, _worker_argv(
        winner_script, "renew", winner_role, issue=issue_a["number"],
        attempt=attempt, token=token))
    report.check("b2 旧 token renew 被 StaleFencing 拒绝",
                 stale_renew.get("error") == "StaleFencing", f"{stale_renew}")
    stale_complete = _spawn(report, winner_checkout, _worker_argv(
        winner_script, "complete", winner_role, issue=issue_a["number"],
        attempt=attempt, token=token, key=f"stale-{run_id}"))
    report.check("b3 旧 token complete 被 StaleFencing 拒绝",
                 stale_complete.get("error") == "StaleFencing", f"{stale_complete}")
    refs_a2 = _lease_refs(gh, issue_a["number"])
    report.check("b4 推进后仍只 1 条租约且 token=2",
                 len(refs_a2) == 1 and refs_a2[0]["token"] == token + 1, f"{refs_a2}")
    # 释放 issue-2 的租约，让场景 c 的 Reaper 只看到 issue-3 的过期租约
    released_a = _spawn(report, winner_checkout, _worker_argv(
        winner_script, "release", winner_role, issue=issue_a["number"],
        attempt=attempt, token=token + 1))
    report.check("b5 主动释放租约成功", bool(released_a.get("ok")), f"{released_a}")
    report.check("b6 释放后 issue-2 无 lease ref", _lease_refs(gh, issue_a["number"]) == [],
                 f"{_lease_refs(gh, issue_a['number'])}")

    # c) 短 TTL 过期 → Reaper CAS 回收 → 另一 checkout 重新认领
    issue_c = new_issue("expiry-reap")
    claimed_c = _spawn(report, checkout_a, _worker_argv(
        script_a, "claim", "a", issue=issue_c["number"], ttl=2))
    report.check("c1 短 TTL 认领成功", bool(claimed_c.get("ok")), f"{claimed_c}")
    time.sleep(3.0)
    backend = Path(__file__).resolve().parents[2]
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from scheduler.factory import make_ref_store  # noqa: PLC0415
    from scheduler.ref_reap import reap_expired_leases  # noqa: PLC0415

    reap_records = reap_expired_leases(
        make_ref_store(gh.repo, token=gh.token), int(time.time() * 1000), 2000,
        reaper_instance=f"driver-{run_id[:6]}")
    reaped = [r for r in reap_records
              if r.get("outcome") == "reaped" and r["issue_number"] == issue_c["number"]]
    report.check("c2 Reaper CAS 回收过期租约（仅 issue-3 一条）",
                 len(reaped) == 1 and len(reap_records) == 1, f"records={reap_records}")
    report.check("c3 回收后 lease ref 已删", _lease_refs(gh, issue_c["number"]) == [],
                 f"{_lease_refs(gh, issue_c['number'])}")
    report.check("c4 HWM 保留（token 不复用）", _hwm_tokens(gh, issue_c["number"]) == [1],
                 f"hwm={_hwm_tokens(gh, issue_c['number'])}")
    reclaim = _spawn(report, checkout_b, _worker_argv(
        script_b, "claim", "b", issue=issue_c["number"], ttl=600))
    report.check("c5 另一 checkout 重新认领成功且 token=2",
                 bool(reclaim.get("ok")) and int(reclaim.get("token", 0)) == 2, f"{reclaim}")

    # d) 重叠 scope 只 1 成功；不重叠都成功
    issue_d1, issue_d2 = new_issue("scope-overlap-1"), new_issue("scope-overlap-2")
    overlap = "src/shared/overlap.py"
    ds1 = _spawn(report, checkout_a, _worker_argv(
        script_a, "claim_scope", "a", issue=issue_d1["number"], scopes=overlap, ttl=600))
    report.check("d1 任务1 认领+获取重叠 scope 成功", bool(ds1.get("ok")), f"{ds1}")
    ds2 = _spawn(report, checkout_b, _worker_argv(
        script_b, "claim_scope", "b", issue=issue_d2["number"], scopes=overlap, ttl=600))
    report.check("d2 任务2 认领成功但 scope 冲突",
                 bool(ds2.get("token")) and ds2.get("error") == "ScopeConflict", f"{ds2}")
    ds2b = _spawn(report, checkout_b, _worker_argv(
        script_b, "acquire_scope", "b", issue=issue_d2["number"],
        attempt=ds2.get("attempt"), token=ds2.get("token"), scopes="docs/guide.md"))
    report.check("d3 不重叠 scope 并发成功", bool(ds2b.get("ok")), f"{ds2b}")
    scope_refs = gh.refs(PREFIX_SCOPE)
    report.check("d4 真实 scope refs 记录了两把锁",
                 any("overlap.py" in ref for ref in scope_refs)
                 and any("guide.md" in ref for ref in scope_refs), f"scope_refs={scope_refs}")
    rs1 = _spawn(report, checkout_a, _worker_argv(
        script_a, "release_scope", "a", issue=issue_d1["number"],
        attempt=ds1.get("attempt"), token=ds1.get("token")))
    rs2 = _spawn(report, checkout_b, _worker_argv(
        script_b, "release_scope", "b", issue=issue_d2["number"],
        attempt=ds2b.get("attempt") or ds2.get("attempt"),
        token=ds2b.get("token") or ds2.get("token")))
    report.check("d5 释放 scope 成功",
                 bool(rs1.get("released")) and bool(rs2.get("released")),
                 f"release1={rs1} release2={rs2}")
    scope_after = gh.refs(PREFIX_SCOPE)
    report.check("d6 释放后 scope refs 清空",
                 not any(f"issue-{issue_d1['number']}" in ref for ref in scope_after)
                 and not any(f"issue-{issue_d2['number']}" in ref for ref in scope_after),
                 f"{scope_after}")

    # e) 完成幂等：重复 complete 只 1 条 completion 评论 + 标签收敛
    issue_e = new_issue("completion-idempotent")
    flow = _spawn(report, checkout_a, _worker_argv(
        script_a, "complete_flow", "a", issue=issue_e["number"],
        key=f"complete-{run_id}", ttl=600))
    report.check("e1 完成流程成功且重复提交被幂等吸收",
                 bool(flow.get("ok")) and flow.get("created") == [True, False], f"{flow}")
    marker = f"ref-completion-id: complete-{run_id}"
    bodies = [comment.get("body", "") for comment in gh.comments(issue_e["number"])]
    report.check("e2 只有 1 条完成标记评论",
                 sum(1 for body in bodies if marker in body) == 1,
                 f"completion_comments={sum(1 for body in bodies if marker in body)}")
    labels = gh.labels(issue_e["number"])
    report.check("e3 标签收敛为 state:in-review", "state:in-review" in labels,
                 f"labels={labels}")
    report.check("e4 完成后 lease ref 已删", _lease_refs(gh, issue_e["number"]) == [],
                 f"{_lease_refs(gh, issue_e['number'])}")

    # g) 真实 API 分页 / 422 语义：page_size=2 强制两页，重复 create 必须返回 False
    issue_g = new_issue("pagination-422")
    from scheduler.github_refs import RestRefStore  # noqa: PLC0415

    store = RestRefStore(gh.repo, token=gh.token, timeout=30.0, page_size=2,
                         max_pages=10, sleep=time.sleep)
    anchor = store.branch_sha("main")
    prefix_g = f"refs/heads/agent-hwm/issue-{issue_g['number']}/"
    for candidate in (1, 2, 3):
        store.create(f"{prefix_g}{candidate:08d}", anchor)
    listed = [info.ref for info in store.list(prefix_g)]
    duplicate = store.create(f"{prefix_g}00000001", anchor)
    report.check("g1 真实 matching-refs 分页返回全部 ref（page_size=2）",
                 len(listed) == 3, f"listed={listed}")
    report.check("g2 真实 422=已存在→False（POST 不重试）", duplicate is False,
                 f"duplicate={duplicate}")
    return None


def driver_main(args) -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("缺少 GITHUB_TOKEN（只从环境变量读取）", file=sys.stderr)
        return 2
    owner, _, name = (args.repo or DEFAULT_REPO).partition("/")
    if not owner or not name:
        print(f"--repo 必须是 owner/name：{args.repo!r}", file=sys.stderr)
        return 2
    os.environ.setdefault("VERIFY_OWNER", owner)
    os.environ.setdefault("VERIFY_NAME", name)
    checkout_a = Path(os.environ.get("CHECKOUT_A", r"D:\tmp\verify-a")).resolve()
    checkout_b = Path(os.environ.get("CHECKOUT_B", r"D:\tmp\verify-b")).resolve()
    manual = "apps/control-plane/backend/tests/manual"
    script_a = checkout_a / manual / "real_github_ref_worker.py"
    script_b = checkout_b / manual / "real_github_ref_worker.py"
    for script in (script_a, script_b):
        if not script.exists():
            print(f"checkout 里缺少 worker 脚本：{script}", file=sys.stderr)
            return 2
    report = Report(Path(args.log).resolve())
    gh = Gh(f"{owner}/{name}", token)
    run_id = uuid.uuid4().hex[:8]
    report.log(f"DRIVER start repo={owner}/{name} run_id={run_id}")
    for label in ("state:todo", "state:running", "state:in-review", "state:blocked"):
        gh.ensure_label(label)
    issues: list[dict] = []
    try:
        run_scenarios(report, gh, checkout_a, checkout_b, script_a, script_b, run_id, issues)
    finally:
        created_numbers = [issue["number"] for issue in issues]
        report.log("CLEANUP 删除测试 ref")
        for prefix in (PREFIX_HWM, PREFIX_LEASE, PREFIX_SCOPE, PREFIX_REAP):
            for ref in gh.refs(prefix):
                if any(f"issue-{number}/" in ref for number in created_numbers):
                    report.log(f"  delete {ref} -> {gh.delete_ref(ref)}")
        report.log("CLEANUP 删除/关闭测试 Issue")
        for issue in issues:
            report.log(f"  issue #{issue['number']} -> {gh.delete_issue(issue)}")
        remaining = [ref for prefix in (PREFIX_HWM, PREFIX_LEASE, PREFIX_SCOPE)
                     for ref in gh.refs(prefix)
                     if any(f"issue-{number}/" in ref for number in created_numbers)]
        report.check("f1 测试 refs 已清理", not remaining, f"remaining={remaining}")
        report.log(f"DRIVER done failures={report.failures}")
        report.flush()
    return 0 if not report.failures else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="真实 GitHub 多 checkout ref 权威层验证")
    parser.add_argument("mode", nargs="?", default="driver", choices=("driver",))
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--log", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    return driver_main(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
