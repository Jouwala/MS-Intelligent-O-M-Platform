"""dispatch 测试夹具：sys.path 与假 API 实例。

- 把 `apps/control-plane/backend` 放进 sys.path，使 `dispatch` 包可导入
  （作为包运行时 pytest 会自己插入，这里兜底）；
- `fake` 夹具默认 retry_limit=2、sleep 立即返回（避免测试里的退避等待）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dispatch.tests.fake_github import HEAD_SHA, MAIN_SHA, MERGE_SHA, FakeGitHub  # noqa: E402


@pytest.fixture()
def fake() -> FakeGitHub:
    return FakeGitHub(main_sha=MAIN_SHA)


@pytest.fixture()
def merged_pr(fake: FakeGitHub) -> dict:
    """一个已合入 main 的 PR（带 merge sha 与 builder 标签）。"""
    return fake.seed_pull(
        31,
        HEAD_SHA,
        merge_sha=MERGE_SHA,
        labels=("agent:muse-2",),
        body="Closes #1",
    )
