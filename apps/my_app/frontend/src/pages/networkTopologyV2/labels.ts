/* ============================================================================
 * OLIB GIS 拓扑 —— 文案映射（显式 switch，禁止动态拼 i18n key）
 * ============================================================================
 * i18n 守卫（check:i18n）会把 `t('a.' + v)` 读成缺键 `a.`，所以这里统一用
 * switch 返回**字面量**键。页面、图例、详情、tooltip 共用同一套口径。
 * ========================================================================== */
import { siteKind } from './geoData'
import type { GeoMatchMethod, GeoSite } from './types'

export type TFn = (key: string, params?: Record<string, string | number>) => string

/** 站点类型（核心/ISP/5G/普通）。 */
export function siteTypeLabel(site: GeoSite, t: TFn): string {
  switch (siteKind(site)) {
    case 'core': return t('olibV2.type.core')
    case 'isp': return t('olibV2.type.isp')
    case '5g': return t('olibV2.type.5g')
    default: return t('olibV2.type.normal')
  }
}

/** 匹配方式（精确/别名/未匹配/歧义/映射过期）。 */
export function methodLabel(method: GeoMatchMethod, t: TFn): string {
  switch (method) {
    case 'name-exact': return t('olibV2.method.exact')
    case 'alias': return t('olibV2.method.alias')
    case 'ambiguous': return t('olibV2.method.ambiguous')
    case 'missing': return t('olibV2.method.missing')
    default: return t('olibV2.method.unmatched')
  }
}

/** 未定位原因（只用于提醒条/名单，不参与地图）。 */
export function reasonLabel(reason: string | undefined, t: TFn): string {
  switch (reason) {
    case 'same-name-different-coords': return t('olibV2.reason.ambiguous')
    case 'mapping-out-of-date': return t('olibV2.reason.outOfDate')
    case 'no-coordinates': return t('olibV2.reason.noCoords')
    default: return t('olibV2.reason.noName')
  }
}
