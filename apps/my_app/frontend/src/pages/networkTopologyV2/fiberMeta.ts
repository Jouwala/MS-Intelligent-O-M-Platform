/* ============================================================================
 * OLIB GIS 拓扑 —— PDF 链路图层（第 1 页数字化）的样式与图例
 * ============================================================================
 * 数据来自 public/olib/olib-fiber-links.json，由
 * backend/scripts/gen_olib_fiber_links.py 从 PDF 第 1 页抽取（站点对边）。
 *
 * 这一层是**背景网**：线比骨干细、透明度低，颜色只区分群组（40G / 10G /
 * IPRAN·DWDM / P26 项目 / 基础链路），具体类型放在 tooltip 里。
 * ========================================================================== */
import type { FiberGroup } from './types'

export interface FiberStyle {
  color: string
  weight: number
  opacity: number
  /** 规划中的链路用虚线 */
  dash?: string
}

export const FIBER_STYLES: Record<FiberGroup, FiberStyle> = {
  f40: { color: '#ea580c', weight: 1.8, opacity: 0.85 },
  f10: { color: '#ca8a04', weight: 1.3, opacity: 0.78 },
  ipran: { color: '#0891b2', weight: 1.3, opacity: 0.78 },
  p26: { color: '#a21caf', weight: 1.1, opacity: 0.72 },
  plan: { color: '#f97316', weight: 1.3, opacity: 0.78, dash: '6 5' },
  base: { color: '#64748b', weight: 1.0, opacity: 0.5 },
}

/** 图例顺序：粗到细、亮到淡，基础链路放最后 */
export const FIBER_LEGEND: { group: FiberGroup; key: string }[] = [
  { group: 'f40', key: 'olibV2.fiber.f40' },
  { group: 'f10', key: 'olibV2.fiber.f10' },
  { group: 'ipran', key: 'olibV2.fiber.ipran' },
  { group: 'p26', key: 'olibV2.fiber.p26' },
  { group: 'plan', key: 'olibV2.fiber.plan' },
  { group: 'base', key: 'olibV2.fiber.base' },
]
