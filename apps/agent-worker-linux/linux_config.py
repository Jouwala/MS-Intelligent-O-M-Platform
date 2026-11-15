"""Linux 沙箱 Worker 配置（JSON）+ 校验 + 认证解析。

配置是**每台 VM 一份**（三台 VM 各用不同 `instance`）；原型配置里的旧字段名
（`agent` / `agent_id` / `instance_id`）继续兼容，便于迁移时只改必要的值。

关键约定
--------
- `instance` 是给人看的唯一实例名（如 `muse-1-linux`），默认等于 `worker_id`；
  禁止留空、禁止沿用原型里撞车的 `htch-runtime`。
- ref-CAS 权威层的租约 ref 名里只接受 **十六进制** 的 instance/attempt
  （见 `scheduler/ref_api.py` 的 `lease_ref`），因此 Worker 会把 `instance`
  稳定映射成 `ref_instance = sha256(instance)[:16]`；同一 instance 在任意机器上
  映射结果一致，不同 instance 不会撞 ref。`worker_id` 自身必须满足 ref 名的
  agent 约束（小写字母/数字/连字符）。
- token 优先级：显式 `token` > `token_env`（默认 `GITHUB_TOKEN`）> `token_file` > 空。
  surrogate 网关（`api_base` 指向本地代理）允许空 token；**本模块所有 repr /
  `public_identity()` / `redacted_dict()` 都不会带出 token 值**。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_TOKEN_ENV = "GITHUB_TOKEN"
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_LEASE_TTL_SECONDS = 600.0
DEFAULT_RENEW_INTERVAL_SECONDS = 150.0
DEFAULT_EXECUTOR_TIMEOUT_SECONDS = 1800.0
#: 原型里两台 VM 撞过的实例名，出现即报错（Issue #73 第 3 点）。
RESERVED_INSTANCES = frozenset({"htch-runtime"})

_AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_REPO_RE = re.compile(r"[:/](?P<owner>[^/:]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")

#: 必填/可选字段（仅用于 README 与排障；未知字段忽略，避免旧配置整体报错）。
KNOWN_KEYS = frozenset(
    {
        "worker_id",
        "agent",
        "agent_id",
        "instance",
        "instance_id",
        "repo_root",
        "repo",
        "api_base",
        "token",
        "token_env",
        "token_file",
        "agent_label",
        "poll_seconds",
        "lease_ttl_seconds",
        "renew_interval_seconds",
        "executor_timeout_seconds",
        "workdir",
        "per_task_dir",
        "state_dir",
        "log_file",
        "registry_issue",
    }
)


class ConfigError(ValueError):
    """配置缺失 / 非法；信息里会指出具体字段与修法。"""


def _as_positive_number(raw: Mapping[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"配置字段 {key} 必须是数字，实际：{value!r}")
    number = float(value)
    if number <= 0:
        raise ConfigError(f"配置字段 {key} 必须大于 0，实际：{value!r}")
    return number


def _as_bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false", "1", "0"):
        return value.lower() in ("true", "1")
    raise ConfigError(f"配置字段 {key} 必须是布尔值，实际：{value!r}")


def _first_present(raw: Mapping[str, Any], names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _git_remote_url(repo_root: Path) -> str:
    """读 repo_root 的 `origin` remote URL；失败返回空串（由调用方给清晰错误）。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def repo_from_remote_url(url: str) -> str:
    """`git@github.com:o/r.git` / `https://github.com/o/r` → `o/r`；解析失败返回空串。"""
    match = _REPO_RE.search(str(url or "").strip())
    if not match:
        return ""
    owner, name = match.group("owner"), match.group("repo")
    if not owner or not name:
        return ""
    return f"{owner}/{name}"


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = str(repo or "").partition("/")
    if not owner or not name or "/" in name:
        raise ConfigError(f"配置字段 repo 必须是 owner/name 形式，实际：{repo!r}")
    return owner, name


