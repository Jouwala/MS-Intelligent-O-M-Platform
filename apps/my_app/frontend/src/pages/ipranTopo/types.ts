/** UME 拓扑页的数据与布局类型（与 /api/ipran-topo/graph 的返回结构对齐）。 */

/** 四张 UME 逻辑拓扑层；与后端 layers.LAYERS 的 key 一一对应，非法值后端会 400。 */
export type LayerKey = 'ipran' | 'ipcore' | 'metro' | 'dwdm'

/** 0 层外上游 / 1 核心 / 2 汇聚 / 3 接入（层级名称按层翻译，见 layerDefs.ts） */
export type Tier = 0 | 1 | 2 | 3

export type Sev = 'crit' | 'major' | 'minor' | 'warn' | 'ok'

/** 站点坐标匹配结果：exact 精确 / fuzzy 近似（需人工确认）/ miss 库内无坐标 / ext 层外设备 */
export type GeoHow = 'exact' | 'fuzzy' | 'miss' | 'ext'

export interface TopoNode {
  id: string
  name: string
  site: string
  tier: Tier
  model: string
  ip: string
  /** 1 = connectionState 正常 */
  up: number
  crit: number
  major: number
  minor: number
  warn: number
  sev: Sev
  lat: number | null
  lon: number | null
  geo: GeoHow
  geoName: string
  /** 端口级度数（折叠前） */
  deg: number
}

export interface TopoEdge {
  /** 端点 node id */
  s: string
  t: string
  /** 这对机框折叠了几条物理链路 */
  n: number
  /** 组内最小端口速率（GB）；解析不出为 null */
  gb: number | null
  /** 组内 down 的链路条数 */
  down: number
  crit: number
  major: number
  /** 最多 12 条链路名，供悬停明细 */
  ports: string[]
  /**
   * 接收/发送利用率（%）。只有 `?traffic=1` 的响应才有这两项；缺省即界面显示 N/A。
   * 禁止用端口速率 gb 去填。方向口径见 types 顶部与后端 topo_traffic.py：
   * tx = 源设备该端口的发送（源→宿），rx = 同一端口的接收（宿→源）。
   */
  rxUtil?: number | null
  txUtil?: number | null
  /** 命中的物理链路明细（折叠边里每条各自的方向利用率），最多 12 条 */
  utilPorts?: UtilPort[]
}

/** 一条物理链路的流量利用率明细（由后端匹配，前端不重算） */
export interface UtilPort {
  /** 参与测量的端口名（`side` 说明取的是链路哪一端） */
  port: string
  me: string
  /** src = 本条边的源设备端口；dst = 源侧没采到，退到宿侧 */
  side: 'src' | 'dst'
  gb: number | null
  rx: number | null
  tx: number | null
}

export interface TopoMeta {
  /** 后端回填的层 key / 层名，用于核对响应与当前页签是否一致 */
  layer?: string
  layerName?: string
  fetchedAt?: string
  elapsedSec?: number
  cached?: boolean
  /** 上游取数失败、当前为上次成功数据 */
  stale?: boolean
  ne: number
  ext: number
  rawLinks: number
  foldedLinks: number
  tierCounts: Record<string, number>
  geo: Record<string, number>
  geoMissSites: string[]
  geoFuzzyPairs: { site: string; matched: string }[]
  critNe: number
  warnings: string[]
  /** 流量负载模式的取数情况（仅 `?traffic=1`）；无此字段表示本次响应没带流量数据 */
  traffic?: TrafficMeta
}

/** 端口流量贴图结果（后端 topo_traffic.annotate_edges 的 meta.traffic） */
export interface TrafficMeta {
  /** KPI 采样时刻（各表最新一个 Begin Time） */
  sampledAt?: string | null
  /** 参与匹配的 15 分钟表 */
  tables: string[]
  /** 本次读到的原始行数（含 Repeat 的两个方向与 SubPort） */
  rows: number
  totalEdges: number
  matchedEdges: number
  totalPorts: number
  matchedPorts: number
  /** 采到了流量但端口速率解析不出（如 ETH 口）的数量 */
  noBandwidthPorts: number
}

export interface TopoResp {
  nodes: TopoNode[]
  edges: TopoEdge[]
  meta: TopoMeta
}

export interface Pt { x: number; y: number }

/** 分层视图的分带标注；labelKey 由页面翻成文案，count 用于「接入 78 个 · 3 行」这类补充 */
export interface TopoBand {
  y: number
  labelKey: string
  count?: number
  rows?: number
}

export interface LayoutResult {
  pos: Pt[]
  bands: TopoBand[]
  /** 地理视图：有坐标的节点数 / 画布上方的地图区高度 */
  geoMatched?: number
  geoBoxH?: number
}

export type LayoutKey = 'force' | 'geo' | 'tier'
/** 着色：层级 / 在线状态 / 告警级 / 流量负载（Weathermap，2026-11-19 新增） */
export type ColorMode = 'tier' | 'up' | 'sev' | 'traffic'

/** 邻接索引：adj[i] = [{ n: 邻居下标, e: 边下标 }] */
export interface TopoIndex {
  adj: { n: number; e: number }[][]
  /** 接入点的主归属汇聚下标（-1 表示无）。仅用于布局排序，不代表唯一上游 */
  parent: number[]
  /** 每个节点的全部上游邻居（去重）：双归属设备会同时保留两个上游 */
  upstream: number[][]
  /** 核心+汇聚节点，按「层级 → 度数」排序 */
  hubs: number[]
  hubOrder: Record<number, number>
  byId: Record<string, number>
}
