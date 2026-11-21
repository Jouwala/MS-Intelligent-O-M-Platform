/* ============================================================================
 * 流量负载色阶（Weathermap 模式）
 * ============================================================================
 * 8 档色阶照搬 PHP Network Weathermap 的 **DEFAULT SCALE**（不是这里自定的配色）：
 * `0-1% #d9d9d9 / 1-10% #8c00ff / 10-25% #2020ff / 25-40% #00c0ff /
 *  40-55% #00f000 / 55-70% #f0f000 / 70-85% #ffc000 / 85-100% #ff0000`。
 * 用同一套颜色的目的是让看过 Weathermap 的人不必重新学一遍图例。
 *
 * 两条约定（与后端 `topo_traffic.py` 一致，改一边要改另一边）：
 *   1. **没有数据 ≠ 0%**：`null` / `undefined` / 非有限数一律返回「无数据」色，
 *      标签留空，绝不画成灰白的 0-1%。
 *   2. 区间是左闭右开（`1%` 归入 1-10%），与 Weathermap 的判定一致。
 * ========================================================================== */

export interface TrafficBin {
  /** 区间下沿（含），百分比 */
  min: number
  /** 区间上沿（不含），百分比；最后一档为 100 */
  max: number
  color: string
  /** 图例文案，如 `1-10%` */
  label: string
}

export const TRAFFIC_BINS: TrafficBin[] = [
  { min: 0, max: 1, color: '#d9d9d9', label: '0-1%' },
  { min: 1, max: 10, color: '#8c00ff', label: '1-10%' },
  { min: 10, max: 25, color: '#2020ff', label: '10-25%' },
  { min: 25, max: 40, color: '#00c0ff', label: '25-40%' },
  { min: 40, max: 55, color: '#00f000', label: '40-55%' },
  { min: 55, max: 70, color: '#f0f000', label: '55-70%' },
  { min: 70, max: 85, color: '#ffc000', label: '70-85%' },
  { min: 85, max: 100, color: '#ff0000', label: '85-100%' },
]

/** 未采集到流量（端口不在 KPI 覆盖里 / 速率解析不出）时链路的颜色：中性浅灰虚线 */
export const TRAFFIC_NA_COLOR = '#9aa0a6'

/** 利用率 → 颜色。缺数据返回 `TRAFFIC_NA_COLOR`，调用方据此画虚线并省略标签。 */
export function weathermapColor(util: number | null | undefined): string {
  if (util == null || !Number.isFinite(util)) return TRAFFIC_NA_COLOR
  if (util < 0) return TRAFFIC_BINS[0].color
  // 超过 100% 属于计数器/带宽口径不一致，按最高档上色但保留原始数字（不截断成 100%）
  for (const bin of TRAFFIC_BINS) {
    if (util < bin.max) return bin.color
  }
  return TRAFFIC_BINS[TRAFFIC_BINS.length - 1].color
}

/** 画布上的百分比标签；缺数据返回空串（调用方据此不画标签）。 */
export function trafficLabel(util: number | null | undefined): string {
  if (util == null || !Number.isFinite(util)) return ''
  return `${util.toFixed(1)}%`
}

export interface Pt { x: number; y: number }

/**
 * 把一条二次贝塞尔 `a — c — b` 在中点拆成两段（de Casteljau）。
 *
 * 流量模式要「源侧一段、宿侧一段」分别上色，但链路是弯的：直接取两端的直线中点
 * 会让分段点偏离曲线。这里按 t=0.5 精确拆分，两段各自仍是同一条曲线的一半，
 * 视觉上没有接缝。返回的两段控制点可直接拼成 `Q` 路径。
 */
export function splitQuadraticAtMid(a: Pt, c: Pt, b: Pt) {
  const m1: Pt = { x: (a.x + c.x) / 2, y: (a.y + c.y) / 2 }
  const m2: Pt = { x: (c.x + b.x) / 2, y: (c.y + b.y) / 2 }
  const mid: Pt = { x: (m1.x + m2.x) / 2, y: (m1.y + m2.y) / 2 }
  return { mid, first: [a, m1, mid] as const, second: [mid, m2, b] as const }
}

/** 二次贝塞尔上参数 t 处的点（标签定位用：前半段 t=0.25，后半段 t=0.75）。 */
export function quadraticAt(a: Pt, c: Pt, b: Pt, t: number): Pt {
  const u = 1 - t
  return {
    x: u * u * a.x + 2 * t * u * c.x + t * t * b.x,
    y: u * u * a.y + 2 * t * u * c.y + t * t * b.y,
  }
}
