/* RAN 资产台账 —— 表格列定义（汇总视图 / 型号透视视图）。
 *
 * 两种视图共用「站点 / 类型 / 管理 IP」三列：
 *   · 汇总：硬件数量 + 板卡/射频/天馈 + **三列型号构成**（紧凑「型号 数量 · …」，悬停看全部）
 *   · 型号透视：每个型号一列，列头按 板卡 / 射频 / 天馈 分组，组头带该组总量
 * 型号列只保留**当前筛选结果**里数量最多的 topN 个，其余并成「其他」，不会留一排 0。
 */
import { Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import { WH } from '../../theme'
import { ExplainIcon, ExplainPopover } from '../../components/MetricExplain'
import { fmt, int, MOC_COLOR, type ModelDef, type NeRow } from './ranInventoryTypes'
import {
  MODEL_CAT_LABEL, catCount, catTotal, modelsFullText, modelsText,
  type CatColumns, type ModelCat, type ModelMaps,
} from './ranInventoryModels'

const { Text } = Typography

export type T = (key: string, params?: Record<string, string | number>) => string

const mono = { fontSize: 12, fontFamily: 'monospace' } as const

/** 站点 / 类型 / 管理 IP —— 两种视图共用，站点列固定在左侧 */
function identityColumns(t: T): ColumnsType<NeRow> {
  return [
    {
      title: t('ranInv.col.site'),
      dataIndex: 'site',
      width: 190,
      fixed: 'left',
      render: (_: unknown, r) => (
        <div style={{ display: 'grid', gap: 1 }}>
          <Text strong style={{ fontSize: 13 }}>{fmt(r.site)}</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>#{fmt(r.nbi)}</Text>
        </div>
      ),
    },
    {
      title: t('ranInv.col.moc'),
      dataIndex: 'moc',
      width: 92,
      render: (v: string) => <Tag color={MOC_COLOR[v] || 'default'}>{fmt(v)}</Tag>,
    },
    {
      title: t('ranInv.col.ip'),
      dataIndex: 'ip',
      width: 128,
      render: (v: string) => <Text style={mono}>{fmt(v)}</Text>,
    },
  ]
}

/** 型号构成单元格：紧凑三行内联，悬停看该网元全部型号 */
function modelsCell(
  t: T, maps: Map<string, ModelMaps>, defs: Record<string, ModelDef[]>,
  ne: NeRow, cat: ModelCat,
) {
  const m = maps.get(ne.id)
  // 列宽有限：只列前 2 个型号（列 2 个正好放得下不被截断），其余用 +N，
  // 悬停气泡与「型号透视」里都是全部型号，信息不会丢。
  const text = modelsText(m, defs, cat, 2)
  if (!text) return <Text type="secondary">—</Text>
  const more = Math.max(0, (m?.[cat]?.size ?? 0) - 2)
  return (
    <ExplainPopover text={t('ranInv.tip.models', { list: modelsFullText(m, defs, cat) })}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, minWidth: 0, maxWidth: '100%' }}>
        {/* 单行截断：型号多的时候换行会把行高撑成两行，整张表看起来参差 */}
        <Text style={{ fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {text}
        </Text>
        {more > 0 && <Tag style={{ marginInlineEnd: 0, flex: 'none' }}>+{more}</Tag>}
      </span>
    </ExplainPopover>
  )
}

/** 汇总视图：在原有数量列之后追加三列型号构成 */
export function buildSummaryColumns(p: {
  t: T
  maps: Map<string, ModelMaps>
  defs: Record<string, ModelDef[]>
}): ColumnsType<NeRow> {
  const { t, maps, defs } = p
  return [
    ...identityColumns(t),
    {
      title: <>{t('ranInv.col.hw')}<ExplainIcon text={t('ranInv.explain.hw')} /></>,
      dataIndex: 'hw',
      width: 160,
      align: 'right',
      sorter: (a, b) => a.hw - b.hw,
      render: (v: number, r) => {
        const tot = v || 1
        const segs = [
          { n: r.u.pack || 0, c: WH.teal },
          { n: r.u.RRU || 0, c: WH.blue },
          { n: r.u.antenna || 0, c: WH.indigo },
          { n: Math.max(0, v - (r.u.pack || 0) - (r.u.RRU || 0) - (r.u.antenna || 0)), c: WH.textDim },
        ]
        return (
          <ExplainPopover text={t('ranInv.tip.hw', {
            total: v, pack: r.u.pack || 0, rru: r.u.RRU || 0, ant: r.u.antenna || 0,
          })}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              <Text strong>{int(v)}</Text>
              <span style={{
                display: 'inline-flex', width: 64, height: 6, borderRadius: 999,
                overflow: 'hidden', background: WH.cardBorder,
              }}>
                {segs.map((s, i) => s.n > 0
                  ? <i key={i} style={{
                      display: 'block', height: '100%',
                      width: `${(s.n / tot) * 100}%`, background: s.c,
                    }} />
                  : null)}
              </span>
            </span>
          </ExplainPopover>
        )
      },
    },
    { title: t('ranInv.col.pack'), dataIndex: ['u', 'pack'], width: 70, align: 'right', sorter: (a, b) => (a.u.pack || 0) - (b.u.pack || 0), render: (v: number) => int(v || 0) },
    { title: t('ranInv.col.rru'), dataIndex: ['u', 'RRU'], width: 70, align: 'right', sorter: (a, b) => (a.u.RRU || 0) - (b.u.RRU || 0), render: (v: number) => int(v || 0) },
    { title: t('ranInv.col.ant'), dataIndex: ['u', 'antenna'], width: 70, align: 'right', sorter: (a, b) => (a.u.antenna || 0) - (b.u.antenna || 0), render: (v: number) => int(v || 0) },
    {
      title: <>{t('ranInv.col.modelPack')}<ExplainIcon text={t('ranInv.explain.models')} /></>,
      key: 'modelPack', width: 200,
      render: (_: unknown, r) => modelsCell(t, maps, defs, r, 'pack'),
    },
    {
      title: t('ranInv.col.modelRru'), key: 'modelRru', width: 200,
      render: (_: unknown, r) => modelsCell(t, maps, defs, r, 'RRU'),
    },
    {
      title: t('ranInv.col.modelAnt'), key: 'modelAnt', width: 200,
      render: (_: unknown, r) => modelsCell(t, maps, defs, r, 'antenna'),
    },
    { title: t('ranInv.col.opt'), dataIndex: 'opt', width: 88, align: 'right', sorter: (a, b) => a.opt - b.opt, render: (v: number) => int(v) },
    {
      title: t('ranInv.col.ver'),
      dataIndex: 'ver',
      width: 140,
      render: (v: string, r) => (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          <Text style={mono}>{fmt((v || '').split('#')[0])}</Text>
          {r.patch > 0 && <Tag color="gold" style={{ marginInlineEnd: 0 }}>{t('ranInv.col.patch')} {r.patch}</Tag>}
        </span>
      ),
    },
    {
      title: t('ranInv.col.launch'),
      dataIndex: 'launch',
      width: 100,
      render: (v: string) => <Text style={mono}>{fmt(v)}</Text>,
    },
    {
      title: <>{t('ranInv.col.age')}<ExplainIcon text={t('ranInv.explain.age')} /></>,
      dataIndex: 'age',
      width: 84,
      align: 'right',
      sorter: (a, b) => (a.age || 0) - (b.age || 0),
      render: (v: number | null) => (
        <ExplainPopover text={t('ranInv.tip.age', { v: v == null ? '—' : v })}>
          <Text style={{ color: v != null && v >= 8 ? WH.warn : undefined }}>{v == null ? '—' : v.toFixed(1)}</Text>
        </ExplainPopover>
      ),
    },
  ]
}

/** 型号透视：每个型号一列，列头分组，组头带该组在筛选结果里的总量 */
export function buildPivotColumns(p: {
  t: T
  maps: Map<string, ModelMaps>
  cats: CatColumns[]
}): ColumnsType<NeRow> {
  const { t, maps, cats } = p
  const modelCol = (cat: ModelCat, idx: number, name: string, n: number): any => ({
    title: (
      <Tooltip title={`${name} · ${int(n)}`}>
        <span style={{ ...mono, fontSize: 11 }}>{name}</span>
      </Tooltip>
    ),
    key: `${cat}-${idx}`,
    width: 96,
    align: 'right',
    sorter: (a: NeRow, b: NeRow) => catCount(maps.get(a.id), cat, idx) - catCount(maps.get(b.id), cat, idx),
    render: (_: unknown, r: NeRow) => {
      const v = catCount(maps.get(r.id), cat, idx)
      return v ? <Text>{int(v)}</Text> : <Text type="secondary">·</Text>
    },
  })

  const groups = cats.map(c => ({
    title: (
      <span>
        {t(MODEL_CAT_LABEL[c.cat])}
        <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>{int(c.total)}</Text>
      </span>
    ),
    key: `grp-${c.cat}`,
    children: [
      {
        title: t('ranInv.model.total'),
        key: `${c.cat}-total`,
        width: 80,
        align: 'right' as const,
        sorter: (a: NeRow, b: NeRow) =>
          catTotal(maps.get(a.id), c.cat) - catTotal(maps.get(b.id), c.cat),
        render: (_: unknown, r: NeRow) => <Text strong>{int(catTotal(maps.get(r.id), c.cat))}</Text>,
      },
      ...c.cols.map(col => modelCol(c.cat, col.idx, col.name, col.n)),
      ...(c.other > 0 ? [{
        title: <Tooltip title={t('ranInv.model.otherTip')}><span style={{ fontSize: 11 }}>{t('ranInv.other')}</span></Tooltip>,
        key: `${c.cat}-other`,
        width: 84,
        align: 'right',
        sorter: (a: NeRow, b: NeRow) =>
          otherCount(maps.get(a.id), c) - otherCount(maps.get(b.id), c),
        render: (_: unknown, r: NeRow) => {
          const v = otherCount(maps.get(r.id), c)
          return v ? <Text>{int(v)}</Text> : <Text type="secondary">·</Text>
        },
      }] : []),
    ],
  }))

  return [
    ...identityColumns(t),
    ...groups,
    {
      title: t('ranInv.col.hw'),
      dataIndex: 'hw',
      width: 90,
      align: 'right',
      fixed: 'right',
      sorter: (a, b) => a.hw - b.hw,
      render: (v: number) => <Text strong>{int(v)}</Text>,
    },
  ]
}

/** 某网元在「其他」里的数量 = 该类合计 − 已列出的型号合计 */
export function otherCount(maps: ModelMaps | undefined, c: CatColumns): number {
  if (!maps) return 0
  let listed = 0
  for (const col of c.cols) listed += catCount(maps, c.cat, col.idx)
  return Math.max(0, catTotal(maps, c.cat) - listed)
}
