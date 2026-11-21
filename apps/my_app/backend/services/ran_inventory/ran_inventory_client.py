# -*- coding: utf-8 -*-
"""RAN 资产台账的数据采集层：会话、鉴权、分页取数。

数据源（同目录两份抓包）：

| 文件 | 接口 | 内容 |
| --- | --- | --- |
| `url.txt` | `POST /api/ranim/v1/inventorys/conditionQuery/{hw,sw,opt}` | 硬件单元 / 软件包 / 光模块 |
| `online_url.txt` | `GET /api/res/v1/ranmes?queryDn=model:subnetwork` | 网元表（含 `connectionState`） |

认证不照抄抓包里的 `-b`（那是浏览器会话 Cookie，会过期且属敏感值）：
走项目既有的 `auth_manager.get_auth(probe=...)` + `credential_store` 的 `zte_ume` 组，
与 `RANElementManagement` / `ran_ume_service` 共享跨进程会话缓存；探活打本服务
自己的业务端点（登录首页会返回误导性的 200）。

聚合与缓存见 `ran_inventory_service.py`。
"""
from __future__ import annotations

import json
import logging
import os

import requests

logger = logging.getLogger("ran_inventory_service")

try:  # 上游是内网 HTTPS 自签证书，对应抓包的 --insecure
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:  # noqa: BLE001
    pass

HOST = os.environ.get("RAN_INV_HOST", "10.100.28.27")
PORT = int(os.environ.get("RAN_INV_PORT", "28001"))
CONNECT_TIMEOUT = float(os.environ.get("RAN_INV_CONNECT_TIMEOUT", "15"))
READ_TIMEOUT = float(os.environ.get("RAN_INV_READ_TIMEOUT", "300"))
PAGE_SIZE = max(100, int(os.environ.get("RAN_INV_PAGE_SIZE", "1000")))

INV_KINDS = ("hw", "sw", "opt")
INV_PATH = "/api/ranim/v1/inventorys/conditionQuery/{kind}"
NE_PATH = (
    "/api/res/v1/ranmes?queryDn=model:subnetwork"
    "&operation=operation.rem.rnm.tools"
    "&includeAttr=id,name,nbiId,nbiIdDn,moc,mimVersion,ipAddress,maintainStatus,"
    "version,connectionState,description,displayName,protocol,subType,"
    "virtualizationId,subnetwork_nbiId"
)

_SESSION = requests.Session()
_SESSION.trust_env = False
_SESSION.verify = False


class AuthExpired(RuntimeError):
    """业务端点返回 401/403：会话在探活之后、取数之前失效。"""


def _headers(cookie: str, forgery_token: str, referer: str) -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
        ),
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "language-option": "en-US",
        "timeZoneInfo": "Default,Atlantic/Reykjavik,",
        "Cookie": cookie,
        "forgerydefense": forgery_token,
    }


def _inv_headers(cookie: str, token: str) -> dict:
    return _headers(cookie, token, f"https://{HOST}:{PORT}/iui/ranim/")


def _ne_headers(cookie: str, token: str) -> dict:
    return _headers(cookie, token, f"https://{HOST}:{PORT}/iui/ranrnmiui/")


def _probe(cookie: str, forgery_token: str) -> bool:
    """探活：只取状态行就关掉，不下载整张资产表。"""
    body = {
        "pageInfo": {"pageSize": 1, "pageIndex": 1},
        "sortInfo": [], "singleColFilters": [], "neInfo": [],
    }
    try:
        r = _SESSION.post(
            f"https://{HOST}:{PORT}{INV_PATH.format(kind='hw')}",
            headers=_inv_headers(cookie, forgery_token),
            data=json.dumps(body), timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            stream=True,
        )
        code = r.status_code
        r.close()
        alive = code not in (401, 403)
        if not alive:
            logger.info("[RAN-INV] 共享会话已失效（探活 inventory -> %s）", code)
        return alive
    except Exception as e:  # noqa: BLE001
        logger.warning("[RAN-INV] 会话探活异常（%s: %s），按不可用处理",
                       type(e).__name__, e)
        return False


def _runtime_credentials():
    from backend.services.credential.credential_store import get_credential
    return get_credential(
        "zte_ume",
        os.environ.get("RAN_INV_USER", "NOC"),
        os.environ.get("RAN_INV_PASS", ""),
        env_user="RAN_INV_USER", env_secret="RAN_INV_PASS",
    )


class Session:
    """一轮取数用的会话凭据（不放在模块全局，避免并发请求互相串）。"""

    def __init__(self) -> None:
        self.cookie = ""
        self.token = ""
        self.source = ""

    def acquire(self, force_refresh: bool = False) -> "Session":
        from backend.services.auth_manager import get_auth
        user, pwd = _runtime_credentials()
        if not user or not pwd:
            raise RuntimeError(
                "zte_ume 凭据为空：请在「系统设置 → 凭据」维护 RAN UME 账号口令"
            )
        r = get_auth(HOST, PORT, user, pwd, probe=_probe, force_refresh=force_refresh)
        self.cookie = r["cookie"]
        self.token = r["forgery_token"]
        self.source = ("缓存" if r["from_cache"] else
                       ("重登（探活失败）" if r.get("from_relogin") else "重登"))
        logger.info("[RAN-INV] 鉴权成功（来源: %s）", self.source)
        return self


def _post_inv(sess: Session, kind: str, page: int, size: int) -> dict:
    body = {
        "pageInfo": {"pageSize": size, "pageIndex": page},
        "sortInfo": [{"key": "userlabel", "sortDirection": "asc"}],
        "singleColFilters": [], "neInfo": [],
    }
    r = _SESSION.post(
        f"https://{HOST}:{PORT}{INV_PATH.format(kind=kind)}",
        headers=_inv_headers(sess.cookie, sess.token),
        data=json.dumps(body), timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    if r.status_code in (401, 403):
        raise AuthExpired(f"{kind} 资产接口返回 HTTP {r.status_code}")
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, dict) or "data" not in j:
        raise RuntimeError(
            f"{kind} 资产接口返回结构异常（HTTP {r.status_code}）：{r.text[:160]!r}"
        )
    return j


def fetch_inventory(sess: Session, kind: str) -> tuple[list, int]:
    """分页拉全量。返回 (rows, 上游声明总数)。

    翻页以「拿到的行数」收敛，避免上游 total 抖动时空转；
    500 页上限只是防死循环的兜底（实测 16 页）。
    """
    rows: list = []
    total = 0
    page = 1
    while True:
        j = _post_inv(sess, kind, page, PAGE_SIZE)
        total = int((j.get("state") or {}).get("total") or 0)
        part = j.get("data") or []
        rows.extend(r for r in part if isinstance(r, dict))
        if len(part) < PAGE_SIZE or (total and len(rows) >= total):
            break
        page += 1
        if page > 500:
            raise RuntimeError(f"{kind} 分页超过 500 页，疑似上游 total 异常")
    return rows, total


def fetch_network_elements(sess: Session) -> list:
    r = _SESSION.get(
        f"https://{HOST}:{PORT}{NE_PATH}",
        headers=_ne_headers(sess.cookie, sess.token),
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    if r.status_code in (401, 403):
        raise AuthExpired(f"网元表返回 HTTP {r.status_code}")
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"网元表返回结构异常：期望 list，实际 {type(data).__name__}")
    return [x for x in data if isinstance(x, dict)]
