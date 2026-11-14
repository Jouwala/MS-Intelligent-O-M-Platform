"""SQLite 持久化层：schema、唯一约束、事务原语（TASK-02 / TASK-04 / TASK-05）。

职责边界
--------
本模块**只**负责「数据库怎么连、表怎么建、事务怎么开」。业务规则（认领、fencing、
范围重叠判定、幂等完成、回收）在 `service.py` / `leasing.py` / `locking.py` /
`completion.py` 里，SQL 由业务层在 `transaction()` 给出的连接上执行。

关键约定
--------
- 每个 `SQLiteStore` 实例持有**一条**独立连接，只在单线程内使用；多线程/多进程
  并发场景请各自构造 `SchedulerService`（同一个数据库路径），不要共享 store。
- 所有写操作统一 `BEGIN IMMEDIATE`：开事务即拿写锁，数据库层面消灭
  「先检查后写入」的窗口；配合主键/唯一约束形成双保险。
- 时间全部以 **epoch 秒（REAL，UTC）** 存储，避免字符串比较歧义。
- 建表语句全部 `IF NOT EXISTS`，`__init__` 反复执行幂等；`_migrate` 只做
  「缺列补列」的幂等迁移，方便旧库就地升级。

数据库位置
----------
Worker 主入口按 `AGENT_SCHEDULER_DB` 环境变量定位：
- 显式设置：用该路径（相对路径按当前工作目录解析）；
- 未设置：`<repo>/TMP/scheduler/scheduler.db`（`TMP/` 已被 `.gitignore` 忽略，
  不会把运行时数据库混进版本库）。
`default_db_path()` 是唯一的解析入口，Worker 与测试都调它，避免出现第二套约定。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Sequence, Union

#: 环境变量名：调度器数据库文件位置（Worker 与测试共用）。
ENV_DB_PATH = "AGENT_SCHEDULER_DB"

#: 默认数据库相对仓库根的路径。
DEFAULT_DB_RELATIVE = Path("TMP") / "scheduler" / "scheduler.db"

#: 写事务等待锁的最长时间（毫秒）；多进程竞争时排队而不是立刻报 database is locked。
DEFAULT_BUSY_TIMEOUT_MS = 5000


def repo_root() -> Path:
    """仓库根目录（本文件位于 apps/control-plane/backend/scheduler/store.py）。"""
    return Path(__file__).resolve().parents[4]


def default_db_path() -> Path:
    """解析调度器数据库路径：环境变量优先，否则 `<repo>/TMP/scheduler/scheduler.db`。"""
    raw = os.environ.get(ENV_DB_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return repo_root() / DEFAULT_DB_RELATIVE


# ---------------------------------------------------------------------------
# schema（全部 IF NOT EXISTS；唯一约束是原子性的第二道保险）
# ---------------------------------------------------------------------------

_SCHEMA: tuple[str, ...] = (
    # 任务主表：状态机 + 当前代次 + 当前租约摘要（租约明细在 leases）
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id          TEXT PRIMARY KEY,
        issue_number     INTEGER NOT NULL,
        repo             TEXT NOT NULL DEFAULT '',
        title            TEXT NOT NULL DEFAULT '',
        state            TEXT NOT NULL DEFAULT 'queued',
        terminal_status  TEXT,
        fencing_token    INTEGER NOT NULL DEFAULT 0,
        attempt_id       TEXT NOT NULL DEFAULT '',
        agent_id         TEXT NOT NULL DEFAULT '',
        instance_id      TEXT NOT NULL DEFAULT '',
        lease_expire_at  REAL,
        heartbeat_at     REAL,
        started_at       REAL,
        completed_at     REAL,
        error_code       TEXT NOT NULL DEFAULT '',
        error_message    TEXT NOT NULL DEFAULT '',
        idempotency_key  TEXT NOT NULL DEFAULT '',
        scopes           TEXT NOT NULL DEFAULT '[]',
        cancel_requested_at REAL,
        updated_at       REAL
    )
    """,
    # 租约：每个 task 至多一行（主键即 task_id），与 tasks.fencing_token 同步递增
    """
    CREATE TABLE IF NOT EXISTS leases (
        task_id       TEXT PRIMARY KEY,
        attempt_id    TEXT NOT NULL,
        agent_id      TEXT NOT NULL,
        instance_id   TEXT NOT NULL,
        fencing_token INTEGER NOT NULL,
        claimed_at    REAL NOT NULL,
        expires_at    REAL NOT NULL,
        heartbeat_at  REAL NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
    )
    """,
    # 执行尝试：每次 claim 一行，旧行保留作审计（superseded / expired / terminal）
    """
    CREATE TABLE IF NOT EXISTS attempts (
        attempt_id    TEXT PRIMARY KEY,
        task_id       TEXT NOT NULL,
        agent_id      TEXT NOT NULL DEFAULT '',
        instance_id   TEXT NOT NULL DEFAULT '',
        fencing_token INTEGER NOT NULL DEFAULT 0,
        state         TEXT NOT NULL DEFAULT 'running',
        started_at    REAL NOT NULL,
        ended_at      REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id)",
    # 范围锁：每个 task 至多一行；通过 fencing_token 与租约绑定
    """
    CREATE TABLE IF NOT EXISTS scope_locks (
        task_id       TEXT PRIMARY KEY,
        attempt_id    TEXT NOT NULL,
        agent_id      TEXT NOT NULL DEFAULT '',
        instance_id   TEXT NOT NULL DEFAULT '',
        fencing_token INTEGER NOT NULL DEFAULT 0,
        scopes        TEXT NOT NULL DEFAULT '[]',
        acquired_at   REAL NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
    )
    """,
    # Worker 实例注册表（实例身份，不是任务租约）
    """
    CREATE TABLE IF NOT EXISTS workers (
        agent_id          TEXT NOT NULL,
        instance_id       TEXT NOT NULL,
        hostname          TEXT NOT NULL DEFAULT '',
        platform          TEXT NOT NULL DEFAULT '',
        capabilities      TEXT NOT NULL DEFAULT '[]',
        state             TEXT NOT NULL DEFAULT 'active',
        last_heartbeat_at REAL,
        registered_at     REAL NOT NULL,
        PRIMARY KEY (agent_id, instance_id)
    )
    """,
    # 完成记录：UNIQUE(idempotency_key) 保证重复提交只生效一次；
    # UNIQUE(task_id, attempt_id) 保证同一尝试不会产生两条完成记录。
    """
    CREATE TABLE IF NOT EXISTS completions (
        idempotency_key TEXT NOT NULL UNIQUE,
        task_id         TEXT NOT NULL,
        attempt_id      TEXT NOT NULL,
        terminal_status TEXT NOT NULL,
        summary         TEXT NOT NULL DEFAULT '',
        created_at      REAL NOT NULL,
        UNIQUE (task_id, attempt_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_completions_task ON completions(task_id)",
    # outbox：完成时一次性写入 comment / labels / audit，GitHub 回写失败可重放
    """
    CREATE TABLE IF NOT EXISTS outbox (
        op_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id    TEXT NOT NULL,
        kind       TEXT NOT NULL,
        payload    TEXT NOT NULL DEFAULT '{}',
        status     TEXT NOT NULL DEFAULT 'pending',
        attempts   INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, op_id)",
    # 分派幂等表：PRIMARY KEY(dispatch_key) 就是原子登记录
    """
    CREATE TABLE IF NOT EXISTS dispatches (
        dispatch_key   TEXT PRIMARY KEY,
        issue_number   INTEGER,
        pr_number      INTEGER,
        merge_sha      TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL DEFAULT 'registered',
        comment_posted INTEGER NOT NULL DEFAULT 0,
        created_at     REAL NOT NULL,
        updated_at     REAL NOT NULL
    )
    """,
    # 审计流水：认领、续租、迁移、完成、回收、范围锁全部留痕
    """
    CREATE TABLE IF NOT EXISTS audit (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id       TEXT NOT NULL DEFAULT '',
        event         TEXT NOT NULL,
        agent_id      TEXT NOT NULL DEFAULT '',
        instance_id   TEXT NOT NULL DEFAULT '',
        attempt_id    TEXT NOT NULL DEFAULT '',
        fencing_token INTEGER NOT NULL DEFAULT 0,
        detail        TEXT NOT NULL DEFAULT '{}',
        created_at    REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_task ON audit(task_id, id)",
)

#: 幂等迁移：老库缺列时补上（表名, 列名, 列定义）。新库建表已含全部列。
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("tasks", "terminal_status", "TEXT"),
    ("tasks", "cancel_requested_at", "REAL"),
    ("tasks", "scopes", "TEXT NOT NULL DEFAULT '[]'"),
    ("outbox", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("outbox", "last_error", "TEXT NOT NULL DEFAULT ''"),
    ("dispatches", "comment_posted", "INTEGER NOT NULL DEFAULT 0"),
    ("workers", "platform", "TEXT NOT NULL DEFAULT ''"),
)


class SQLiteStore:
    """一条 SQLite 连接 + schema 初始化 + `BEGIN IMMEDIATE` 事务。

    用法::

        store = SQLiteStore(path)
        with store.transaction() as conn:
            conn.execute(...)          # 同一事务内，要么全提交要么全回滚
        store.close()

    同一实例可跨线程复用（内部 RLock 串行化事务），但**不推荐**：并发测试与
    生产 Worker 都应「一线程/一进程一个 SchedulerService」。
    """

    def __init__(
        self,
        db_path: Union[str, Path, None] = None,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        path = Path(db_path).expanduser() if db_path is not None else default_db_path()
        self.db_path = str(path)
        if self.db_path != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=max(busy_timeout_ms / 1000.0, 0.1),
            isolation_level=None,  # 手动管理事务：显式 BEGIN IMMEDIATE / COMMIT
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.db_path != ":memory:":
            # WAL：读写不互相阻塞；多进程时由 busy_timeout 排队。内存库不支持。
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()

    # -- 连接与 schema -------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def _init_schema(self) -> None:
        with self.transaction() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """幂等补列：只 ADD COLUMN，不重建表；重复执行无副作用。"""
        for table, column, ddl in _MIGRATIONS:
            cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # -- 事务原语 ------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：`BEGIN IMMEDIATE` → 提交/回滚。不可嵌套。"""
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # 连接已损坏时不要覆盖原始异常
                    pass
                raise
            else:
                try:
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise

    # -- 便捷查询（读写都在调用方的事务内使用） --------------------------------
    def one(self, sql: str, params: Sequence[object] = ()) -> Optional[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    def run(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
