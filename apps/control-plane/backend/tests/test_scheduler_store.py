"""存储层：schema / 唯一约束 / 事务回滚 / 建表幂等 / 默认路径。

这些用例直接验证 SQLite 层，不经过业务封装——唯一约束是并发原子性的第二道保险，
必须证明它真的建在库里，而不是只写在文档里。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scheduler import SchedulerService
from scheduler.store import ENV_DB_PATH, SQLiteStore, default_db_path, repo_root

EXPECTED_TABLES = {
    "tasks",
    "leases",
    "attempts",
    "scope_locks",
    "workers",
    "completions",
    "outbox",
    "dispatches",
    "audit",
}


def test_st01_schema_tables_and_unique_constraints(tmp_path):
    store = SQLiteStore(tmp_path / "schema.db")
    try:
        tables = {
            row["name"]
            for row in store.all("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert EXPECTED_TABLES <= tables
        # leases / scope_locks 每 task 至多一行：task_id 是主键
        for table in ("leases", "scope_locks"):
            info = store.all(f"PRAGMA table_info({table})")
            primary = [row["name"] for row in info if row["pk"]]
            assert primary == ["task_id"]
        # completions 两个唯一约束
        indexes = store.all("PRAGMA index_list(completions)")
        assert any(row["unique"] for row in indexes)
        # 直接插重复 completion 必须被数据库拒绝（不依赖应用层检查）
        store.run(
            "INSERT INTO completions(idempotency_key, task_id, attempt_id,"
            " terminal_status, summary, created_at) VALUES ('k','t','a','completed','',0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.run(
                "INSERT INTO completions(idempotency_key, task_id, attempt_id,"
                " terminal_status, summary, created_at) VALUES ('k','t2','a2','completed','',0)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            store.run(
                "INSERT INTO completions(idempotency_key, task_id, attempt_id,"
                " terminal_status, summary, created_at) VALUES ('k2','t','a','completed','',0)"
            )
    finally:
        store.close()


def test_st02_reopen_is_idempotent_and_persists(db_path):
    for _ in range(3):  # 反复建表/补列必须幂等
        service = SchedulerService(db_path)
        service.upsert_task("issue-1", 1)
        service.close()
    service = SchedulerService(db_path)
    try:
        assert service.get_task("issue-1").state == "queued"
        journal = service.store.one("PRAGMA journal_mode")
        assert str(journal[0]).lower() == "wal"  # 文件库启用 WAL
    finally:
        service.close()


def test_st03_transaction_rolls_back_on_exception(db_path):
    store = SQLiteStore(db_path)
    try:
        with pytest.raises(RuntimeError):
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO tasks(task_id, issue_number, state) VALUES (?,?,?)",
                    ("rollback-me", 1, "queued"),
                )
                raise RuntimeError("回滚")
        assert store.one("SELECT * FROM tasks WHERE task_id = 'rollback-me'") is None
    finally:
        store.close()


def test_st04_default_db_path_env_override(monkeypatch, tmp_path):
    expected = tmp_path / "custom" / "scheduler.db"
    monkeypatch.setenv(ENV_DB_PATH, str(expected))
    assert default_db_path() == expected
    monkeypatch.delenv(ENV_DB_PATH, raising=False)
    default = default_db_path()
    assert default == repo_root() / "TMP" / "scheduler" / "scheduler.db"
    assert Path(default).is_absolute()


def test_st05_direct_duplicate_lease_violates_primary_key(tmp_path):
    store = SQLiteStore(tmp_path / "lease.db")
    try:
        store.run(
            "INSERT INTO tasks(task_id, issue_number, state) VALUES ('t',1,'claimed')"
        )
        store.run(
            "INSERT INTO leases(task_id, attempt_id, agent_id, instance_id,"
            " fencing_token, claimed_at, expires_at, heartbeat_at)"
            " VALUES ('t','a1','muse-2','i-1',1,0,9999999999,0)"
        )
        # 「每 task 至多一行租约」由主键保证，不依赖应用层判断
        with pytest.raises(sqlite3.IntegrityError):
            store.run(
                "INSERT INTO leases(task_id, attempt_id, agent_id, instance_id,"
                " fencing_token, claimed_at, expires_at, heartbeat_at)"
                " VALUES ('t','a2','muse-3','i-2',2,0,9999999999,0)"
            )
    finally:
        store.close()
