# -*- coding: utf-8 -*-
"""Site Outage 防伪令牌：回灌时丢掉旧副本，请求头跟当前 Cookie 走。

2026-11-17 18:01 起截图连续失败。登录是成功的，门户 toast 是
「该请求因跨站点访问被禁止」——HttpOnly 的 forgerydefense Cookie 已换新，
localStorage 里回灌的 csrftoken 还是旧的。

本文件不连网管、不读真实会话文件。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "services"))

import site_outage_session as s  # noqa: E402


def test_cookie_header_prefers_ported_name():
    header = "a=1; forgerydefense=bare; forgerydefense-28001=ported"
    assert s.csrf_token_from_cookie_header(header) == "ported"


def test_cookie_header_accepts_bare_name():
    assert s.csrf_token_from_cookie_header("forgerydefense=bare") == "bare"


def test_cookie_header_empty():
    assert s.csrf_token_from_cookie_header("") == ""
    assert s.csrf_token_from_cookie_header(None) == ""
    assert s.csrf_token_from_cookie_header("other=1") == ""


def test_restore_drops_csrf_copies():
    items = [
        {"name": "csrftoken", "value": "old"},
        {"name": "forgerydefense", "value": "old"},
        {"name": "language-option", "value": "zh-CN"},
        {"name": "", "value": "x"},
        "not-a-dict",
    ]
    kept = s.storage_items_for_restore(items)
    assert [it["name"] for it in kept] == ["language-option"]


if __name__ == "__main__":
    test_cookie_header_prefers_ported_name()
    test_cookie_header_accepts_bare_name()
    test_cookie_header_empty()
    test_restore_drops_csrf_copies()
    print("ok")
