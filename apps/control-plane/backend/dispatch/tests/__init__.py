"""dispatch 模块测试包。

**为什么是包（有 `__init__.py`）**：本目录与 `apps/agent-worker/tests` 会在
同一个 pytest 进程里一起跑（CI 的 agent-tests 作业）。两个非包测试目录各有
`conftest.py` 时，pytest 都把 conftest 注册成顶层模块名 `conftest`，
agent-worker 测试里的 `from conftest import ...` 会解析到错误的那个文件
（实测 ImportError）。把本目录变成包后，conftest 的模块名是
`dispatch.tests.conftest`，不再抢占顶层 `conftest`。

`fake_github` 因此按包路径导入：`from dispatch.tests.fake_github import ...`。
"""
