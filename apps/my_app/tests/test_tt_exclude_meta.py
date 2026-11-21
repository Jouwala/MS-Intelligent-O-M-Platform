# -*- coding: utf-8 -*-
"""NUR 排除名单的元信息/写入路径（2026-11-18 切生产后复核）。

背景：`tt_exclude.replace_all` 与 `fetch_meta` 曾写死 `_uploaded_by`，而生产 PG
与本地 SQLite 的实际列名是 `uploaded_by`（只有 `_source_file` / `_import_time`
带下划线）。结果：
  · 元信息查询每次都抛 `column "_uploaded_by" does not exist` → 页面把 998 条
    名单显示成「0 条」；
  · 上传名单的 INSERT 同样失败 → 上传功能不可用。

这里用临时 SQLite（与生产同构：只有 `uploaded_by`）锁住「读、写都认真实列名」。
"""
import sqlite3

import pytest

from backend.services.tt import tt_exclude as tx


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    path = tmp_path / "tt.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE tt_exclude (ticket_no TEXT, source_file TEXT, source_sheet TEXT, "
        "import_time TEXT, uploaded_by TEXT, _source_file TEXT, _source_sheet TEXT, _import_time TEXT)"
    )
    con.commit()
    con.close()

    class _Wrap:
        def __init__(self, p):
            self._c = sqlite3.connect(p)

        def cursor(self):
            return self._c.cursor()

        def commit(self):
            self._c.commit()

        def rollback(self):
            self._c.rollback()

        def close(self):
            self._c.close()

    monkeypatch.setattr(tx, "get_raw_connection", lambda *a, **k: _Wrap(path))
    monkeypatch.setattr(tx, "_BY_COL_CACHE", None)
    yield path


def test_replace_all_uses_real_uploaded_by_column(temp_db):
    rep = tx.replace_all(["TT202601010001", "TT202601010002"],
                         source_file="probe.xlsx", uploaded_by="probe")
    assert rep["after_count"] == 2
    meta = tx.fetch_meta()
    assert meta["table_ready"] is True
    assert meta["count"] == 2
    assert meta["uploaded_by"] == "probe"
    assert meta["source_file"] == "probe.xlsx"
    assert sorted(tx.fetch_tickets()) == ["TT202601010001", "TT202601010002"]


def test_fetch_meta_reads_existing_rows(temp_db):
    con = sqlite3.connect(temp_db)
    con.execute("INSERT INTO tt_exclude (ticket_no, _source_file, _import_time, uploaded_by) "
                "VALUES ('TT202601010009','seed.xlsx','2026-10-18T16:02','__seed__')")
    con.commit()
    con.close()
    meta = tx.fetch_meta()
    assert meta["count"] == 1
    assert meta["uploaded_by"] == "__seed__"
    assert meta["import_time"] == "2026-10-18T16:02"
