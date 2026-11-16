#!/usr/bin/env node
/* ============================================================================
 * OLIB 站点 GPS 映射表生成器（?page=olibTopo_v2 的数据源）
 * ============================================================================
 * 输入
 *   1. src/pages/networkTopology/topologyData.ts 里的 553 条 RAW_SITES
 *      （站点名 + PDF 相对坐标 + 5G/ISP 标记 + 区域；**不是 GPS**）
 *   2. 后端 GET /api/gis/sites（master_site_db，真实经纬度）
 *   3. scripts/olib-site-aliases.json（人工核验的拼写别名）
 *
 * 输出
 *   public/olib/olib-site-geo.json —— 一条 OLIB 站点一行，带匹配方式与经纬度；
 *   未匹配 / 重名歧义的条目保留在数组里并写明原因，页面据此显示「未定位」。
 *
 * 匹配规则（保守优先，宁缺勿错）
 *   · 站名规范化（小写、去非字母数字）后**唯一**命中 → name-exact
 *   · 同一规范化站名在 GIS 里有多条但坐标一致（2G/3G 与 4G 两行同一站）→ 取一条
 *   · 同一规范化站名坐标不一致 → ambiguous，不上图
 *   · 别名表命中且目标唯一 → alias（别名表逐条人工确认）
 *   · 其余 → unmatched（**不做模糊自动匹配**，避免 6th Street → 16th Street 这类错配）
 *
 * 用法
 *   node scripts/gen-olib-site-geo.mjs                 # 重新生成
 *   node scripts/gen-olib-site-geo.mjs --check         # 只比对，漂移则退出码 1
 *   GIS_API_URL=http://127.0.0.1:7038/api/gis/sites node scripts/gen-olib-site-geo.mjs
 *
 * ⚠ 这份 JSON 是**站点坐标映射快照**，不是实时接口：master_site_db 新增/改名后
 *   需要重跑本脚本并提交差异。页面不直连 GIS 接口，避免每次进页面都做一遍匹配。
 * ========================================================================== */
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(HERE, '..')
const TOPO_DATA = resolve(FRONTEND, 'src/pages/networkTopology/topologyData.ts')
const ALIASES = resolve(HERE, 'olib-site-aliases.json')
const OUT = resolve(FRONTEND, 'public/olib/olib-site-geo.json')
const API = process.env.GIS_API_URL || 'http://127.0.0.1:7038/api/gis/sites'
const CHECK = process.argv.includes('--check')

/** 站名规范化：与页面 networkTopology/siteMatch.ts 的口径一致（只保留字母数字）。 */
const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '')

