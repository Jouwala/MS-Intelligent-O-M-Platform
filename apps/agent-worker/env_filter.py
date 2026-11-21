"""任务命令环境白名单（#78）。

Worker 进程自身的环境里可能带有 Secret / 生产凭据（webhook key、PAT、
云凭据等）。任务命令属于半可信输入，**默认不继承** Worker 的完整环境：
`build_task_env()` 只放行一份最小白名单（进程正常运行必需的系统变量），
其余变量一律不进任务子进程——默认拒绝，不需要枚举 Secret 的名字，
新出现的敏感变量天然被挡住。

部署确有额外需求时，由调用方（Executor 的 `env_extra_allowed`）显式追加
变量名，追加项会随执行日志留痕，便于审计。白名单只做「复制已有的值」，
从不臆造变量：源环境里没有的变量不会凭空出现在子进程里。

注意 PWD / OLDPWD / SHLVL 这类 shell 簿记变量不在白名单内：它们描述的
是 Worker 自己的 shell 状态，带进任务子进程只会误导子进程的 cwd 判断。
"""

from __future__ import annotations

import os
from typing import Iterable, Mapping, Optional

#: POSIX 最小白名单：路径、身份、语言、时区、临时目录这类运行必需项。
_POSIX_ALLOWED = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TZ",
        "TMPDIR",
        "LANG",
        "LANGUAGE",
    }
)

#: POSIX 前缀放行：全部 LC_* 语言环境变量（LC_ALL / LC_CTYPE / …）。
_POSIX_PREFIXES = ("LC_",)

#: Windows 最小白名单（匹配时大小写不敏感）：系统目录、临时目录、
#: 用户目录与进程运行必需项；任务命令里的 git / node / python 都依赖这些。
_WINDOWS_ALLOWED = frozenset(
    {
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "OS",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "USERNAME",
        "USERDOMAIN",
        "COMPUTERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
    }
)


def _allowed(key: str, extra_upper: frozenset) -> bool:
    if key.upper() in extra_upper:
        return True
    if os.name == "nt":
        return key.upper() in _WINDOWS_ALLOWED
    return key in _POSIX_ALLOWED or key.startswith(_POSIX_PREFIXES)


def build_task_env(
    source: Optional[Mapping[str, str]] = None,
    extra_allowed: Iterable[str] = (),
) -> dict:
    """从 `source`（默认 Worker 自身 os.environ）过滤出任务子进程的环境。

    只保留白名单 + `extra_allowed` 显式追加的变量；其余全部丢弃。
    返回新 dict，值原样复制，不修改源环境。
    """
    src = os.environ if source is None else source
    extra_upper = frozenset(name.upper() for name in extra_allowed)
    return {k: v for k, v in src.items() if _allowed(k, extra_upper)}
