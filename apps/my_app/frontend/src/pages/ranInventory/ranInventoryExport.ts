/* RAN 资产台账 —— 三套 Excel 导出（全部走 utils/excelReport 的 UNIFIED_EXCEL_V1）。
 *
 * 拆出本文件：页面已有 KPI / 5 张图 / 两个视图的列 / 下钻，再放三套导出会超过
 * 单文件 500 行的工程约定。三个函数都是纯函数，入参由页面给，不自己取数。
 *
 *   · exportLedger —— 站点汇总台账（含三列型号构成文本）
 *   · exportPivot  —— 型号透视（两层表头：板卡/射频/天馈 分组，组内 合计 + 各型号）
 *   · exportDetail —— 单网元的 硬件 / 光模块 / 软件包 三张表
 */
import { exportReport, type HeaderCell } from '../../utils/excelReport'
import { MODEL_CAT_LABEL, catCount, catTotal, modelsFullText, type CatColumns, type ModelMaps } from './ranInventoryModels'
import type { Detail, ModelDef, NeRow } from './ranInventoryTypes'
import { otherCount } from './ranInventoryColumns'

type T = (key: string, params?: Record<string, string | number>) => string

const day = (iso: string) => (iso || '').slice(0, 10)

/** 站点汇总台账：一行一个网元，型号列写成「VBP 4 · VFC 3」这样的文本便于在 Excel 里读 */
export async function exportLedger(p: {
  t: T
  rows: NeRow[]
  modelIndex: Map<string, ModelMaps>
  defs: Record<string, ModelDef[]>
  generatedAt: string
}): Promise<void> {
  const { t, rows, modelIndex, defs, generatedAt } = p
  if (!rows.length) return
  const header = [
    t('ranInv.col.site'), t('ranInv.col.siteName'), t('ranInv.col.neId'), t('ranInv.col.moc'),
    t('ranInv.col.subType'), t('ranInv.col.ip'), t('ranInv.col.hw'), t('ranInv.col.pack'),
    t('ranInv.col.rru'), t('ranInv.col.ant'), t('ranInv.col.subrack'), t('ranInv.col.shelf'),
    t('ranInv.col.rack'), t('ranInv.col.accessory'), t('ranInv.col.modelPack'),
    t('ranInv.col.modelRru'), t('ranInv.col.modelAnt'), t('ranInv.col.opt'),
    t('ranInv.col.software'), t('ranInv.col.patch'), t('ranInv.col.neVer'),
    t('ranInv.col.launch'), t('ranInv.col.oldest'), t('ranInv.col.ageY'),
  ]
  const body = rows.map(r => {
    const m = modelIndex.get(r.id)
    return [
      r.site, r.display, r.nbi, r.moc, r.sub, r.ip,
      r.hw, r.u.pack || 0, r.u.RRU || 0, r.u.antenna || 0,
      r.u.subrack || 0, r.u.shelf || 0, r.u.rack || 0, r.u.accessory || 0,
      modelsFullText(m, defs, 'pack'), modelsFullText(m, defs, 'RRU'), modelsFullText(m, defs, 'antenna'),
      r.opt, r.sw, r.patch, r.ver, r.launch, r.oldest, r.age ?? '',
    ]
  })
  // 数量列可合计；型号列是文本；「机龄」是逐网元指标，求和没有意义，留空
  const sumCols = [6, 7, 8, 9, 10, 11, 12, 13, 17, 18, 19]
  const numeric = [...sumCols, 23]
  const total: (string | number)[] = new Array(header.length).fill('')
  total[0] = t('ranInv.export.total')
  for (const i of sumCols) {
    total[i] = body.reduce((s, row) => s + (typeof row[i] === 'number' ? (row[i] as number) : 0), 0)
  }
  await exportReport([{
    name: 'RAN_Asset_Ledger',
    title: t('ranInv.export.title'),
    header,
    rows: body,
    totalRow: total,
    numericCols: numeric,
    tabColor: 'FF843C0C',
  }], `RAN_Asset_Ledger_${day(generatedAt)}.xlsx`)
}