/** 从 topologyData.ts 抽出 RAW_SITES 的 [name, x, y, is5g, isp, sector] 元组。 */
function readTopoSites() {
  const src = readFileSync(TOPO_DATA, 'utf8')
  const m = src.match(/const RAW_SITES[^\[]*\[([\s\S]*?)\n\]\s*\n/)
  if (!m) throw new Error(`RAW_SITES 解析失败：${TOPO_DATA}`)
  const tuple = /\["((?:[^"\\]|\\.)*)",\s*([\d.]+),\s*([\d.]+),\s*(true|false),\s*(true|false),\s*"((?:[^"\\]|\\.)*)"\]/g
  const rows = []
  let hit
  while ((hit = tuple.exec(m[1]))) {
    rows.push({
      name: hit[1].replace(/\\"/g, '"'),
      sector: hit[5].replace(/\\"/g, '"'),
    })
  }
  if (!rows.length) throw new Error('RAW_SITES 解析出 0 条站点')
  return rows
}

async function readGisSites() {
  const res = await fetch(API, { headers: { Accept: 'application/json' } })
  if (!res.ok) throw new Error(`GET ${API} → HTTP ${res.status}`)
  const payload = await res.json()
  const sites = Array.isArray(payload?.sites) ? payload.sites : []
  if (!sites.length) throw new Error('GIS 返回的 sites 为空，拒绝生成空映射表')
  return sites
}

function groupByName(sites) {
  const by = new Map()
  for (const s of sites) {
    const name = String(s.site_name ?? '').trim()
    if (!name) continue
    if (!Number.isFinite(Number(s.lat)) || !Number.isFinite(Number(s.lng))) continue
    const key = norm(name)
    if (!key) continue
    if (!by.has(key)) by.set(key, [])
    by.get(key).push(s)
  }
  return by
}

/** 同名多条：坐标（保留 4 位小数）完全一致则视为同一物理站，取 site_id 最小的一条。 */
function resolveUnique(list) {
  if (!list?.length) return { state: 'none' }
  const coords = new Set(list.map((s) => `${Number(s.lat).toFixed(4)},${Number(s.lng).toFixed(4)}`))
  if (coords.size > 1) return { state: 'ambiguous', candidates: list }
  return { state: 'ok', site: [...list].sort((a, b) => Number(a.site_id) - Number(b.site_id))[0] }
}

function readAliases() {
  const raw = JSON.parse(readFileSync(ALIASES, 'utf8'))
  const list = Array.isArray(raw?.aliases) ? raw.aliases : []
  return list.filter((a) => a && a.olib && a.gis)
}

function record(i, name, method, extra = {}) {
  return { i, name, method, verified: false, ...extra }
}

async function build() {
  const rows = readTopoSites()
  const gisSites = await readGisSites()
  const byName = groupByName(gisSites)
  const aliasMap = new Map()
  const aliasProblems = []

  for (const a of readAliases()) {
    const resolved = resolveUnique(byName.get(norm(a.gis)))
    if (resolved.state !== 'ok') {
      aliasProblems.push(`${a.olib} → ${a.gis}：目标${resolved.state === 'none' ? '不存在' : '坐标冲突'}`)
      continue
    }
    aliasMap.set(norm(a.olib), { ...resolved.site, note: a.note || '' })
  }
  if (aliasProblems.length) {
    throw new Error(`别名表有 ${aliasProblems.length} 条无效，请先修正：\n  ${aliasProblems.join('\n  ')}`)
  }

  const records = []
  const counts = { 'name-exact': 0, alias: 0, unmatched: 0, ambiguous: 0 }

  rows.forEach((row, i) => {
    const key = norm(row.name)
    const direct = resolveUnique(byName.get(key))
    if (direct.state === 'ok') {
      counts['name-exact']++
      records.push(record(i, row.name, 'name-exact', {
        siteId: String(direct.site.site_id ?? ''),
        gisName: direct.site.site_name,
        lat: Number(direct.site.lat),
        lng: Number(direct.site.lng),
        county: direct.site.province_county ?? '',
        region: direct.site.region ?? '',
        verified: true,
      }))
      return
    }
    if (direct.state === 'ambiguous') {
      counts.ambiguous++
      records.push(record(i, row.name, 'ambiguous', {
        reason: 'same-name-different-coords',
        candidates: direct.candidates.map((s) => `${s.site_name} (${s.site_id})`).slice(0, 4),
      }))
      return
    }
    const alias = aliasMap.get(key)
    if (alias) {
      counts.alias++
      records.push(record(i, row.name, 'alias', {
        siteId: String(alias.site_id ?? ''),
        gisName: alias.site_name,
        lat: Number(alias.lat),
        lng: Number(alias.lng),
        county: alias.province_county ?? '',
        region: alias.region ?? '',
        note: alias.note,
        verified: true,
      }))
      return
    }
    counts.unmatched++
    records.push(record(i, row.name, 'unmatched', { reason: 'no-name-match' }))
  })

  const total = records.length
  const located = counts['name-exact'] + counts.alias
  return {
    generatedAt: new Date().toISOString().slice(0, 10),
    source: {
      topology: 'Orange_network_topology_20251226.pdf → networkTopology/topologyData.ts（553 条站名/相对坐标）',
      gps: 'GET /api/gis/sites → master_site_db（真实经纬度快照）',
      aliases: 'scripts/olib-site-aliases.json（人工核验）',
    },
    summary: { total, located, ...counts, ambiguous: counts.ambiguous },
    records,
  }
}

function serialize(data) {
  /* 一条记录一行：既便于 diff，也让人能直接查「这个站映射到哪」。 */
  const head = JSON.stringify(
    { generatedAt: data.generatedAt, source: data.source, summary: data.summary },
    null, 2,
  ).replace(/\n}$/, '')
  const lines = data.records.map((r) => `    ${JSON.stringify(r)}`)
  return `${head},\n  "records": [\n${lines.join(',\n')}\n  ]\n}\n`
}

const data = await build()
const next = serialize(data)

if (CHECK) {
  const prev = existsSync(OUT) ? readFileSync(OUT, 'utf8') : ''
  const stripDate = (s) => s.replace(/"generatedAt": "[^"]*",?\s*/g, '')
  if (stripDate(prev) !== stripDate(next)) {
    console.error(`[olib-site-geo] 映射表与当前 GIS/拓扑数据不一致：${OUT}\n  请重新运行：node scripts/gen-olib-site-geo.mjs`)
    process.exit(1)
  }
  console.log('[olib-site-geo] 一致')
  process.exit(0)
}

mkdirSync(dirname(OUT), { recursive: true })
writeFileSync(OUT, next, 'utf8')
console.log(`[olib-site-geo] 已写入 ${OUT}`)
console.log(`  站点 ${data.summary.total} · 已定位 ${data.summary.located}`
  + `（精确 ${data.summary['name-exact']} + 别名 ${data.summary.alias}）`
  + ` · 未匹配 ${data.summary.unmatched} · 同名歧义 ${data.summary.ambiguous}`)
