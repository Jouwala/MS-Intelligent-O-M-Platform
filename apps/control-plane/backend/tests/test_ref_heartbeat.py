"""Phase 3 P3-1（#141）：Worker 心跳的 refs 通道 + 七态推导。

覆盖：beat ref 命名/校验/解析、节流写、读最新、清理过期、状态推导矩阵、
以及经 fake GitHub server 的端到端（注册写 beat、working/idle 上报、
有效租约判 working、过期判 offline）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scheduler import ref_api, ref_heartbeat
from scheduler.api import SchedulerError
from scheduler.ref_service import GitHubRefScheduler

AGENT = "muse-2"
INSTANCE = "1a2b3c4d"
ATTEMPT = "a1b2c3d4e5f6"

EPOCH = datetime(2026, 11, 18, 8, 0, 0, tzinfo=timezone.utc)


def scheduler(ref_store, ref_transport, ref_clock, **kwargs) -> GitHubRefScheduler:
    return GitHubRefScheduler(ref_store, ref_transport, repo="o/r", clock=ref_clock, **kwargs)


class FakeRefStore:
    """内存 ref 存储（只实现 post/read/prune 用的最小协议）。"""

    def __init__(self) -> None:
        self.refs: dict[str, str] = {}

    def create(self, ref: str, sha: str) -> bool:
        if ref in self.refs:
            return False
        self.refs[ref] = sha
        return True

    def get(self, ref: str):
        if ref not in self.refs:
            return None
        return SimpleNamespace(ref=ref, sha=self.refs[ref])

    def list(self, prefix: str):
        return [
            SimpleNamespace(ref=ref, sha=sha)
            for ref, sha in sorted(self.refs.items())
            if ref.startswith(prefix)
        ]

    def delete(self, ref: str) -> bool:
        return self.refs.pop(ref, None) is not None

    def branch_sha(self, name: str = "main") -> str:
        return "a" * 40


# -- 命名与解析 ------------------------------------------------------------


def test_heartbeat_ref_round_trip_and_validation():
    epoch_ms = ref_heartbeat.epoch_ms_of(EPOCH)
    ref = ref_heartbeat.heartbeat_ref(AGENT, INSTANCE, epoch_ms, "working")
    assert ref == (
        f"refs/heads/agent-heartbeat/muse-2/1a2b3c4d/{epoch_ms:013d}/working"
    )
    beat = ref_heartbeat.parse_heartbeat_ref(ref)
    assert beat is not None
    assert (beat.agent_id, beat.instance_id) == (AGENT, INSTANCE)
    assert beat.epoch_ms == epoch_ms and beat.state == "working"
    assert beat.at == EPOCH
    # 大写身份归一化为小写
    assert ref_heartbeat.heartbeat_ref("Muse-2", "1A2B3C4D", 1, "online").startswith(
        "refs/heads/agent-heartbeat/muse-2/1a2b3c4d/"
    )
    with pytest.raises(ValueError):
        ref_heartbeat.heartbeat_ref(AGENT, "bad_instance", 1, "online")
    with pytest.raises(ValueError):
        ref_heartbeat.heartbeat_ref(AGENT, INSTANCE, 1, "sleeping")
    with pytest.raises(ValueError):
        ref_heartbeat.heartbeat_ref(AGENT, INSTANCE, -1, "online")
    assert ref_heartbeat.parse_heartbeat_ref("refs/heads/agent-hwm/issue-1/00000001") is None
    assert ref_heartbeat.parse_heartbeat_ref("refs/heads/agent-heartbeat/muse-2/x/1/online") is None


# -- 写 / 读 / 清理 ----------------------------------------------------------


def test_post_heartbeat_throttles_and_duplicate_same_ms_is_accepted():
    store = FakeRefStore()
    interval = timedelta(seconds=60)
    posted, ref = ref_heartbeat.post_heartbeat(
        store, "a" * 40, AGENT, INSTANCE, "online", EPOCH,
        interval=interval, last_post=None,
    )
    assert posted is True and ref is not None
    # 节流窗口内不再写
    posted, ref = ref_heartbeat.post_heartbeat(
        store, "a" * 40, AGENT, INSTANCE, "online", EPOCH,
        interval=interval, last_post=EPOCH,
    )
    assert posted is False and ref is None
    assert len(store.refs) == 1
    # 窗口外再写一条新 beat
    later = EPOCH + timedelta(seconds=61)
    posted, _ref = ref_heartbeat.post_heartbeat(
        store, "a" * 40, AGENT, INSTANCE, "working", later,
        interval=interval, last_post=EPOCH,
    )
    assert posted is True
    assert len(store.refs) == 2
    # 同一毫秒重复创建：ref 已存在，按已心跳处理，不报错
    posted, _ref = ref_heartbeat.post_heartbeat(
        store, "a" * 40, AGENT, INSTANCE, "working", later,
        interval=interval, last_post=None,
    )
    assert posted is True


def test_read_heartbeats_keeps_latest_and_ignores_garbage():
    store = FakeRefStore()
    store.create(
        ref_heartbeat.heartbeat_ref(AGENT, INSTANCE, 1000, "online"), "a" * 40
    )
    store.create(
        ref_heartbeat.heartbeat_ref(AGENT, INSTANCE, 3000, "working"), "a" * 40
    )
    store.create(
        ref_heartbeat.heartbeat_ref("muse-3", "9f9f9f9f", 2000, "idle"), "a" * 40
    )
    store.refs["refs/heads/agent-heartbeat/garbage"] = "a" * 40
    beats = ref_heartbeat.read_heartbeats(store)
    assert beats[(AGENT, INSTANCE)].epoch_ms == 3000
    assert beats[(AGENT, INSTANCE)].state == "working"
    assert beats[("muse-3", "9f9f9f9f")].state == "idle"
    assert len(beats) == 2


def test_prune_heartbeats_deletes_only_expired():
    store = FakeRefStore()
    now = EPOCH + timedelta(hours=2)
    store.create(
        ref_heartbeat.heartbeat_ref(AGENT, INSTANCE, 1000, "online"), "a" * 40
    )
    fresh_ms = ref_heartbeat.epoch_ms_of(now - timedelta(minutes=10))
    store.create(
        ref_heartbeat.heartbeat_ref(AGENT, INSTANCE, fresh_ms, "online"), "a" * 40
    )
    assert ref_heartbeat.prune_heartbeats(store, now) == 1
    assert len(store.refs) == 1
    assert list(store.refs)[0].endswith(f"/{fresh_ms:013d}/online")


def test_post_heartbeat_transport_failure_is_scheduler_error():
    class BrokenStore(FakeRefStore):
        def create(self, ref: str, sha: str):
            raise RuntimeError("断网")

    with pytest.raises(SchedulerError):
        ref_heartbeat.post_heartbeat(
            BrokenStore(), "a" * 40, AGENT, INSTANCE, "online", EPOCH,
            interval=timedelta(seconds=60),
        )


# -- 七态推导 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "reported,lease,expected",
    [
        ("online", False, "online"),
        ("idle", False, "idle"),
        ("working", False, "working"),
        ("online", True, "working"),
        ("idle", True, "working"),
        ("waiting", False, "waiting"),
        ("waiting", True, "working"),
        ("blocked", False, "blocked"),
        ("blocked", True, "blocked"),
        ("failed", False, "failed"),
        ("failed", True, "failed"),
        ("working", True, "working"),
    ],
)
def test_derive_worker_state_matrix(reported, lease, expected):
    state = ref_heartbeat.derive_worker_state(
        last_heartbeat_at=EPOCH,
        now=EPOCH + timedelta(seconds=10),
        stale_after=timedelta(seconds=90),
        reported_state=reported,
        has_live_lease=lease,
    )
    assert state == expected


def test_derive_worker_state_unknown_and_offline():
    assert (
        ref_heartbeat.derive_worker_state(
            last_heartbeat_at=None,
            now=EPOCH,
            stale_after=timedelta(seconds=90),
        )
        == "unknown"
    )
    # 过期且无租约 → offline
    assert (
        ref_heartbeat.derive_worker_state(
            last_heartbeat_at=EPOCH,
            now=EPOCH + timedelta(seconds=91),
            stale_after=timedelta(seconds=90),
            reported_state="working",
            has_live_lease=False,
        )
        == "offline"
    )
    # 过期但持有有效租约 → 仍算 working（租约由活进程续租，长任务不误判）
    assert (
        ref_heartbeat.derive_worker_state(
            last_heartbeat_at=EPOCH,
            now=EPOCH + timedelta(seconds=91),
            stale_after=timedelta(seconds=90),
            reported_state="working",
            has_live_lease=True,
        )
        == "working"
    )
    # 边界：恰好等于 TTL 仍算新鲜
    assert (
        ref_heartbeat.derive_worker_state(
            last_heartbeat_at=EPOCH,
            now=EPOCH + timedelta(seconds=90),
            stale_after=timedelta(seconds=90),
            reported_state="online",
        )
        == "online"
    )


# -- 经 fake GitHub server 的端到端 -------------------------------------------


def test_register_posts_beat_ref(fake_github, ref_store, ref_transport, ref_clock):
    authority = scheduler(ref_store, ref_transport, ref_clock)
    authority.register_worker(AGENT, INSTANCE, hostname="host-a", platform="linux")
    beats = ref_heartbeat.read_heartbeats(ref_store)
    assert (AGENT, INSTANCE) in beats
    assert beats[(AGENT, INSTANCE)].state == "online"


def test_heartbeat_state_reported_in_list_workers(
    fake_github, ref_store, ref_transport, ref_clock
):
    authority = scheduler(ref_store, ref_transport, ref_clock)
    authority.register_worker(AGENT, INSTANCE)
    ref_clock.advance(61)
    assert authority.heartbeat_worker(AGENT, INSTANCE, state="idle") is True
    workers = authority.list_workers(stale_after=timedelta(seconds=90))
    assert workers[0].state == "idle"
    ref_clock.advance(61)
    assert authority.heartbeat_worker(AGENT, INSTANCE, state="blocked") is True
    assert authority.list_workers(stale_after=timedelta(seconds=90))[0].state == "blocked"
    with pytest.raises(ValueError):
        authority.heartbeat_worker(AGENT, INSTANCE, state="sleeping")


def test_live_lease_makes_worker_working(fake_github, ref_store, ref_transport, ref_clock):
    fake_github.add_issue(1)
    authority = scheduler(ref_store, ref_transport, ref_clock)
    authority.register_worker(AGENT, INSTANCE)
    authority.claim(
        "issue-1", agent_id=AGENT, instance_id=INSTANCE, attempt_id=ATTEMPT
    )
    workers = authority.list_workers(stale_after=timedelta(seconds=90))
    assert workers[0].state == "working"
    # 租约过期后不再是 working（心跳仍新鲜 → online）
    ref_clock.advance(601)
    workers = authority.list_workers(stale_after=timedelta(seconds=3600))
    assert workers[0].state == "online"


def test_comment_facts_and_beat_facts_merge(
    fake_github, ref_store, ref_transport, ref_clock
):
    fake_github.add_issue(99)
    authority = scheduler(ref_store, ref_transport, ref_clock, registry_issue=99)
    authority.register_worker(AGENT, INSTANCE, hostname="h1", platform="linux")
    ref_clock.advance(61)
    assert authority.heartbeat_worker(AGENT, INSTANCE, state="idle") is True
    workers = authority.list_workers(stale_after=timedelta(seconds=90))
    assert workers[0].hostname == "h1"
    assert workers[0].state == "idle"
    # 评论通道的节流仍在：窗口内不重复发评论
    comments = len(fake_github.comment_bodies(99))
    assert authority.heartbeat_worker(AGENT, INSTANCE, state="idle") is True
    assert len(fake_github.comment_bodies(99)) == comments


# ---------------------------------------------------------------------------
# #141 复审回归（P0-1 / P1-2 / P1-3 / P2-4）：复现脚本改写为正式测试
# ---------------------------------------------------------------------------


def test_linux_ref_identity_lease_makes_worker_working(
    fake_github, ref_store, ref_transport, ref_clock
):
    """P0-1：Linux Worker 认领用租约身份 (agent, sha256(instance)[:16])，
    注册/心跳用人类身份 (agent, instance)——list_workers 必须把租约键归一，
    否则长任务的 Linux Worker 永远拿不到 working，会被误判 offline。"""
    fake_github.add_issue(1)
    authority = scheduler(ref_store, ref_transport, ref_clock)
    authority.register_worker("muse-1", "muse-1-linux", hostname="h", platform="linux")
    ref_instance = hashlib.sha256("muse-1-linux".encode()).hexdigest()[:16]
    assert ref_instance == ref_heartbeat.ref_instance_id("muse-1-linux")
    authority.claim(
        "issue-1", agent_id="muse-1", instance_id=ref_instance, attempt_id=ATTEMPT
    )
    assert ("muse-1", ref_instance) in authority._live_lease_keys(authority.now())
    workers = authority.list_workers(stale_after=timedelta(seconds=90))
    assert [(w.agent_id, w.instance_id) for w in workers] == [
        ("muse-1", "muse-1-linux")
    ]
    assert workers[0].state == "working"


def test_list_workers_does_not_pollute_local_registry(
    fake_github, ref_store, ref_transport, ref_clock
):
    """P1-2：list_workers 是读方法，不得把外来 beat 身份写进本进程 _workers，
    否则“心跳前必须先注册”的守卫会被一次 list_workers 绕过。"""
    a1 = scheduler(ref_store, ref_transport, ref_clock)
    a1.register_worker("muse-2", "1a2b3c4d")
    a2 = scheduler(ref_store, ref_transport, ref_clock)
    a2.register_worker("muse-9", "other-vm")
    assert ("muse-9", "other-vm") not in a1._workers
    assert a1.heartbeat_worker("muse-9", "other-vm") is False
    a1.list_workers(stale_after=timedelta(seconds=90))
    assert ("muse-9", "other-vm") not in a1._workers
    assert a1.heartbeat_worker("muse-9", "other-vm") is False


def test_comment_failure_does_not_block_refs_beat(
    fake_github, ref_store, ref_transport, ref_clock
):
    """P1-3：评论通道失败不得阻塞 refs 主通道——beat 先写，评论失败只记日志。"""
    fake_github.add_issue(99)
    authority = scheduler(ref_store, ref_transport, ref_clock, registry_issue=99)
    authority.register_worker(AGENT, INSTANCE)
    before = ref_heartbeat.read_heartbeats(ref_store)[(AGENT, INSTANCE)]
    ref_clock.advance(61)
    real_add = authority.transport.add_comment

    def boom(*args, **kwargs):
        raise RuntimeError("评论接口限流")

    authority.transport.add_comment = boom
    try:
        assert authority.heartbeat_worker(AGENT, INSTANCE, state="idle") is True
    finally:
        authority.transport.add_comment = real_add
    after = ref_heartbeat.read_heartbeats(ref_store)[(AGENT, INSTANCE)]
    assert after.epoch_ms > before.epoch_ms and after.state == "idle"


def test_beat_write_failure_returns_false(
    fake_github, ref_store, ref_transport, ref_clock
):
    """P2-4：beat 写失败时 heartbeat_worker 不得谎报 True。"""
    authority = scheduler(ref_store, ref_transport, ref_clock)
    authority.register_worker(AGENT, INSTANCE)
    ref_clock.advance(61)
    real_create = ref_store.create

    def boom(ref, sha):
        raise RuntimeError("断网")

    ref_store.create = boom
    try:
        assert authority.heartbeat_worker(AGENT, INSTANCE, state="idle") is False
    finally:
        ref_store.create = real_create
