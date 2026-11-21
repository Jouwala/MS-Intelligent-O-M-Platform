"""控制面测试包。

加 `__init__.py` 是刻意的：`apps/agent-worker/tests` 与这里两个目录都叫 `tests`，
且仓库里没有统一 pytest 配置；没有包标记时两个 `conftest.py` 会以同名模块
`conftest` 互相覆盖，导致 `python -m pytest apps/agent-worker/tests
apps/control-plane/backend/tests` 收集失败。加包标记后本目录 conftest 的模块名
变成 `tests.conftest`，与 agent-worker 的 `conftest` 不再冲突。
"""
