/* RAN 资产台账 —— 型号统计（pack / RRU / antenna 按型号计数）。
 *
 * 后端只发「型号字典 + 逐网元 [下标, 数量] 对」，不重复下发型号名；
 * 这里负责解码成 Map，并按**当前筛选结果**挑出要展示的型号列 ——
 * 所以筛到 SDR 时不会留下一排全 0 的 ITBBU 板卡列。
 */
import type { ModelDef, NeRow } from './ranInventoryTypes'

export type ModelCat = 'pack' | 'RRU' | 'antenna'
export const MODEL_CATS: ModelCat[] = ['pack', 'RRU', 'antenna']

/** 型号列的取值字段说明：板卡/射频用 packfunction，天馈用 vendorunittypename */
export const MODEL_CAT_LABEL: Record<ModelCat, string> = {
  pack: 'ranInv.unit.pack',
  RRU: 'ranInv.unit.RRU',
  antenna: 'ranInv.unit.antenna',
}

export type ModelMaps = Record<ModelCat, Map<number, number>>
export type ModelCol = { idx: number; name: string; n: number }

/** 把每个网元的 [下标, 数量] 解码成 Map，整页只算一次。 */
export function buildModelIndex(rows: NeRow[]): Map<string, ModelMaps> {
  const out = new Map<string, ModelMaps>()
  for (const r of rows) {
    const maps = {} as ModelMaps
    for (const cat of MODEL_CATS) {
      const m = new Map<number, number>()
      for (const pair of (r.mc && r.mc[cat]) || []) m.set(pair[0], pair[1])
      maps[cat] = m
    }
    out.set(r.id, maps)
  }
  return out
}

/** 该网元某一类硬件的型号合计（= 该类的硬件单元数）。 */
export function catTotal(maps: ModelMaps | undefined, cat: ModelCat): number {
  if (!maps) return 0
  let t = 0
  for (const n of maps[cat].values()) t += n
  return t
}

/** 该网元某个型号的数量。 */
export function catCount(maps: ModelMaps | undefined, cat: ModelCat, idx: number): number {
  return maps ? (maps[cat].get(idx) || 0) : 0
}

export type CatColumns = {
  cat: ModelCat
  /** 参与展示的型号列（按当前筛选结果里的数量降序） */
  cols: ModelCol[]
  /** 其余型号的数量合计；0 表示没有剩余 */
  other: number
  /** 这一类在筛选结果里的总量 */
  total: number
}

/** 依据筛选后的行，给每一类挑出数量最多的 topN 个型号，其余并成「其他」。 */
export function buildCatColumns(
  rows: NeRow[],
  index: Map<string, ModelMaps>,
  defs: Record<string, ModelDef[]>,
  topN: number,
): CatColumns[] {
  return MODEL_CATS.map(cat => {
    const tally = new Map<number, number>()
    let total = 0
    for (const r of rows) {
      const m = index.get(r.id)
      if (!m) continue
      for (const [idx, n] of m[cat]) {
        tally.set(idx, (tally.get(idx) || 0) + n)
        total += n
      }
    }
    const sorted = [...tally.entries()].sort((a, b) => b[1] - a[1] || a[0] - b[0])
    const picked = sorted.slice(0, Math.max(1, topN))
    const cols = picked.map(([idx, n]) => ({
      idx,
      name: defs[cat]?.[idx]?.name || `#${idx}`,
      n,
    }))
    const other = sorted.slice(Math.max(1, topN)).reduce((s, [, n]) => s + n, 0)
    return { cat, cols, other, total }
  })
}

/** 「VBP 4 · VFC 3 · +5」这类紧凑文案，给汇总视图的型号列与 Excel 共用。 */
export function modelsText(
  maps: ModelMaps | undefined,
  defs: Record<string, ModelDef[]>,
  cat: ModelCat,
  limit = 3,
): string {
  if (!maps) return ''
  const list = [...maps[cat].entries()]
    .sort((a, b) => b[1] - a[1] || a[0] - b[0])
    .map(([idx, n]) => ({ name: defs[cat]?.[idx]?.name || `#${idx}`, n }))
  if (!list.length) return ''
  return list.slice(0, limit).map(x => `${x.name} ${x.n}`).join(' · ')
}

/** 全量型号文案（悬停气泡 / 导出用），不截断。 */
export function modelsFullText(
  maps: ModelMaps | undefined,
  defs: Record<string, ModelDef[]>,
  cat: ModelCat,
): string {
  if (!maps) return ''
  return [...maps[cat].entries()]
    .sort((a, b) => b[1] - a[1] || a[0] - b[0])
    .map(([idx, n]) => `${defs[cat]?.[idx]?.name || `#${idx}`} ${n}`)
    .join(' · ')
}
