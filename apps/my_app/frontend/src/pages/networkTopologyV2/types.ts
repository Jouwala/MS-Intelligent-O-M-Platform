/* ============================================================================
 * OLIB GIS 拓扑（?page=olibTopo_v2）—— 类型定义
 * ============================================================================
 * 数据链路：
 *   topologyData.ts（667 个 PDF 站点，相对坐标）
 *     + public/olib/olib-site-geo.json（站点名 → master_site_db 真实经纬度）
 *     → GeoSite[]（本文件的视图模型）
 *
 * ⚠ 经纬度来自静态映射快照，不是 PDF 坐标，也不是运行时接口。
 *   生成与校验见 scripts/gen-olib-site-geo.mjs。
 * ========================================================================== */
import type { CoreLinkType, TopoSite } from '../networkTopology/topologyData'

/** 站点名与主数据站点的匹配方式；missing = 映射表与站点清单位置不一致。 */
export type GeoMatchMethod = 'name-exact' | 'alias' | 'unmatched' | 'ambiguous' | 'missing'

/** public/olib/olib-site-geo.json 的一条记录（按 OLIB 站点在 SITES 里的下标 i 对齐）。 */
export interface GeoRecord {
  i: number
  name: string
  method: Exclude<GeoMatchMethod, 'missing'>
  verified: boolean
  siteId?: string
  gisName?: string
  lat?: number
  lng?: number
  county?: string
  region?: string
  /** 别名表里的人工说明 */
  note?: string
  /** 未匹配 / 歧义原因 */
  reason?: string
  candidates?: string[]
}

export interface GeoFile {
  generatedAt: string
  source: Record<string, string>
  summary: {
    total: number
    located: number
    'name-exact': number
    alias: number
    unmatched: number
    ambiguous: number
  }
  records: GeoRecord[]
}

/** 页面视图模型：保留 PDF 侧字段（name/x/y/5g/isp/sector/core），叠加 GPS 匹配结果。 */
export interface GeoSite extends TopoSite {
  /** 在 SITES 里的下标，用作稳定选中键 */
  index: number
  located: boolean
  method: GeoMatchMethod
  verified: boolean
  siteId?: string
  gisName?: string
  lat?: number
  lng?: number
  county?: string
  region?: string
  geoNote?: string
  reason?: string
  candidates?: string[]
}

export type GeoViewMode = 'geo' | 'physical' | 'logical' | 'core'

/* ---------------------------------------------------------------------------
 * PDF 链路图层（第 1 页数字化，见 backend/scripts/gen_olib_fiber_links.py）
 *
 * public/olib/olib-fiber-links.json 里是「站点对」边：a / b 是站点在 SITES 里的
 * 下标，c 是群组、t 是 PDF 图例里的原始类型。页面按两端站点的真实 GPS 画直线，
 * 所以站点表增删后要么重跑脚本，要么靠 sites 里的站名核对把过期边判为无效。
 * ------------------------------------------------------------------------- */
export type FiberGroup = 'base' | 'f40' | 'f10' | 'ipran' | 'p26' | 'plan'

export interface FiberEdge {
  a: number
  b: number
  c: FiberGroup
  /** PDF 图例里的原始类型，如 40G / 10G 规划 / P26B / DWDM / 基础链路 */
  t: string
  /** 1 = 虚线（规划中） */
  d?: 0 | 1
}

export interface FiberFile {
  generatedAt: string
  source: string
  page: number
  method: string
  anchors: number
  summary: {
    segments: number
    edges: number
    byGroup: Record<string, number>
    linkedSites: number
  }
  /** 站点下标 → 站名，用于核对站点表是否已变动 */
  sites: Record<string, string>
  edges: FiberEdge[]
}

/** 画到地图上的一条 PDF 链路（两端都已定位） */
export interface FiberLink {
  id: string
  a: GeoSite
  b: GeoSite
  group: FiberGroup
  type: string
  dash: boolean
}

export interface GeoLink {
  /** `${a.index}-${b.index}-${type}-${offset}` */
  id: string
  a: GeoSite
  b: GeoSite
  type: CoreLinkType
  color: string
  /** 平行链路（同两端不同链路类型）的垂直偏移，单位：度 */
  offset: number
}
