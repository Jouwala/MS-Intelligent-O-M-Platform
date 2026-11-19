/* ============================================================================
 * 流量负载色阶与中分几何（vitest，纯函数）
 * ============================================================================
 * 三件事必须守住：
 *   1. 8 档色阶的边界是左闭右开，且与 Weathermap 的 DEFAULT SCALE 逐一对应；
 *   2. 「没数据」不能落进 0-1% 那一档（否则用户看到的是「0% 灰白」而不是 N/A）；
 *   3. 中分两段拼回去仍是同一条二次贝塞尔（两段在 t=0.5 处相连、端点不漂）。
 * ========================================================================== */
import { describe, expect, it } from 'vitest'

import {
  TRAFFIC_BINS, TRAFFIC_NA_COLOR, quadraticAt, splitQuadraticAtMid, trafficLabel, weathermapColor,
} from '../trafficScale'

describe('流量色阶', () => {
  it('8 档、区间首尾相接、上沿到 100', () => {
    expect(TRAFFIC_BINS).toHaveLength(8)
    expect(TRAFFIC_BINS[0].min).toBe(0)
    expect(TRAFFIC_BINS[TRAFFIC_BINS.length - 1].max).toBe(100)
    for (let i = 1; i < TRAFFIC_BINS.length; i++) {
      expect(TRAFFIC_BINS[i].min).toBe(TRAFFIC_BINS[i - 1].max)
    }
  })

  it('落在正确的档位（左闭右开）', () => {
    expect(weathermapColor(0)).toBe(TRAFFIC_BINS[0].color)
    expect(weathermapColor(0.99)).toBe(TRAFFIC_BINS[0].color)
    expect(weathermapColor(1)).toBe(TRAFFIC_BINS[1].color)
    expect(weathermapColor(9.99)).toBe(TRAFFIC_BINS[1].color)
    expect(weathermapColor(10)).toBe(TRAFFIC_BINS[2].color)
    expect(weathermapColor(25)).toBe(TRAFFIC_BINS[3].color)
    expect(weathermapColor(84.9)).toBe(TRAFFIC_BINS[6].color)
    expect(weathermapColor(85)).toBe(TRAFFIC_BINS[7].color)
  })

  it('超过 100% 仍按最高档上色（计数器与带宽口径不一致时不静默截断数字）', () => {
    expect(weathermapColor(180)).toBe(TRAFFIC_BINS[7].color)
  })

  it('缺数据 = N/A 色，不是 0% 的灰白', () => {
    expect(weathermapColor(null)).toBe(TRAFFIC_NA_COLOR)
    expect(weathermapColor(undefined)).toBe(TRAFFIC_NA_COLOR)
    expect(weathermapColor(NaN)).toBe(TRAFFIC_NA_COLOR)
    expect(TRAFFIC_NA_COLOR).not.toBe(TRAFFIC_BINS[0].color)
  })

  it('百分比标签：一位小数，缺数据留空（不画标签）', () => {
    expect(trafficLabel(32.876)).toBe('32.9%')
    expect(trafficLabel(0)).toBe('0.0%')
    expect(trafficLabel(null)).toBe('')
    expect(trafficLabel(undefined)).toBe('')
  })
})

describe('中分几何', () => {
  const a = { x: 0, y: 0 }
  const c = { x: 50, y: 20 }
  const b = { x: 100, y: 0 }

  it('mid 落在曲线的 t=0.5 处，两段端点与首尾一致', () => {
    const { mid, first, second } = splitQuadraticAtMid(a, c, b)
    const at = quadraticAt(a, c, b, 0.5)
    expect(mid.x).toBeCloseTo(at.x, 6)
    expect(mid.y).toBeCloseTo(at.y, 6)
    expect(first[0]).toEqual(a)
    expect(first[2]).toEqual(mid)
    expect(second[0]).toEqual(mid)
    expect(second[2]).toEqual(b)
  })

  it('参数端点：t=0 是起点、t=1 是终点（标签定位不会飘到曲线外）', () => {
    expect(quadraticAt(a, c, b, 0)).toEqual(a)
    const end = quadraticAt(a, c, b, 1)
    expect(end.x).toBeCloseTo(b.x, 6)
    expect(end.y).toBeCloseTo(b.y, 6)
  })
})
