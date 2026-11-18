/* RAN 资产台账：与后端接口对齐的类型与展示常量。
 * 页面（pages/RanInventory.tsx）与明细组件（RanInventoryDetail.tsx）共用这一份，
 * 避免两边各写一套字段名。 */
import { WH } from '../../theme'

export type UnitCounts = Record<string, number>

/** 型号字典项（全局，按出现数量降序） */
export type ModelDef = { name: string; n: number }
/** 逐网元的型号计数：[[型号下标, 数量], ...] */
export type ModelPairs = Record<string, [number, number][]>

export type NeRow = {
  id: string; site: string; display: string; nbi: string; moc: string; sub: string
  ip: string; proto: string; ver: string; maint: string
  hw: number; u: UnitCounts; mc: ModelPairs
  opt: number; sw: number; patch: number
  launch: string; oldest: string; age: number | null
  hw_bad: number; opt_bad: number
}

export type Counts = {
  ne_total: number; ne_online: number; ne_offline: number; ne_sites: number
  hw_total: number; hw_online: number; sw_total: number; sw_online: number
  opt_total: number; opt_online: number; hw_bad: number; opt_bad: number
  patch_total: number; patch_ne: number; avg_age: number | null; old_ne: number
}

export type UnitTotal = { key: string; label: string; n: number }

export type Ledger = {
  generated_at: string; snapshot: string; auth_source?: string; error?: string
  counts: Counts
  unit_totals: UnitTotal[]
  model_defs: Record<string, ModelDef[]>
  by_type: { moc: string; ne: number; hw: number; opt: number }[]
  years: { year: string; n: number }[]
  opt_vendors: { name: string; n: number }[]
  pack_models: { name: string; n: number }[]
  ne: NeRow[]
}

export type HwUnit = { t: string; m: string; mt: string; sn: string; p: string; d: string; st: string }
export type OptUnit = { v: string; pn: string; sn: string; p: string; d: string; st: string }
export type SwPkg = { t: string; v: string; m: string; st: string; a: string }
export type Detail = { ne_id: string; hw: HwUnit[]; opt: OptUnit[]; sw: SwPkg[] }

/** 统一空值展示：— 而不是 undefined / 空串 */
export const fmt = (v: unknown) => (v == null || v === '' ? '—' : String(v))
export const int = (v: number | null | undefined) =>
  (v == null ? '—' : v.toLocaleString('en-US'))

/** 单位 key → i18n key（显式映射：动态拼 key 会被 check:i18n 拦下） */
export const UNIT_LABEL: Record<string, string> = {
  pack: 'ranInv.unit.pack',
  RRU: 'ranInv.unit.RRU',
  antenna: 'ranInv.unit.antenna',
  subrack: 'ranInv.unit.subrack',
  shelf: 'ranInv.unit.shelf',
  rack: 'ranInv.unit.rack',
  accessory: 'ranInv.unit.accessory',
}

/** DOM 层的构成条配色（ECharts 用 DARK/LIGHT 实值，不走这里） */
export const UNIT_COLOR: Record<string, string> = {
  pack: WH.teal, RRU: WH.blue, antenna: WH.indigo,
  subrack: WH.textDim, shelf: WH.textDim, rack: WH.textDim, accessory: WH.textDim,
}

export const MOC_COLOR: Record<string, string> = {
  ITBBU: 'cyan', SDR: 'blue', MRNC: 'purple', MEC: 'orange',
}
