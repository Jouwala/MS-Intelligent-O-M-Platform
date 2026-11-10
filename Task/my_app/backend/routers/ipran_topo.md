# IP RAN 拓扑路由（routers/ipran_topo.py）

前缀：`/api/ipran-topo`。只读，无写接口。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/graph` | 完整图数据（网元 + 折叠后的边 + meta）。`refresh=1` 绕过图缓存与站点坐标缓存 |

实现与口径见 `backend/services/ipran_topo/ipran_topo_service.py` 与同目录 `.md`。
失败时返回**空图 + `meta.warnings[load_failed]`**（HTTP 200），不抛 500 ——
前端据此渲染错误条，与项目内其他看板路由一致。

本路由**不在** `_PUBLIC_API` 白名单内，需要登录。