@dataclass(frozen=True)
class LinuxWorkerConfig:
    """一份已校验的 Worker 配置（不可变；token 不进入 repr）。"""

    worker_id: str
    instance: str
    repo_root: Path
    repo: str
    api_base: str = DEFAULT_API_BASE
    token: str = field(default="", repr=False)
    token_source: str = "none"
    agent_label: str = ""
    poll_seconds: float = DEFAULT_POLL_SECONDS
    lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS
    renew_interval_seconds: float = DEFAULT_RENEW_INTERVAL_SECONDS
    executor_timeout_seconds: float = DEFAULT_EXECUTOR_TIMEOUT_SECONDS
    workdir: Path = field(default_factory=lambda: Path("~/workspace").expanduser())
    per_task_dir: bool = False
    state_dir: Path = field(default_factory=lambda: Path("~/workspace").expanduser())
    log_file: Optional[Path] = None
    registry_issue: Optional[int] = None
    config_path: Optional[Path] = None

    # -- 身份派生 -----------------------------------------------------------
    @property
    def ref_agent_id(self) -> str:
        """ref 名里的 agent 段：小写（ref_api 要求 `[a-z0-9][a-z0-9-]{0,40}`）。"""
        return self.worker_id.lower()

    @property
    def ref_instance(self) -> str:
        """ref 名里的 instance 段：稳定十六进制（契约只接受 hex）。"""
        return hashlib.sha256(self.instance.lower().encode("utf-8")).hexdigest()[:16]

    @property
    def owner(self) -> str:
        return _split_repo(self.repo)[0]

    @property
    def repo_name(self) -> str:
        return _split_repo(self.repo)[1]

    @property
    def auth_mode(self) -> str:
        """描述认证来源（**绝不包含 token 值**）。"""
        if self.token:
            return self.token_source
        if self.api_base.rstrip("/") != DEFAULT_API_BASE:
            return "none(surrogate api_base, no token)"
        return "none"

    @property
    def surrogate(self) -> bool:
        return not self.token and self.api_base.rstrip("/") != DEFAULT_API_BASE

    def heartbeat_path(self) -> Path:
        return self.state_dir / f".heartbeat-{self.worker_id}.json"

    def task_workdir(self, issue_number: int) -> Path:
        if self.per_task_dir:
            return self.state_dir / "tasks" / f"issue-{int(issue_number)}"
        return self.workdir

    def task_log_dir(self, issue_number: int) -> Path:
        return self.state_dir / "logs" / f"issue-{int(issue_number)}"

    # -- 对外可打印视图（脱敏） ----------------------------------------------
    def public_identity(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "instance": self.instance,
            "ref_agent_id": self.ref_agent_id,
            "ref_instance": self.ref_instance,
            "repo": self.repo,
            "api_base": self.api_base,
            "agent_label": self.agent_label,
            "auth_mode": self.auth_mode,
            "repo_root": str(self.repo_root),
            "per_task_dir": self.per_task_dir,
            "workdir": str(self.workdir),
            "state_dir": str(self.state_dir),
        }

    def redacted_dict(self) -> dict[str, Any]:
        """配置快照（排障用）：保留字段形状但去掉 token 值。"""
        data = dict(self.public_identity())
        data.update(
            {
                "token": "***" if self.token else "",
                "token_source": self.token_source,
                "poll_seconds": self.poll_seconds,
                "lease_ttl_seconds": self.lease_ttl_seconds,
                "renew_interval_seconds": self.renew_interval_seconds,
                "executor_timeout_seconds": self.executor_timeout_seconds,
                "log_file": str(self.log_file) if self.log_file else None,
                "registry_issue": self.registry_issue,
                "config_path": str(self.config_path) if self.config_path else None,
            }
        )
        return data


def _validate_identity(worker_id: str, instance: str) -> None:
    if not _AGENT_RE.match(worker_id.lower()):
        raise ConfigError(
            "worker_id 只能是小写字母/数字/连字符（ref 名要求），"
            f"且不超过 41 个字符，实际：{worker_id!r}"
        )
    if not instance:
        raise ConfigError("instance 不能为空（每台 VM 必须唯一）")
    if instance.lower() in RESERVED_INSTANCES:
        raise ConfigError(
            f"instance={instance!r} 是原型里两台 VM 撞车的保留值，必须改成每台 VM 唯一的名字，"
            "例如 muse-1-linux / muse-2-linux / muse-3-linux"
        )
    if len(instance) > 64:
        raise ConfigError(f"instance 过长（≤64 字符），实际 {len(instance)}")


