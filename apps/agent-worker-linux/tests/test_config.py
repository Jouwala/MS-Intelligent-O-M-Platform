"""配置加载/校验（Issue #73：instance 唯一、别名兼容、token 不打印）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _linux_testkit import PACKAGE_DIR, REPO_ROOT, linux_config

BASE = {
    "worker_id": "muse-1-linux",
    "repo_root": str(REPO_ROOT),
    "repo": "Jouwala/noc-soc-intelligent-platform",
}


def _cfg(**overrides):
    raw = dict(BASE)
    raw.update(overrides)
    return linux_config.from_mapping(raw)


def test_minimal_defaults():
    config = _cfg()
    assert config.instance == "muse-1-linux"  # 默认 = worker_id
    assert config.agent_label == "agent:muse-1-linux"
    assert config.poll_seconds == 30.0
    assert config.lease_ttl_seconds == 600.0
    assert config.renew_interval_seconds == 150.0
    assert config.executor_timeout_seconds == 1800.0
    assert config.per_task_dir is False
    assert config.state_dir.name == "agent-worker-linux-state"
    assert config.auth_mode == "none"
    assert len(config.ref_instance) == 16
    int(config.ref_instance, 16)  # ref 名要求十六进制
    assert config.ref_agent_id == "muse-1-linux"


def test_prototype_aliases_accepted():
    raw = {
        "agent": "muse-2-linux",
        "instance_id": "inst-2",
        "repo_root": str(REPO_ROOT),
        "repo": "o/r",
    }
    config = linux_config.from_mapping(raw)
    assert config.worker_id == "muse-2-linux"
    assert config.instance == "inst-2"
    assert config.agent_label == "agent:muse-2-linux"


def test_instance_defaults_to_worker_id_when_omitted():
    raw = {
        "worker_id": "muse-3-linux",
        "repo_root": str(REPO_ROOT),
        "repo": "o/r",
    }
    assert linux_config.from_mapping(raw).instance == "muse-3-linux"


@pytest.mark.parametrize("bad_instance", ["htch-runtime", "HTCH-Runtime", "", "   "])
def test_reserved_or_empty_instance_rejected(bad_instance):
    with pytest.raises(linux_config.ConfigError) as excinfo:
        _cfg(instance=bad_instance)
    assert "instance" in str(excinfo.value)


def test_repo_root_without_factory_rejected(tmp_path):
    with pytest.raises(linux_config.ConfigError) as excinfo:
        _cfg(repo_root=str(tmp_path))
    assert "factory.py" in str(excinfo.value)


def test_repo_root_from_env(monkeypatch):
    raw = {"worker_id": "muse-1-linux", "repo": "o/r"}
    config = linux_config.from_mapping(raw, environ={"NOC_SOC_REPO_ROOT": str(REPO_ROOT)})
    assert config.repo_root == REPO_ROOT.resolve()


def test_repo_derived_from_remote_url():
    raw = {"worker_id": "muse-1-linux", "repo_root": str(REPO_ROOT)}
    config = linux_config.from_mapping(
        raw, repo_url=lambda _root: "git@github.com:Jouwala/noc-soc-intelligent-platform.git"
    )
    assert config.repo == "Jouwala/noc-soc-intelligent-platform"

    ssh_config = linux_config.from_mapping(
        {"worker_id": "muse-1-linux", "repo_root": str(REPO_ROOT)},
        repo_url=lambda _root: "https://github.com/o/r.git",
    )
    assert ssh_config.repo == "o/r"


def test_repo_underivable_raises():
    with pytest.raises(linux_config.ConfigError) as excinfo:
        linux_config.from_mapping(
            {"worker_id": "muse-1-linux", "repo_root": str(REPO_ROOT)},
            repo_url=lambda _root: "",
        )
    assert "repo" in str(excinfo.value)


def test_token_priority_explicit_over_env():
    config = linux_config.from_mapping(
        dict(BASE, token="explicit-token", token_env="MY_TOKEN"),
        environ={"MY_TOKEN": "env-token"},
    )
    assert config.token == "explicit-token"
    assert config.auth_mode == "config"


def test_token_from_env():
    config = linux_config.from_mapping(
        dict(BASE, token_env="MY_TOKEN"), environ={"MY_TOKEN": "env-token"}
    )
    assert config.token == "env-token"
    assert config.auth_mode == "env:MY_TOKEN"


def test_token_from_file(tmp_path):
    token_file = tmp_path / "token.txt"
    token_file.write_text("file-token\n", encoding="utf-8")
    config = linux_config.from_mapping(
        dict(BASE, token_file=str(token_file)), environ={}
    )
    assert config.token == "file-token"
    assert config.auth_mode.startswith("file:")


def test_missing_token_file_rejected(tmp_path):
    with pytest.raises(linux_config.ConfigError) as excinfo:
        linux_config.from_mapping(
            dict(BASE, token_file=str(tmp_path / "nope.txt")), environ={}
        )
    assert "token_file" in str(excinfo.value)


def test_token_never_leaks_into_repr_or_public_views():
    secret = "ghp_SUPER_SECRET_TOKEN_VALUE"
    config = _cfg(token=secret)
    assert secret not in repr(config)
    assert secret not in json.dumps(config.public_identity(), ensure_ascii=False)
    redacted = json.dumps(config.redacted_dict(), ensure_ascii=False)
    assert secret not in redacted
    assert "***" in redacted


def test_surrogate_mode_without_token():
    config = _cfg(api_base="http://127.0.0.1:8765")
    assert config.token == ""
    assert config.surrogate is True
    assert config.auth_mode.startswith("none(surrogate")


def test_renew_interval_must_be_less_than_lease_ttl():
    with pytest.raises(linux_config.ConfigError) as excinfo:
        _cfg(lease_ttl_seconds=600, renew_interval_seconds=600)
    assert "renew_interval_seconds" in str(excinfo.value)


@pytest.mark.parametrize("field", ["poll_seconds", "lease_ttl_seconds", "executor_timeout_seconds"])
def test_non_positive_numbers_rejected(field):
    with pytest.raises(linux_config.ConfigError):
        _cfg(**{field: 0})


def test_config_file_must_be_single_object(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps([BASE]), encoding="utf-8")
    with pytest.raises(linux_config.ConfigError) as excinfo:
        linux_config.load_config(config_path)
    assert "JSON 对象" in str(excinfo.value)


def test_missing_config_file_rejected(tmp_path):
    with pytest.raises(linux_config.ConfigError):
        linux_config.load_config(tmp_path / "nope.json")


def test_example_config_has_three_unique_instances():
    example = json.loads((PACKAGE_DIR / "config.example.json").read_text(encoding="utf-8"))
    assert isinstance(example, list) and len(example) == 3
    instances = [entry["instance"] for entry in example]
    assert instances == ["muse-1-linux", "muse-2-linux", "muse-3-linux"]
    for entry in example:
        assert entry["worker_id"] == entry["instance"]
        assert entry["repo_root"].startswith("/home/")
        assert entry["token_env"] == "GITHUB_TOKEN"
