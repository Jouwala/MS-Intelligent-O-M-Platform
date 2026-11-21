/* ============================================================================
 * OLIB FO 网络拓扑 —— 站点模糊匹配
 * ============================================================================
 * 667 条静态站名，在页面里过滤，不走接口。
 * 「模糊」在这里是四档，从紧到松，命中即停在更高分：
 *   1. 规范化后的整名 / 词包含（忽略大小写、空格和标点）
 *   2. 查询被空格拆开后，每个词都出现在站名或区域里
 *   3. 站名的某一个词与查询编辑距离 ≤ 1（允许打错一个字母，查询至少 5 字，
 *      避免 10th 被算成 16th）
 *   4. 站名的某一个词按顺序包含查询的每个字符，且多出来的字符不超过 4 个
 * 1～3 个字符只走包含，避免三个字母就扫中大半张图。
 * ========================================================================== */
import type { SiteFilter, TopoSite } from './topologyData'

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, '')

function tokensOf(s: string): string[] {
  return s.toLowerCase().split(/[^a-z0-9\u4e00-\u9fff]+/).map(norm).filter(Boolean)
}

/** 编辑距离是否不超过 max。长度差已经超过 max 时直接否决。 */
function withinEdit(a: string, b: string, max: number): boolean {
  if (Math.abs(a.length - b.length) > max) return false
  const n = b.length
  let prev = Array.from({ length: n + 1 }, (_, j) => j)
  let curr = new Array<number>(n + 1)
  for (let i = 1; i <= a.length; i++) {
    curr[0] = i
    let rowMin = curr[0]
    for (let j = 1; j <= n; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      curr[j] = Math.min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
      if (curr[j] < rowMin) rowMin = curr[j]
    }
    if (rowMin > max) return false
    const swap = prev
    prev = curr
    curr = swap
  }
  return prev[n] <= max
}

function isTightSubsequence(hay: string, needle: string): boolean {
  if (hay.length - needle.length > 4) return false
  let i = 0
  for (const ch of hay) {
    if (ch === needle[i]) i++
    if (i === needle.length) return true
  }
  return false
}

function scoreHay(hay: string, q: string): number {
  if (!hay || !q) return 0
  if (hay === q) return 1000
  if (hay.startsWith(q)) return 860
  if (hay.includes(q)) return 720
  if (q.length >= 5 && withinEdit(hay, q, 1)) return 300
  if (q.length >= 4 && isTightSubsequence(hay, q)) return 420
  return 0
}

/** 0 = 不匹配。分数越高越靠前。空查询返回 1，调用方据此保留原顺序。 */
export function siteFuzzyScore(site: TopoSite, query: string): number {
  const raw = query.trim().toLowerCase()
  if (!raw) return 1
  const q = norm(raw)
  if (!q) return 0
  const name = norm(site.name)
  const sector = norm(site.sector)
  let best = Math.max(scoreHay(name, q), scoreHay(sector, q) > 0 ? Math.min(scoreHay(sector, q), 640) : 0)
  for (const token of tokensOf(site.name)) {
    const s = scoreHay(token, q)
    if (s > best) best = s
  }
  const parts = raw.split(/[^a-z0-9\u4e00-\u9fff]+/).map(norm).filter(Boolean)
  if (parts.length > 1 && parts.every(p => name.includes(p) || sector.includes(p))) {
    best = Math.max(best, 560)
  }
  return best
}

export function matchSites(sites: TopoSite[], query: string, sector: string, filter: SiteFilter): TopoSite[] {
  const q = query.trim()
  const ranked: { site: TopoSite; score: number; index: number }[] = []
  sites.forEach((site, index) => {
    if (sector && site.sector !== sector) return
    if (filter === '5g' && !site.is5g) return
    if (filter === 'isp' && !site.isp) return
    if (filter === 'core' && !site.core) return
    const score = siteFuzzyScore(site, q)
    if (score <= 0) return
    ranked.push({ site, score, index })
  })
  if (!q) return ranked.map(r => r.site)
  ranked.sort((a, b) => b.score - a.score || a.index - b.index)
  return ranked.map(r => r.site)
}