/** 型号透视：两层表头与屏幕上的分组列一一对应 */
export async function exportPivot(p: {
  t: T
  rows: NeRow[]
  cats: CatColumns[]
  modelIndex: Map<string, ModelMaps>
  generatedAt: string
  topN: number
}): Promise<void> {
  const { t, rows, cats, modelIndex, generatedAt, topN } = p
  if (!rows.length) return

  const head1: HeaderCell[] = [
    { text: t('ranInv.col.site') }, { text: t('ranInv.col.moc') },
    { text: t('ranInv.col.neId') }, { text: t('ranInv.col.ip') },
  ]
  const head2: HeaderCell[] = ['', '', '', ''].map(text => ({ text }))
  const numericCols: number[] = []

  for (const c of cats) {
    const span = 1 + c.cols.length + (c.other > 0 ? 1 : 0)
    head1.push({ text: `${t(MODEL_CAT_LABEL[c.cat])} (${c.total})`, colSpan: span })
    head2.push({ text: t('ranInv.model.total') })
    numericCols.push(head2.length - 1)
    for (const col of c.cols) {
      head2.push({ text: col.name })
      numericCols.push(head2.length - 1)
    }
    if (c.other > 0) {
      head2.push({ text: t('ranInv.other') })
      numericCols.push(head2.length - 1)
    }
  }
  head1.push({ text: t('ranInv.col.hw') })
  head2.push({ text: '' })

  const body = rows.map(r => {
    const m = modelIndex.get(r.id)
    const cells: (string | number)[] = [r.site, r.moc, r.nbi, r.ip]
    for (const c of cats) {
      cells.push(catTotal(m, c.cat))
      for (const col of c.cols) cells.push(catCount(m, c.cat, col.idx))
      if (c.other > 0) cells.push(otherCount(m, c))
    }
    cells.push(r.hw)
    return cells
  })

  const total: (string | number)[] = new Array(head2.length).fill('')
  total[0] = t('ranInv.export.total')
  for (const i of numericCols) {
    total[i] = body.reduce((s, row) => s + (typeof row[i] === 'number' ? (row[i] as number) : 0), 0)
  }
  // 最右的「硬件合计」不在分组里，单独补
  const last = head2.length - 1
  total[last] = body.reduce((s, row) => {
    const v = row[row.length - 1]
    return s + (typeof v === 'number' ? v : 0)
  }, 0)

  await exportReport([{
    name: 'RAN_Model_Pivot',
    title: t('ranInv.export.modelsTitle'),
    subtitle: t('ranInv.model.note', { n: topN }),
    header: [head1, head2],
    rows: body,
    totalRow: total,
    numericCols,
    tabColor: 'FF843C0C',
  }], `RAN_Model_Pivot_${day(generatedAt)}.xlsx`)
}

/** 单网元明细：硬件 / 光模块 / 软件包 三个 sheet */
export async function exportDetail(p: {
  t: T
  ne: NeRow
  detail: Detail
  generatedAt: string
}): Promise<void> {
  const { t, ne, detail, generatedAt } = p
  // displayName 形如 `Beever(4480)`，自带 NE ID，比站点名更适合做标题
  const who = ne.display || ne.site
  const date = t('ranInv.dhw.date')
  await exportReport([
    {
      name: 'Hardware',
      title: `${who} — ${t('ranInv.detail.hw')}`,
      header: [t('ranInv.dhw.type'), t('ranInv.dhw.model'), t('ranInv.dhw.sn'), t('ranInv.dhw.pos'), date],
      rows: detail.hw.map(u => [u.t, u.mt || u.m, u.sn, u.p, u.d]),
      tabColor: 'FF2F5496',
    },
    {
      name: 'Optical',
      title: `${who} — ${t('ranInv.detail.opt')}`,
      header: [t('ranInv.dopt.vendor'), t('ranInv.dopt.pn'), t('ranInv.dopt.sn'), t('ranInv.dopt.pos'), date],
      rows: detail.opt.map(o => [o.v, o.pn, o.sn, o.p, o.d]),
      tabColor: 'FF548235',
    },
    {
      name: 'Software',
      title: `${who} — ${t('ranInv.detail.sw')}`,
      header: [t('ranInv.dsw.type'), t('ranInv.dsw.ver'), t('ranInv.dsw.main'), t('ranInv.dsw.time')],
      rows: detail.sw.map(p => [p.t, p.v, p.m, p.a]),
      tabColor: 'FFBF8F00',
    },
  ], `RAN_Asset_${ne.nbi || ne.site}_${day(generatedAt)}.xlsx`)
}
