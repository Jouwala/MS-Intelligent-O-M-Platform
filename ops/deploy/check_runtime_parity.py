# -*- coding: utf-8 -*-
"""check_runtime_parity.py —— 切换后「运行时状态」与上一版生产的一比一核对
============================================================================
背景（2026-11-19 事故）：2026-11-18 切到 D:\\02_olbr_portal 后，用户连续发现
「Domain Dashboard 的 CXO View 只剩 3 条」「OSS Portal Login 的链接只剩 1 条」
「用户管理里之前分配的 team portal 没了」。三者都不是代码问题，而是**运行时
状态没搬全**：

  · my_app `backend\\data\\portal_catalog.json` 没搬 —— v3 门户在目录为空时
    回落到 MsPortal/assets/data.js（54 条内置、CXO View 只有 3 条、
    Friends Link 只有 1 条），于是整页看板目录缩水；
  · `ui_tool_order.json` / `ui_tool_visibility.json` / `settings.json` 没搬；
  · 门户 `auth-data\\portal-auth.db` 用的是 01_RPA 那份（30 个账号），
    而不是 `D:\\01_olbr_portal` 那份（33 个，含 team 分配）。

为什么之前的检查没发现：`post-cutover-check.ps1` 只验证**存活与结构** ——
端口、健康、OpenAPI 路由、库 schema、看板组件能否取到数。它从不比较
「目录有多少条」「用户有多少个、team 字段是什么」。本脚本补这一层。

做什么（只读，不改任何文件）：
  1. 门户 auth-data：账号缺失、team 字段丢失、待审申请缺失、team_scope 矩阵差异；
  2. 门户 portal-default.json；
  3. my_app backend/data 的运行时 JSON（目录 / UI 工具顺序 / 可见性 / 侧栏布局 / settings）；
  4. 看板目录条目数（按分组计数，重点 CXO View / Friends Link）；
  5. my_app 本地权威表（credentials / task_schedule_config / email_recipients）。

判定口径：
  FAIL —— 目标侧**缺**源侧有的东西（用户/字段/条目/文件），或目标文件比源文件旧；
  WARN —— 两边都有但内容不同（可能是切换后合法修改，需人看一眼）；
  INFO —— 目标侧多出来的东西（切换后新建，正常）。

用法：
  python ops\\deploy\\check_runtime_parity.py
  python ops\\deploy\\check_runtime_parity.py --source D:\\01_olbr_portal --target D:\\02_olbr_portal
退出码：0 = 无 FAIL；2 = 有 FAIL；3 = 参数/路径错误。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sqlite3
import sys

FAIL: list[str] = []
WARN: list[str] = []
INFO: list[str] = []


def fail(msg: str) -> None:
    FAIL.append(msg)
    print("FAIL  " + msg)


def warn(msg: str) -> None:
    WARN.append(msg)
    print("WARN  " + msg)


def info(msg: str) -> None:
    INFO.append(msg)
    print("INFO  " + msg)


def ok(msg: str) -> None:
    print("PASS  " + msg)


def _first_dir(*cands: str) -> str:
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return ""


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _connect_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_rows(path: str, table: str) -> list[dict]:
    conn = _connect_ro(path)
    try:
        return [dict(r) for r in conn.execute('select * from "%s"' % table)]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 1. 门户：portal-default.json + auth-data
# --------------------------------------------------------------------------
def check_portal(src_portal: str, dst_portal: str) -> None:
    print("\n== 门户状态（auth-data / portal-default.json）")
    if not src_portal or not dst_portal:
        warn("找不到门户目录，跳过（源=%s 目标=%s）" % (src_portal or "-", dst_portal or "-"))
        return

    # portal-default.json
    for name in ("portal-default.json",):
        a = os.path.join(src_portal, name)
        b = os.path.join(dst_portal, name)
        if not os.path.exists(a):
            warn("源门户缺 %s，无法比对" % name)
            continue
        if not os.path.exists(b):
            fail("目标门户缺 %s" % name)
            continue
        if _md5(a) == _md5(b):
            ok("%s 一致" % name)
        else:
            warn("%s 内容不同（源 %s / 目标 %s）" % (name, _fmt_ts(_mtime(a)), _fmt_ts(_mtime(b))))

    # auth-data
    auth_src = os.path.join(src_portal, "auth-data", "portal-auth.db")
    auth_dst = os.path.join(dst_portal, "auth-data", "portal-auth.db")
    if not os.path.exists(auth_src):
        warn("源门户缺 auth-data/portal-auth.db，跳过账号比对")
        return
    if not os.path.exists(auth_dst):
        fail("目标门户缺 auth-data/portal-auth.db")
        return

    try:
        su = _table_rows(auth_src, "users")
        du = _table_rows(auth_dst, "users")
    except sqlite3.Error as e:
        fail("读取账号表失败：%s" % e)
        return

    s_by = {str(r.get("username")): r for r in su}
    d_by = {str(r.get("username")): r for r in du}
    missing = sorted(set(s_by) - set(d_by))
    extra = sorted(set(d_by) - set(s_by))
    if missing:
        fail("账号缺失 %d 个：%s" % (len(missing), ", ".join(missing)))
    else:
        ok("账号无缺失（源 %d / 目标 %d）" % (len(su), len(du)))
    if extra:
        info("目标侧多出账号 %d 个（切换后新建，正常）：%s" % (len(extra), ", ".join(extra)))

    team_diff = []
    for name in sorted(set(s_by) & set(d_by)):
        st = (s_by[name].get("team") or "")
        dt = (d_by[name].get("team") or "")
        if st != dt:
            team_diff.append("%s(%s→%s)" % (name, st or "空", dt or "空"))
    if team_diff:
        fail("team portal 分配丢失/不同 %d 个：%s" % (len(team_diff), ", ".join(team_diff)))
    else:
        ok("team portal 分配一致")

    # 待审申请
    try:
        sr = {str(r.get("id")): r for r in _table_rows(auth_src, "registration_requests")}
        dr = {str(r.get("id")): r for r in _table_rows(auth_dst, "registration_requests")}
        miss_r = sorted(set(sr) - set(dr))
        if miss_r:
            fail("待审账号申请缺失 %d 条：%s" % (len(miss_r), ", ".join(miss_r)))
        else:
            ok("待审账号申请无缺失（源 %d / 目标 %d）" % (len(sr), len(dr)))
    except sqlite3.Error as e:
        warn("读取 registration_requests 失败：%s" % e)

    # team_scope 矩阵（管理员可在门户里改，差异只提示）
    try:
        sm = _table_rows(auth_src, "team_scope")
        dm = _table_rows(auth_dst, "team_scope")
        if sm and dm and (sm[0].get("matrix") or "") != (dm[0].get("matrix") or ""):
            warn("team_scope 矩阵内容不同（可能是切换后合法修改，请人工确认）")
        elif sm and dm:
            ok("team_scope 矩阵一致")
    except sqlite3.Error as e:
        warn("读取 team_scope 失败：%s" % e)


# --------------------------------------------------------------------------
# 2. my_app backend/data 的运行时 JSON
# --------------------------------------------------------------------------
CONFIG_FILES = (
    "portal_catalog.json",
    "settings.json",
    "ui_tool_order.json",
    "ui_tool_visibility.json",
    "ui_menu_layout.json",
)


def check_myapp_config(src_my: str, dst_my: str) -> None:
    print("\n== my_app 运行时配置（backend/data）")
    if not src_my or not dst_my:
        warn("找不到 my_app 目录，跳过（源=%s 目标=%s）" % (src_my or "-", dst_my or "-"))
        return
    a_dir = os.path.join(src_my, "backend", "data")
    b_dir = os.path.join(dst_my, "backend", "data")
    total = 0
    for name in CONFIG_FILES:
        a = os.path.join(a_dir, name)
        b = os.path.join(b_dir, name)
        if not os.path.exists(a):
            continue
        total += 1
        if not os.path.exists(b):
            fail("目标缺 backend/data/%s（源 %s）" % (name, _fmt_ts(_mtime(a))))
            continue
        if _md5(a) == _md5(b):
            ok("backend/data/%s 一致" % name)
        elif _mtime(a) > _mtime(b) + 1:
            fail("backend/data/%s 目标比源旧（源 %s / 目标 %s）"
                 % (name, _fmt_ts(_mtime(a)), _fmt_ts(_mtime(b))))
        else:
            warn("backend/data/%s 内容不同（源 %s / 目标 %s）"
                 % (name, _fmt_ts(_mtime(a)), _fmt_ts(_mtime(b))))
    if not total:
        warn("源树没有可比的 backend/data 配置")

    # 看板目录：条目与关键分组计数（这是 CXO View / OSS Portal Login 缩水的直接指标）
    a = os.path.join(a_dir, "portal_catalog.json")
    b = os.path.join(b_dir, "portal_catalog.json")
    if not (os.path.exists(a) and os.path.exists(b)):
        return
    try:
        ca = json.load(open(a, encoding="utf-8"))
        cb = json.load(open(b, encoding="utf-8"))
    except (OSError, ValueError) as e:
        fail("看板目录无法解析：%s" % e)
        return
    ia = ca.get("items") or []
    ib = cb.get("items") or []
    if len(ib) < len(ia):
        fail("看板目录条目缩水：源 %d 条 → 目标 %d 条" % (len(ia), len(ib)))
    else:
        ok("看板目录条目数：源 %d / 目标 %d" % (len(ia), len(ib)))

    def group_counts(items: list[dict]) -> dict[str, int]:
        out: dict[str, int] = {}
        for it in items:
            for g in it.get("groups") or []:
                out[g] = out.get(g, 0) + 1
        return out

    ga, gb = group_counts(ia), group_counts(ib)
    lost = {k: (v, gb.get(k, 0)) for k, v in ga.items() if gb.get(k, 0) < v}
    if lost:
        detail = ", ".join("%s %d→%d" % (k, v[0], v[1]) for k, v in sorted(lost.items()))
        fail("分组条目缩水：%s" % detail)
    else:
        ok("各分组条目数不低于源树")


# --------------------------------------------------------------------------
# 3. my_app 本地权威表（SQLite）
# --------------------------------------------------------------------------
LOCAL_TABLES = {
    "credentials": "key",
    "task_schedule_config": "task_id",
    "email_recipients": "key",
}


def check_local_authority(src_my: str, dst_my: str) -> None:
    print("\n== my_app 本地权威表（db/app.db）")
    a = os.path.join(src_my, "db", "app.db")
    b = os.path.join(dst_my, "db", "app.db")
    if not (os.path.exists(a) and os.path.exists(b)):
        warn("db/app.db 不存在，跳过（源=%s 目标=%s）" % (os.path.exists(a), os.path.exists(b)))
        return
    for table, key_col in LOCAL_TABLES.items():
        try:
            ra = {str(r.get(key_col)): r for r in _table_rows(a, table)}
            rb = {str(r.get(key_col)): r for r in _table_rows(b, table)}
        except sqlite3.Error as e:
            warn("读取 %s 失败：%s" % (table, e))
            continue
        miss = sorted(set(ra) - set(rb))
        if miss:
            fail("%s 缺行 %d 条：%s" % (table, len(miss), ", ".join(miss)))
            continue
        diff = []
        for k in sorted(set(ra) & set(rb)):
            # secret 不比内容；updated_at 只说明「哪边写过」，不是配置差异
            fields = {c: v for c, v in ra[k].items() if c not in ("secret", "password", "updated_at")}
            fields_b = {c: v for c, v in rb[k].items() if c not in ("secret", "password", "updated_at")}
            if fields != fields_b:
                diff.append(k)
        if diff:
            warn("%s 有 %d 行内容不同（密钥字段已忽略）：%s"
                 % (table, len(diff), ", ".join(diff[:8])))
        else:
            ok("%s 行与内容一致（%d 行）" % (table, len(ra)))


# --------------------------------------------------------------------------
# 4. backend/.env 只比键与可解释的路径差异
# --------------------------------------------------------------------------
def _env_map(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def check_env(src_my: str, dst_my: str) -> None:
    print("\n== my_app backend/.env")
    a = _env_map(os.path.join(src_my, "backend", ".env"))
    b = _env_map(os.path.join(dst_my, "backend", ".env"))
    if not a or not b:
        warn("读不到 .env，跳过")
        return
    miss = sorted(set(a) - set(b))
    if miss:
        fail(".env 缺键：%s" % ", ".join(miss))
    else:
        ok(".env 键齐全（源 %d / 目标 %d）" % (len(a), len(b)))
    diff = [k for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    if diff:
        info(".env 值不同（端口/路径等按 02 调整，需人工确认）：%s" % ", ".join(diff[:10]))


def main() -> int:
    ap = argparse.ArgumentParser(description="对比切换前后两棵树的运行时状态（只读）")
    ap.add_argument("--source", default=r"D:\01_olbr_portal", help="上一版生产树")
    ap.add_argument("--target", default=r"D:\02_olbr_portal", help="当前生产树")
    args = ap.parse_args()

    src, dst = os.path.abspath(args.source), os.path.abspath(args.target)
    if not os.path.isdir(src):
        print("源目录不存在：%s" % src)
        return 3
    if not os.path.isdir(dst):
        print("目标目录不存在：%s" % dst)
        return 3

    # 两棵树的历史布局不同：门户在 01 是 <root>\liberia-noc_soc-数据看板统一门户，
    # 在 02 是 <root>\apps\portal；my_app 在 01 是 <root>\Task\my_app，在 02 是 <root>\apps\my_app。
    src_portal = _first_dir(os.path.join(src, "liberia-noc_soc-数据看板统一门户"), os.path.join(src, "apps", "portal"))
    dst_portal = _first_dir(os.path.join(dst, "apps", "portal"), os.path.join(dst, "liberia-noc_soc-数据看板统一门户"))
    src_my = _first_dir(os.path.join(src, "Task", "my_app"), os.path.join(src, "apps", "my_app"))
    dst_my = _first_dir(os.path.join(dst, "apps", "my_app"), os.path.join(dst, "Task", "my_app"))

    print("源：%s\n  门户=%s\n  my_app=%s" % (src, src_portal or "-", src_my or "-"))
    print("目标：%s\n  门户=%s\n  my_app=%s" % (dst, dst_portal or "-", dst_my or "-"))

    check_portal(src_portal, dst_portal)
    check_myapp_config(src_my, dst_my)
    check_local_authority(src_my, dst_my)
    check_env(src_my, dst_my)

    print("\n==== 汇总：PASS(无 FAIL)  FAIL=%d  WARN=%d  INFO=%d" % (len(FAIL), len(WARN), len(INFO)))
    for line in FAIL:
        print("  FAIL " + line)
    return 2 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
