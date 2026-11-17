/* ============================================================================
 * OLIB GIS 拓扑 —— 数据装配
 * ============================================================================
 * 把「PDF 站点清单」与「GPS 映射表」合成页面视图模型，并把 PDF 第 2 页梳理的
 * 骨干链路（CORE_LINKS）落到真实经纬度上。
 *
 * ⚠ 关于链路：原始资料只有「哪两个骨干核心之间有链路」，没有光缆实际走向。
 *   所以本页画的是**连接关系示意直线**，不是光缆路由；端点没有 GPS 的链路
 *   直接跳过，并在图例里报数（见 buildGeoLinks 的 skipped）。
 * ========================================================================== */
import {
  CORE_LINKS, CORE_NODES, LINK_COLORS, SITES, normalizeName,
  type CoreLinkType, type CoreNode, type SiteFilter, type TopoSite,
} from '../networkTopology/topologyData'
import { siteFuzzyScore } from '../networkTopology/siteMatch'
import type { FiberFile, FiberLink, GeoFile, GeoLink, GeoRecord, GeoSite } from './types'

/** 合并站点清单与 GPS 记录。file 为空（加载中）时全部标为未定位。 */
export function buildGeoSites(file: GeoFile | null): GeoSite[] {
  const byIndex = new Map<number, GeoRecord>()
  if (file) for (const r of file.records) byIndex.set(r.i, r)

  return SITES.map((s, index) => {
    const base: GeoSite = { ...s, index, located: false, method: 'missing', verified: false }
    const r = byIndex.get(index)
    /* 按位置对齐后还要核对站名：topologyData.ts 增删站点后映射表必须重生成，
       否则会把 A 站的 GPS 安到 B 站头上。宁可整条判为未定位。 */
    if (!r || r.name !== s.name) return { ...base, reason: 'mapping-out-of-date' }
    if (r.method === 'unmatched') return { ...base, method: 'unmatched', reason: r.reason }
    if (r.method === 'ambiguous') {
      return { ...base, method: 'ambiguous', reason: r.reason, candidates: r.candidates }
    }
    if (typeof r.lat !== 'number' || typeof r.lng !== 'number') {
      return { ...base, method: r.method, reason: 'no-coordinates' }
    }
    return {
      ...base,
      located: true,
      method: r.method,
      verified: r.verified,
      siteId: r.siteId,
      gisName: r.gisName,
      lat: r.lat,
      lng: r.lng,
      county: r.county,
      region: r.region,
      geoNote: r.note,
    }
  })
}

/** 站点类型：与 v1 的 siteColor 同口径（core > isp > 5g > 普通）。 */
export function siteKind(s: TopoSite): 'core' | 'isp' | '5g' | 'normal' {
  if (s.core) return 'core'
  if (s.isp) return 'isp'
  if (s.is5g) return '5g'
  return 'normal'
}

/** 骨干链路 → 真实经纬度。返回可绘制链路和被跳过的链路数（端点无 GPS）。 */
export function buildGeoLinks(sites: GeoSite[]): { links: GeoLink[]; skipped: number } {
  const byName = new Map<string, GeoSite>()
  for (const s of sites) {
    if (s.located) byName.set(normalizeName(s.name), s)
  }
  const nodeSite = (node: CoreNode): GeoSite | undefined =>
    node.site ? byName.get(normalizeName(node.site)) : undefined

  const nodeMap = new Map<string, GeoSite>()
  for (const node of CORE_NODES) {
    const site = nodeSite(node)
    if (site) nodeMap.set(node.id, site)
  }

  const pairSeen = new Map<string, number>()
  const links: GeoLink[] = []
  let skipped = 0

  for (const [aId, bId, type] of CORE_LINKS as [string, string, CoreLinkType][]) {
    const a = nodeMap.get(aId)
    const b = nodeMap.get(bId)
    if (!a || !b || a.index === b.index) {
      skipped++
      continue
    }
    const pair = a.index < b.index ? `${a.index}-${b.index}` : `${b.index}-${a.index}`
    const nth = pairSeen.get(pair) ?? 0
    pairSeen.set(pair, nth + 1)
    /* 同一对端点有多种链路类型时（如 Gbarnga–Botota 同时有 DWDM 与铁路），
       给后一条一个垂直偏移，否则两条线完全重合、只能看到一条。 */
    const offset = nth === 0 ? 0 : (nth % 2 === 1 ? 1 : -1) * Math.ceil(nth / 2)
    links.push({
      id: `${pair}-${type}-${nth}`,
      a, b, type,
      color: LINK_COLORS[type],
      offset,
    })
  }
  return { links, skipped }
}

/** 与某站点相连的已绘制骨干链路。 */
export function linksOfSite(site: GeoSite | null | undefined, links: GeoLink[]): GeoLink[] {
  if (!site) return []
  return links.filter((l) => l.a.index === site.index || l.b.index === site.index)
}

/* ---------------------------------------------------------------------------
 * PDF 链路图层（第 1 页数字化）
 *
 * 边来自 olib-fiber-links.json，a/b 是 SITES 下标。两道防线：
 *   1. 两端站点都必须已定位，否则画不出来（与骨干链路同口径）；
 *   2. 文件里带 sites 映射（下标 → 站名），与当前站点表对不上就整条作废 ——
 *      站点表增删后 PDF 链路会错位，宁可少画，不能把链路接到别的站上。
 * ------------------------------------------------------------------------- */
export function buildFiberLinks(
  sites: GeoSite[],
  file: FiberFile | null,
): { links: FiberLink[]; skipped: number; stale: boolean } {
  if (!file || !Array.isArray(file.edges)) return { links: [], skipped: 0, stale: false }
  const byIndex = new Map<number, GeoSite>()
  for (const s of sites) byIndex.set(s.index, s)

  const links: FiberLink[] = []
  let skipped = 0
  let stale = false
  for (const e of file.edges) {
    const a = byIndex.get(e.a)
    const b = byIndex.get(e.b)
    const nameA = file.sites?.[String(e.a)]
    const nameB = file.sites?.[String(e.b)]
    if (!a || !b || nameA !== a.name || nameB !== b.name) {
      stale = true
      skipped++
      continue
    }
    if (!a.located || !b.located || a.index === b.index) {
      skipped++
      continue
    }
    links.push({
      id: `${e.a}-${e.b}-${e.c}`,
      a,
      b,
      group: e.c,
      type: e.t,
      dash: e.d === 1,
    })
  }
  return { links, skipped, stale }
}

/** 链路另一端。 */
export function otherEnd(link: GeoLink, site: GeoSite): GeoSite {
  return link.a.index === site.index ? link.b : link.a
}

/** 搜索 + 区域 + 类型过滤（复用 v1 的模糊匹配口径）。 */
export function filterGeoSites(
  sites: GeoSite[],
  query: string,
  sector: string,
  filter: SiteFilter,
): GeoSite[] {
  const q = query.trim()
  const ranked: { site: GeoSite; score: number }[] = []
  for (const site of sites) {
    if (sector && site.sector !== sector) continue
    if (filter === '5g' && !site.is5g) continue
    if (filter === 'isp' && !site.isp) continue
    if (filter === 'core' && !site.core) continue
    const score = siteFuzzyScore(site, q)
    if (score <= 0) continue
    ranked.push({ site, score })
  }
  if (q) {
    ranked.sort((a, b) => b.score - a.score || Number(b.site.located) - Number(a.site.located) || a.site.index - b.site.index)
  } else {
    ranked.sort((a, b) => Number(b.site.located) - Number(a.site.located) || a.site.index - b.site.index)
  }
  return ranked.map((r) => r.site)
}