def from_mapping(
    raw: Mapping[str, Any],
    *,
    config_path: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    repo_url: Optional[Callable[[Path], str]] = None,
) -> LinuxWorkerConfig:
    """校验一份配置字典（`load_config` 与测试共用；不读文件）。"""
    if not isinstance(raw, Mapping):
        raise ConfigError("配置必须是 JSON 对象")
    env = os.environ if environ is None else environ

    worker_id = _first_present(raw, ("worker_id", "agent", "agent_id"))
    if not worker_id:
        raise ConfigError("配置缺少必填字段 worker_id（兼容旧名 agent / agent_id）")
    if "instance" in raw or "instance_id" in raw:
        # 显式给了 instance 就必须合法；空白不能静默回落成 worker_id（防撞车）
        instance_raw = raw.get("instance", raw.get("instance_id"))
        instance = instance_raw.strip() if isinstance(instance_raw, str) else ""
    else:
        instance = worker_id
    _validate_identity(worker_id, instance)

    repo_root_raw = raw.get("repo_root") or env.get("NOC_SOC_REPO_ROOT") or ""
    if not isinstance(repo_root_raw, str) or not repo_root_raw.strip():
        raise ConfigError(
            "配置缺少 repo_root（仓库 clone 路径）；也可用环境变量 NOC_SOC_REPO_ROOT 指定"
        )
    repo_root = Path(repo_root_raw).expanduser().resolve()
    factory = repo_root / "apps/control-plane/backend/scheduler/factory.py"
    if not factory.is_file():
        raise ConfigError(
            f"repo_root={repo_root} 不是完整仓库 clone：缺少 {factory.relative_to(repo_root)}"
        )

    repo_raw = raw.get("repo")
    if not isinstance(repo_raw, str) or not repo_raw.strip():
        url_getter = repo_url or _git_remote_url
        derived = repo_from_remote_url(url_getter(repo_root))
        if not derived:
            raise ConfigError(
                "无法从 repo_root 的 git remote origin 推导 repo；"
                "请在配置里显式设置 repo=\"owner/name\""
            )
        repo_raw = derived
    repo = repo_raw.strip()
    _split_repo(repo)

    api_base = str(raw.get("api_base") or DEFAULT_API_BASE).strip().rstrip("/")
    if not api_base.startswith(("http://", "https://")):
        raise ConfigError(f"api_base 必须是 http(s) URL，实际：{api_base!r}")

    # -- token 优先级：config > token_env > token_file > 空 ------------------
    token = ""
    token_source = "none"
    explicit_token = raw.get("token")
    if isinstance(explicit_token, str) and explicit_token.strip():
        token, token_source = explicit_token.strip(), "config"
    else:
        token_env = str(raw.get("token_env") or DEFAULT_TOKEN_ENV)
        env_value = str(env.get(token_env, "") or "").strip()
        if env_value:
            token, token_source = env_value, f"env:{token_env}"
        else:
            token_file_raw = raw.get("token_file")
            if isinstance(token_file_raw, str) and token_file_raw.strip():
                token_path = Path(token_file_raw).expanduser()
                if not token_path.is_file():
                    raise ConfigError(f"token_file 不存在：{token_path}")
                try:
                    file_token = token_path.read_text(encoding="utf-8").strip().splitlines()
                except OSError as exc:
                    raise ConfigError(f"读取 token_file 失败：{token_path}（{exc}）") from exc
                token = (file_token[0].strip() if file_token else "")
                token_source = f"file:{token_path}" if token else "none"

    workdir = Path(str(raw.get("workdir") or "~/workspace")).expanduser()
    per_task_dir = _as_bool(raw, "per_task_dir", False)
    state_dir_raw = raw.get("state_dir")
    state_dir = (
        Path(str(state_dir_raw)).expanduser()
        if isinstance(state_dir_raw, str) and state_dir_raw.strip()
        else workdir / "agent-worker-linux-state"
    )
    log_file_raw = raw.get("log_file")
    log_file = (
        Path(str(log_file_raw)).expanduser()
        if isinstance(log_file_raw, str) and log_file_raw.strip()
        else None
    )
    registry_issue = raw.get("registry_issue")
    if registry_issue is not None:
        if isinstance(registry_issue, bool) or not isinstance(registry_issue, int) or registry_issue <= 0:
            raise ConfigError(f"registry_issue 必须是正整数或 null，实际：{registry_issue!r}")

    poll_seconds = _as_positive_number(raw, "poll_seconds", DEFAULT_POLL_SECONDS)
    lease_ttl_seconds = _as_positive_number(
        raw, "lease_ttl_seconds", DEFAULT_LEASE_TTL_SECONDS
    )
    renew_interval_seconds = _as_positive_number(
        raw, "renew_interval_seconds", DEFAULT_RENEW_INTERVAL_SECONDS
    )
    executor_timeout_seconds = _as_positive_number(
        raw, "executor_timeout_seconds", DEFAULT_EXECUTOR_TIMEOUT_SECONDS
    )
    if renew_interval_seconds >= lease_ttl_seconds:
        raise ConfigError(
            "renew_interval_seconds 必须小于 lease_ttl_seconds，"
            f"实际 renew={renew_interval_seconds} / lease={lease_ttl_seconds}"
        )

    return LinuxWorkerConfig(
        worker_id=worker_id,
        instance=instance,
        repo_root=repo_root,
        repo=repo,
        api_base=api_base,
        token=token,
        token_source=token_source,
        agent_label=str(raw.get("agent_label") or f"agent:{worker_id}"),
        poll_seconds=poll_seconds,
        lease_ttl_seconds=lease_ttl_seconds,
        renew_interval_seconds=renew_interval_seconds,
        executor_timeout_seconds=executor_timeout_seconds,
        workdir=workdir,
        per_task_dir=per_task_dir,
        state_dir=state_dir,
        log_file=log_file,
        registry_issue=registry_issue,
        config_path=Path(config_path).resolve() if config_path else None,
    )


def load_config(
    path: str | Path,
    *,
    environ: Optional[Mapping[str, str]] = None,
    repo_url: Optional[Callable[[Path], str]] = None,
) -> LinuxWorkerConfig:
    """读取并校验 JSON 配置文件；任何问题都抛信息明确的 `ConfigError`。"""
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise ConfigError(
            f"配置文件不存在：{config_path}（可从 apps/agent-worker-linux/config.example.json 复制）"
        )
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{config_path}（{exc}）") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"配置文件必须是单个 JSON 对象：{config_path}"
            "（config.example.json 是三台 VM 的示例数组，请只取其中一条）"
        )
    return from_mapping(raw, config_path=config_path, environ=environ, repo_url=repo_url)


__all__ = [
    "ConfigError",
    "DEFAULT_API_BASE",
    "KNOWN_KEYS",
    "LinuxWorkerConfig",
    "RESERVED_INSTANCES",
    "from_mapping",
    "load_config",
    "repo_from_remote_url",
]
