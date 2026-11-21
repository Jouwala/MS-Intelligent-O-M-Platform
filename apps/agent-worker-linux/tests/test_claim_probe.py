"""CLI 集成：`--claim-probe` 单行 JSON / `--check` / `verify_concurrent_claim.py`。"""

from __future__ import annotations

import json
import subprocess
import sys

from _linux_fixtures import fake_github  # noqa: F401 - pytest 夹具
from _linux_testkit import PACKAGE_DIR, REPO_ROOT, config_mapping, make_body, write_config

WORKER_CLI = PACKAGE_DIR / "worker_linux.py"
VERIFY_CLI = PACKAGE_DIR / "verify_concurrent_claim.py"
REQUIRED_FIELDS = (
    "ok",
    "result",
    "worker_id",
    "instance",
    "attempt",
    "token",
    "hwm_refs",
    "lease_refs",
    "refs_error",
)


def _run(args, timeout=180):
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(PACKAGE_DIR),
    )


def _write_config(tmp_path, server, worker_id="muse-1-linux", instance=None):
    raw = config_mapping(server, worker_id, instance, tmp_path)
    return write_config(tmp_path / f"config-{worker_id}.json", raw)


def _add_issue(server, number=1, *, agent="muse-1-linux"):
    server.add_issue(
        number,
        labels=(f"agent:{agent}", "state:todo"),
        title=f"任务 {number}",
        body=make_body(command=["true"]),
    )


def _refs(server, prefix):
    return [ref for ref in server.refs_snapshot() if ref.startswith(prefix)]


