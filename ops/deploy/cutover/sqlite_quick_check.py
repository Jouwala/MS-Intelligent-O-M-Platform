# -*- coding: utf-8 -*-
r"""sqlite_quick_check.py —— 对给定 SQLite 文件跑 PRAGMA quick_check。

给 ops\deploy\cutover\sync-derived-data.ps1 用：把「检查镜像是否完整」从
PowerShell 里内联 Python 改成独立脚本，避免 PS5.1 对 `-c` 参数里引号的二次解析。

退出码：0=全部 ok；1=有库不 ok 或打不开；2=参数错误。
只读打开（mode=ro），不会改任何数据。
"""
import sqlite3
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: sqlite_quick_check.py <db> [<db> ...]", file=sys.stderr)
        return 2
    bad = 0
    for raw in argv:
        p = Path(raw)
        try:
            conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error as e:
            print(f"{p.name}\tOPEN_FAILED\t{e}")
            bad += 1
            continue
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            val = (row[0] if row else "empty") or "empty"
        except sqlite3.Error as e:
            val = f"ERROR {e}"
        finally:
            conn.close()
        print(f"{p.name}\t{val}")
        if str(val).strip().lower() != "ok":
            bad += 1
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
