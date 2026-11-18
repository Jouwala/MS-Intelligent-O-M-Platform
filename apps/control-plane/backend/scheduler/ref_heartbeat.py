"""Worker 心跳的 refs 通道（Phase 3 P3-1，Issue #141）。

为什么不用注册表 Issue 评论：`ref_workers.py` 的心跳评论通道需要一个真实的
`registry_issue`，但全仓库默认是 None（`factory.py` / `ref_service.py`），
且评论心跳有硬伤——60s 节流下 5 个 Worker 每天也要往一个 Issue 写约 7200 条
评论，`list_workers` 分页读全量评论只会越来越慢。refs 通道与 ADR-7 的
HWM/lease 同一机制：状态只看 ref 名，不读评论、不读 commit。

命名：`refs/heads/agent-heartbeat/<agent>/<instance>/<epoch_ms:013d>/<state>`
- epoch_ms 是心跳时刻（毫秒），存活只看 `now - epoch_ms < ttl`；
- state 是 Worker 自报的状态：online / idle / working / waiting / blocked / failed；
- 每次心跳新建一个 beat ref（锚点 SHA 用 main 头，与 HWM/lease 相同），同一毫秒
  的重复创建返回已存在，按已心跳处理；旧 beat 由 `prune_heartbeats` 清理
  （默认只保留 1 小时内的 beat；写心跳后顺手清理，清理失败不影响心跳本身）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from .api import SchedulerError, utcnow

#: 心跳 ref 的根命名空间（与 agent-hwm / agent-lease / agent-scope 并列）。
HEARTBEAT_ROOT = "refs/heads/agent-heartbeat"

#: Worker 自报的状态（七态中的六态；offline 由读侧按新鲜度推导，见 derive）。
HEARTBEAT_STATES = frozenset(
    {"online", "idle", "working", "waiting", "blocked", "failed"}
)

#: list_workers 输出的全部状态（含读侧推导的 offline / unknown）。
WORKER_STATES = frozenset(
    {"online", "idle", "working", "waiting", "blocked", "failed", "offline", "unknown"}
)

#: beat ref 保留时长（写心跳时顺手清理更早的 beat；崩溃残留的 beat 也会被清掉）。
DEFAULT_HEARTBEAT_RETENTION = timedelta(hours=1)

_AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BEAT_RE = re.compile(
    r"^refs/heads/agent-heartbeat/(?P<agent>[a-z0-9][a-z0-9-]{0,40})/"
    r"(?P<instance>[a-z0-9][a-z0-9-]{0,63})/"
    r"(?P<epoch>\d{13})/(?P<state>[a-z]+)$"
)


@dataclass(frozen=True)
class HeartbeatBeat:
    """从 ref 名解析出的一条心跳（不含 GitHub 返回的 sha）。"""

    agent_id: str
    instance_id: str
    epoch_ms: int
    state: str
    ref: str = ""

    @property
    def at(self) -> datetime:
        return datetime.fromtimestamp(self.epoch_ms / 1000, tz=timezone.utc)


def heartbeat_ref(agent_id: str, instance_id: str, epoch_ms: int, state: str) -> str:
    """心跳 beat ref 名。非法身份/状态直接抛错，不静默拼出坏 ref。"""
    agent = str(agent_id).lower()
    if not _AGENT_RE.match(agent):
        raise ValueError(f"agent_id 不能安全放进 ref 名：{agent_id!r}")
    instance = str(instance_id).lower()
    if not _INSTANCE_RE.match(instance):
        raise ValueError(f"instance_id 不能安全放进 ref 名：{instance_id!r}")
    if state not in HEARTBEAT_STATES:
        raise ValueError(f"心跳状态必须是 {sorted(HEARTBEAT_STATES)} 之一：{state!r}")
    if not isinstance(epoch_ms, int) or epoch_ms < 0:
        raise ValueError(f"epoch_ms 必须是非负整数：{epoch_ms!r}")
    return f"{HEARTBEAT_ROOT}/{agent}/{instance}/{epoch_ms:013d}/{state}"


def parse_heartbeat_ref(ref: str) -> Optional[HeartbeatBeat]:
    """解析 beat ref 名；不匹配返回 None（调用方忽略脏 ref，不报错）。"""
    match = _BEAT_RE.match(ref or "")
    if not match:
        return None
    return HeartbeatBeat(
        agent_id=match.group("agent"),
        instance_id=match.group("instance"),
        epoch_ms=int(match.group("epoch")),
        state=match.group("state"),
        ref=ref,
    )


def ref_instance_id(instance: str) -> str:
    """Linux Worker 的租约身份：人类 instance 名 → ref 名里的 hex instance。

    与 `apps/agent-worker-linux/linux_config.py` 的
    `LinuxWorkerConfig.ref_instance` 同一公式（`sha256(instance.lower())[:16]`）；
    两处必须保持一致——认领侧（lease ref）只接受 hex，注册/心跳侧用人类名，
    `list_workers` 靠本函数把租约键归一回人类身份（见 ref_workers.py）。
    跨应用无法共享 import，故公式在此与 linux_config 各写一份，以本函数为准。
    """
    return hashlib.sha256(str(instance).lower().encode("utf-8")).hexdigest()[:16]


def epoch_ms_of(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def post_heartbeat(
    ref_store: Any,
    anchor_sha: str,
    agent_id: str,
    instance_id: str,
    state: str,
    now: datetime,
    *,
    interval: timedelta,
    last_post: Optional[datetime] = None,
) -> tuple[bool, Optional[str]]:
    """写一条心跳 beat ref。

    返回 (posted, ref)：节流窗口内返回 (False, None)；创建成功或 ref 已存在
    （同一毫秒的重复创建）返回 (True, ref)。传输失败抛 SchedulerError。
    """
    if last_post is not None and now - last_post < interval:
        return False, None
    ref = heartbeat_ref(agent_id, instance_id, epoch_ms_of(now), state)
    try:
        created = bool(ref_store.create(ref, anchor_sha))
    except Exception as exc:  # noqa: BLE001 - 存储实现异常类型不一，统一收敛
        raise SchedulerError(f"写心跳 ref {ref} 失败：{exc}") from exc
    # created=False 说明同名 beat 已存在（同一毫秒重复心跳或另一进程同身份）：
    # beat 本来就在，heartbeat 已被接受，不当作失败。
    return True, ref


def read_heartbeats(ref_store: Any) -> dict[tuple[str, str], HeartbeatBeat]:
    """读全部 beat ref，按 (agent, instance) 只留最新的一条。"""
    try:
        infos = list(ref_store.list(HEARTBEAT_ROOT + "/") or [])
    except Exception as exc:  # noqa: BLE001 - 存储实现异常类型不一，统一收敛
        raise SchedulerError(f"读心跳 refs 失败：{exc}") from exc
    latest: dict[tuple[str, str], HeartbeatBeat] = {}
    for info in infos:
        beat = parse_heartbeat_ref(getattr(info, "ref", "") or "")
        if beat is None:
            continue
        key = (beat.agent_id, beat.instance_id)
        current = latest.get(key)
        if current is None or beat.epoch_ms > current.epoch_ms:
            latest[key] = beat
    return latest


def prune_heartbeats(
    ref_store: Any,
    now: datetime,
    *,
    retention: timedelta = DEFAULT_HEARTBEAT_RETENTION,
) -> int:
    """删除超过保留期的 beat ref（清理崩溃残留与旧 beat）。

    逐条尽力删除，单条失败不中断、不报错；返回实际删除条数。
    """
    try:
        infos = list(ref_store.list(HEARTBEAT_ROOT + "/") or [])
    except Exception:  # noqa: BLE001 - 清理是 best-effort，读失败就跳过
        return 0
    cutoff = epoch_ms_of(now - retention)
    deleted = 0
    for info in infos:
        beat = parse_heartbeat_ref(getattr(info, "ref", "") or "")
        if beat is None or beat.epoch_ms >= cutoff:
            continue
        try:
            if ref_store.delete(beat.ref):
                deleted += 1
        except Exception:  # noqa: BLE001 - 单条清理失败不阻塞
            continue
    return deleted


def derive_worker_state(
    *,
    last_heartbeat_at: Optional[datetime],
    now: Optional[datetime] = None,
    stale_after: timedelta,
    reported_state: str = "online",
    has_live_lease: bool = False,
) -> str:
    """由心跳新鲜度 + 自报状态 + 租约推导七态之一。

    优先级（先判定的胜出）：持有有效租约 → working（租约由活着的进程定期
    续租，是比心跳更强的存活证据——长任务执行中超 90s 未写 beat 仍算 working，
    不误判 offline；但自报 failed / blocked 是更具体的状态，优先于租约）；
    无心跳 → unknown；过期 → offline；自报 failed / blocked / waiting 原样保留；
    自报 working → working；自报 idle → idle；其余新鲜心跳 → online。
    """
    if has_live_lease:
        if reported_state == "failed":
            return "failed"
        if reported_state == "blocked":
            return "blocked"
        return "working"
    if last_heartbeat_at is None:
        return "unknown"
    moment = now or utcnow()
    if moment - last_heartbeat_at > stale_after:
        return "offline"
    if reported_state == "failed":
        return "failed"
    if reported_state == "blocked":
        return "blocked"
    if reported_state == "waiting":
        return "waiting"
    if reported_state == "working":
        return "working"
    if reported_state == "idle":
        return "idle"
    return "online"


__all__ = [
    "DEFAULT_HEARTBEAT_RETENTION",
    "HEARTBEAT_ROOT",
    "HEARTBEAT_STATES",
    "WORKER_STATES",
    "HeartbeatBeat",
    "derive_worker_state",
    "epoch_ms_of",
    "heartbeat_ref",
    "parse_heartbeat_ref",
    "post_heartbeat",
    "prune_heartbeats",
    "read_heartbeats",
    "ref_instance_id",
]
