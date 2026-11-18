# -*- coding: utf-8 -*-
"""清理任务路径守卫（2026-11-18 切生产后回归）。

背景：`log_cleanup.py` / `tmp_cleanup.py` 属于 `backend/services/cleanup/` 子包，
`PROJECT_ROOT` 曾写成 `Path(__file__).parent.parent.parent` —— 只退到 `backend/`。
于是 `LOG_DIR` = `my_app/backend/log`、`TMP_DIR` = `my_app/backend/TMP`（都不存在），
清理任务每次「成功」却一个文件都没处理，日志与探针在磁盘上无限增长且无告警。

这与 `test_cxo_cache_paths.py` 是同一类事故（模块搬进子包后少退一层），一起锁住：
1. 两个清理模块的根目录都必须是 `my_app/`；
2. 目标目录必须是 `my_app/log`、`my_app/TMP`，不能再出现 `backend/` 段；
3. 目标目录允许暂时不存在（首次部署），但父目录必须是 my_app 根。
"""
from backend.services.cleanup import log_cleanup, tmp_cleanup


def test_log_cleanup_root_is_app_root():
    assert log_cleanup.PROJECT_ROOT.name == "my_app"
    assert log_cleanup.PROJECT_ROOT.parent.name == "apps"
    assert log_cleanup.LOG_DIR == log_cleanup.PROJECT_ROOT / "log"
    assert "backend" not in log_cleanup.LOG_DIR.parts, "日志目录不能落在 my_app/backend/log"


def test_tmp_cleanup_root_is_app_root():
    assert tmp_cleanup.PROJECT_ROOT.name == "my_app"
    assert tmp_cleanup.TMP_DIR == tmp_cleanup.PROJECT_ROOT / "TMP"
    assert "backend" not in tmp_cleanup.TMP_DIR.parts, "探针目录不能落在 my_app/backend/TMP"
