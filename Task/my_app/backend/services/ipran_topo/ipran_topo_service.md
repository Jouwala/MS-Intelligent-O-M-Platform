# IP RAN 光纤拓扑服务（ipran_topo_service.py）

> 页面：`?page=ipran_topo`　|　路由：`GET /api/ipran-topo/graph`
> 真相源：`ipran_topo_service.py` 模块 docstring —— 本文是给人看的使用与排障说明，
> 口径细节以代码注释为准。

## 1. 它做什么

一次请求完成「调 UME 8 次 → 折叠平行链路 → 分层 → 匹配站点坐标 → 汇总告警」，
把结果作为一张图的 `nodes` / `edges` / `meta` 返回。前端只负责布局与绘制，不再逐条查。

| 项 | 实测值（2026-11-10） |
| --- | --- |
| 层内网元 | 93 |
| 层外设备 | 7（89MW01/02 微波、HQ-RAN-SW5960-1/2、ODC-M6000-8S-DCW1、HQ-M6K-8S_Plus_PS01/02） |
| 原始链路 | 167 |
| 折叠后唯一边 | 141（一对机框最多 10 条物理链路，折叠掉 11 对） |
| 分层 | 核心 4 / 汇聚 11 / 接入 78 |
| 站点坐标命中 | 77 / 93（74 精确 + 3 近似） |
| 带 critical 告警网元 | 74 / 93（80%） |
| 一次聚合耗时 | 0.6~3.0s（会话热时 0.6s） |

## 2. 数据源与鉴权

- UME：`https://10.100.2.99:28001`（自签 TLS），层 `lid=1293626099480711168`。
- 鉴权复用 `backend/services/ipran_ume_kpi_service.py::UmeSessionClient`，凭据组 `ipran_ume`。
- 站点坐标取 PG/SQLite 的 `kml_mw_links`（`kind='site'`）。它是
  `frontend/public/mw_ume_orange_links.geojson` 的源表，由
  `backend/services/mw_ume/mw_links_store.py` 从 KML 写入。

两个实测坑（改动前务必知道）：

1. `forgerydefense` 请求头**不带 `-28001` 后缀**，带后缀一律 403。
2. 共享会话探活**不能打 `LOGIN_PORTAL`**（那里恒 200，会得到假绿），必须打真实业务端点。

## 3. 缓存与刷新

- 进程内缓存 `CACHE_TTL_SEC = 120` 秒，并发请求串行化（`_fetch_lock`），
  避免每个用户每次刷新都去打 NMS。
- `GET /api/ipran-topo/graph?refresh=1` 同时清图缓存与站点坐标缓存（页面「刷新」按钮）。
- 上游取数失败时**回退上一版成功数据**，并在 `meta.stale = true` +
  `meta.warnings` 里显式标注 —— 界面必须把它显示出来，不能当新鲜数据看。
- KML 重新上传后站点坐标会变：`refresh=1` 即可，无需重启后端。

## 4. 返回结构

```jsonc
{
  "nodes": [{
    "id": "…", "name": "M6K_2S16_Chocolate_City_UPE99", "site": "Chocolate City",
    "tier": 1,            // 0 层外 / 1 核心 / 2 汇聚 / 3 接入
    "model": "M6000-2S16", "ip": "10.16.1.x",
    "up": 1,              // connectionState == 1
    "crit": 1, "major": 2, "minor": 1, "warn": 0,
    "sev": "crit",        // 最高级告警，仅用于徽标与排行，不做节点填色
    "lat": 6.31, "lon": -10.8,    // 未命中为 null
    "geo": "exact",       // exact / fuzzy / miss / ext
    "geoName": "…",       // 命中的库内站名（fuzzy 时用于人工核对）
    "deg": 4              // 端口级度数（折叠前）
  }],
  "edges": [{
    "s": "nodeId", "t": "nodeId",
    "n": 3,               // 该对机框折叠了几条物理链路
    "gb": 100,            // 组内最小端口速率（GB）；解析不出为 null
    "down": 0,            // 组内 down 的链路条数
    "crit": 0, "major": 0,// 组内链路告警合计
    "ports": ["…"]        // 最多 12 条链路名，供悬停明细
  }],
  "meta": {
    "fetchedAt": "2026-11-10T02:11:00+00:00", "elapsedSec": 2.31, "cached": false,
    "ne": 93, "ext": 7, "rawLinks": 167, "foldedLinks": 141,
    "tierCounts": {"0": 7, "1": 4, "2": 11, "3": 78},
    "geo": {"exact": 74, "fuzzy": 3, "miss": 16, "ext": 7},
    "geoMissSites": ["…"], "geoFuzzyPairs": [{"site": "Goodride", "matched": "Goodridge"}],
    "critNe": 75,
    "warnings": ["alarm_saturated: …", "geo_miss: …", "geo_fuzzy: …"]
  }
}
```

`warnings` 的三种前缀：`alarm_saturated`（告警饱和，颜色失去区分度）、
`geo_miss`（站点无坐标）、`geo_fuzzy`（近似匹配需人工确认）、
`upstream_unreachable`（回退到上次成功数据）。

## 5. 口径要点（都有实测依据，勿凭直觉改）

- **折叠取最小端口速率**：一对机框之间有 9 条、10 条链路时，看的是「还剩多少条最窄的」，
  取最大值会虚报容量。
- **端口速率从链路名解析**，不用 UME 的 `rate` 字段 —— 那是枚举码，含义未获确认。
- **模糊匹配带数字守卫**：`12th Street` 与 `18th Street` 相似度 0.94，但这是两条街。
- **告警不做节点填色**：80% 的网元带 critical 告警，填色等于全红。
- **层外设备必须保留**：89MW02 的度达 10，丢了对端会让核心节点看起来孤立。
- **层外设备名从 UME 取**，不从链路名切分（设备名含下划线、板卡名也用 `-`，切分必错）。

## 6. 排障

| 症状 | 先看 |
| --- | --- |
| 页面全空 + 「上游取数失败」 | 后端日志 `[IPRAN-TOPO]` / `[LOGIN]`；凭据组 `ipran_ume` 是否有效（账号锁号 1440 分钟风险见 `05-security-and-credentials.md`） |
| 坐标大量为空（`geo_miss` 很多） | `kml_mw_links` 是否有 `kind='site'` 行；`refresh=1` 重载 |
| 结果一直是旧的 | `meta.fetchedAt` / `meta.cached`；改完代码要重启 7038 |
| 告警数全是 0 | `/api/fm-active/v1/north/statistic/me` 的 `maintainstatus=0` 参数是否被上游改动 |

改完本文件**必须重启 7038**（uvicorn 无 reload 时不会热更；watchdog 会自动拉起）：
只杀 7038 的 uvicorn PID，不要用 `stop.bat`（会连 10 个 worker 一起停）。
