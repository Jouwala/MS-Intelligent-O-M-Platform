/* ============================================================================
 * OLIB GIS 拓扑 —— 县界底衬
 * ============================================================================
 * 数据来自 GET /api/gis/county-boundaries（后端 1 小时缓存，15 个县的多边形）。
 * 只做两件事：
 *   1. 画淡色虚线县界（不填充），让用户知道站点落在哪个县；
 *   2. zoom≥8.5 时在县多边形质心放县名标签（小字 + 描边，不接交互）。
 *
 * 县界是**背景信息**，站点与链路才是主体；所以描边透明度压在 0.5 左右，
 * 标签只在放大后出现。接口失败时静默跳过（地图主体仍可用）。
 * ========================================================================== */
import { useEffect } from 'react'
import L from 'leaflet'
import { isMapAlive, useMap } from '../../components/LeafletMap'

export interface CountyPolygon {
  name: string
  db_name?: string
  polygons: [number, number][][]
}

/** 取最大外环的坐标平均值作为标签落点（够用且不依赖 GIS 库）。 */
function centroidOf(county: CountyPolygon): [number, number] | null {
  let best: [number, number][] | null = null
  for (const ring of county.polygons || []) {
    if (Array.isArray(ring) && ring.length > (best?.length ?? 0)) best = ring
  }
  if (!best?.length) return null
  let lat = 0
  let lng = 0
  for (const p of best) {
    lat += p[0]
    lng += p[1]
  }
  return [lat / best.length, lng / best.length]
}

export default function CountyLayer({
  counties, isDark,
}: {
  counties: CountyPolygon[]
  isDark: boolean
}) {
  const map = useMap()

  useEffect(() => {
    if (!isMapAlive(map) || !counties.length) return
    const border = L.layerGroup().addTo(map)
    const labels = L.layerGroup()

    const stroke = isDark ? '#3f5478' : '#8b9bb0'
    const textColor = isDark ? '#9fb3cf' : '#4b6178'
    const halo = isDark ? '#0a1428' : '#ffffff'

    for (const county of counties) {
      const rings = (county.polygons || []).filter((r) => Array.isArray(r) && r.length > 2)
      if (!rings.length) continue
      L.polygon(rings, {
        color: stroke,
        weight: 1.2,
        opacity: 0.8,
        dashArray: '4 3',
        /* 极淡填充只用来托出国土轮廓：底图没有 earth 图层，
           不铺这一层时海洋与陆地同色，国家形状只能靠道路辨认。 */
        fill: true,
        fillColor: isDark ? '#12233d' : '#e7eef6',
        fillOpacity: isDark ? 0.32 : 0.5,
        interactive: false,
      }).addTo(border)

      const c = centroidOf(county)
      if (!c) continue
      const html = `<span class="olibv2-county" style="color:${textColor};text-shadow:0 0 3px ${halo},0 0 3px ${halo}">`
        + `${county.name.toUpperCase()}</span>`
      L.marker(c, {
        icon: L.divIcon({ className: 'olibv2-county-icon', html, iconSize: [0, 0], iconAnchor: [0, 0] }),
        interactive: false,
        keyboard: false,
      }).addTo(labels)
    }

    const syncLabels = () => {
      if (!isMapAlive(map)) return
      if (map.getZoom() >= 8.5) {
        if (!map.hasLayer(labels)) labels.addTo(map)
      } else if (map.hasLayer(labels)) {
        map.removeLayer(labels)
      }
    }
    syncLabels()
    map.on('zoomend', syncLabels)

    return () => {
      map.off('zoomend', syncLabels)
      labels.remove()
      border.remove()
    }
  }, [map, counties, isDark])

  return null
}