def test_claim_probe_cli_outputs_single_line_json_with_refs(fake_github, tmp_path):
    _add_issue(fake_github)
    config_path = _write_config(tmp_path, fake_github)
    result_file = tmp_path / "probe.json"

    proc = _run(
        [
            str(WORKER_CLI),
            "--config",
            str(config_path),
            "--claim-probe",
            "--issue",
            "1",
            "--result-file",
            str(result_file),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout 必须是单行 JSON：{proc.stdout!r}"
    payload = json.loads(lines[0])
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    assert not missing, missing
    assert payload["result"] == "claimed"
    assert payload["token"] == 1
    assert payload["refs_error"] == ""
    assert payload["hwm_refs"] and payload["lease_refs"]
    assert payload["config_path"] == str(config_path)
    assert result_file.read_text(encoding="utf-8").strip() == lines[0]

    assert len(_refs(fake_github, "refs/heads/agent-hwm/issue-1/")) == 1
    assert len(_refs(fake_github, "refs/heads/agent-lease/issue-1/")) == 1
    # 配置里的 token 值不得出现在任何输出里
    assert "test-token" not in proc.stdout + proc.stderr


def test_claim_probe_second_process_reports_lease_held(fake_github, tmp_path):
    _add_issue(fake_github)
    config_a = _write_config(tmp_path, fake_github, "muse-1-linux")
    config_b = _write_config(tmp_path, fake_github, "muse-2-linux")

    first = _run([str(WORKER_CLI), "--config", str(config_a), "--claim-probe", "--issue", "1"])
    second = _run([str(WORKER_CLI), "--config", str(config_b), "--claim-probe", "--issue", "1"])
    assert first.returncode == 0 and second.returncode == 0
    first_payload = json.loads([line for line in first.stdout.splitlines() if line.strip()][-1])
    second_payload = json.loads([line for line in second.stdout.splitlines() if line.strip()][-1])
    assert first_payload["result"] == "claimed"
    assert second_payload["result"] == "LeaseHeld"
    assert _refs(fake_github, "refs/heads/agent-lease/issue-1/")
    assert len(_refs(fake_github, "refs/heads/agent-hwm/issue-1/")) == 1


def test_check_cli_reports_identity_and_refs(fake_github, tmp_path):
    config_path = _write_config(tmp_path, fake_github)
    proc = _run([str(WORKER_CLI), "--config", str(config_path), "--check"])
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, proc.stdout
    payload = json.loads(lines[0])
    assert payload["ok"] is True and payload["refs_ok"] is True
    assert payload["main_sha"] == fake_github.anchor_sha
    assert payload["identity"]["worker_id"] == "muse-1-linux"
    assert payload["identity"]["instance"] == "muse-1-linux"
    assert payload["auth_mode"] == "config"
    assert payload["hostname"]
    assert "test-token" not in proc.stdout + proc.stderr


def test_check_cli_rejects_invalid_config_with_exit_2(fake_github, tmp_path):
    raw = config_mapping(fake_github, "muse-1-linux", None, tmp_path)
    raw["repo_root"] = str(tmp_path / "not-a-repo")
    config_path = write_config(tmp_path / "bad.json", raw)
    proc = _run([str(WORKER_CLI), "--config", str(config_path), "--check"])
    assert proc.returncode == 2
    assert "config-error" in proc.stderr


def test_verify_concurrent_claim_tool_and_cleanup(fake_github, tmp_path):
    _add_issue(fake_github)
    config_a = _write_config(tmp_path, fake_github, "muse-1-linux")
    config_b = _write_config(tmp_path, fake_github, "muse-2-linux")
    winner_file = tmp_path / "winner.json"

    proc = _run(
        [
            str(VERIFY_CLI),
            "--issue",
            "1",
            "--config-a",
            str(config_a),
            "--config-b",
            str(config_b),
            "--winner-file",
            str(winner_file),
        ]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout
    assert "agent-hwm/issue-1" in proc.stdout
    assert "LeaseHeld" in proc.stdout
    assert winner_file.is_file()
    winner = json.loads(winner_file.read_text(encoding="utf-8"))
    assert winner["result"] == "claimed"
    assert _refs(fake_github, "refs/heads/agent-lease/issue-1/")

    cleanup = _run([str(VERIFY_CLI), "--cleanup-winner", str(winner_file)])
    # HWM（high-water mark）ref 是 token 单调性的真相源，**只增不减**
    # （见 scheduler/ref_api.py 的核心不变量）。并发认领时输家也会先做一次
    # HWM CAS、把 token 推进到赢家之上，于是赢家用自己那份较旧的 token 去
    # release 会拿到 StaleFencing —— 这是**刻意的防分裂脑行为**（旧执行者
    # 不得再改状态），不是缺陷；这两种结局都合法：
    #   rc=0            → 赢家 token 仍是最高代次，租约直接释放；
    #   StaleFencing    → 输家已把 HWM 推高，赢家被正确围栏，租约留给 reaper。
    # 原先这里无条件要求 rc=0 且断言 HWM 恰好 1 条，在后者时序下随机失败
    # （CI 同一提交一次过、一次挂）。
    if cleanup.returncode == 0:
        assert _refs(fake_github, "refs/heads/agent-lease/issue-1/") == []
    else:
        assert "StaleFencing" in (cleanup.stderr or ""), cleanup.stdout + cleanup.stderr
        # 被围栏说明 HWM 已越过赢家的 token；赢家那份 HWM 记录仍然在（只增不减）。
        lease_refs = _refs(fake_github, "refs/heads/agent-lease/issue-1/")
        assert len(lease_refs) <= 1, f"最多只有赢家一份租约，实际 {lease_refs}"

    hwm_refs = _refs(fake_github, "refs/heads/agent-hwm/issue-1/")
    assert hwm_refs, "HWM ref 只增不减，清理后必须仍有记录"
    # token 单调：最高代次必然 >= 赢家拿到的代次（并发探针可能先各自推进过）。
    max_token = max(int(ref.rsplit("/", 1)[-1]) for ref in hwm_refs)
    assert max_token >= int(winner["token"]), (
        f"HWM 最高代次 {max_token} 不应低于赢家 token {winner['token']}，实际 {hwm_refs}"
    )


def test_worker_module_imports_without_repo_side_effects():
    """避免测试误把仓库根目录当状态目录：包内模块导入后不应写文件。"""
    proc = _run(["-c", "import worker_linux, linux_worker_core, linux_config; print('ok')"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
    assert not (REPO_ROOT / ".heartbeat-muse-1-linux.json").exists()
