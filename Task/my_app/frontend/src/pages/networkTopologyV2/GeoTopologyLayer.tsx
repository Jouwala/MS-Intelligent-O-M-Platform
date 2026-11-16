/* ============================================================================
 * OLIB GIS 拓扑 —— Leaflet 站点/链路图层
 * ============================================================================
 * 只负责「把已定位的站点与骨干链路画到地图上」，不持有页面状态。
 *
 * 关键行为
 *   · 站点按**屏幕像素网格**聚合（缩放/平移结束后重算），避免蒙罗维亚 110 个点
 *     叠成一团；点聚合圈 → fitBounds 展开。
 *   · 站点图标按类型区分：核心=紫色圆环、ISP=蓝色菱形、5G=红色圆点、普通=灰点。
 *     只有选中站点有脉冲光环（不给 553 个点同时跑动画）。
 *   · 站名分级显示：zoom≥9 显示核心/ISP/5G 名称，zoom≥11 显示全部名称，
 *     选中站点始终显示。低倍率下用悬浮 tooltip 看名字。
 *   · 链路是**连接关系示意直线**（见 geoData.ts 顶部说明），同端点多条链路
 *     做垂直偏移；点线可看两端站点与链路类型。
 *   · 所有 addTo 之前过 isMapAlive()：地图销毁后 appendChild(undefined) 会打崩整页。
 * ========================================================================== */
import { useEffect } from 'react'
import L from 'leaflet'
import { isMapAlive, useMap } from '../../components/LeafletMap'
import type { CoreLinkType } from '../networkTopology/topologyData'
import { siteKind } from './geoData'
import type { GeoLink, GeoSite } from './types'

const esc = (s: string) => s
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;')

const DASH: Record<CoreLinkType, string | undefined> = {
  exist: undefined,
  dwdm: '7 5',
  rail: '3 5',
  p25a: '10 4 3 4',
}

/** 像素聚合网格：zoom 越低格子越大；zoom≥11.5 不再聚合。 */
function bucketSizeOf(zoom: number): number {
  if (zoom < 8.5) return 56
  if (zoom < 9.5) return 46
  if (zoom < 10.5) return 38
  if (zoom < 11) return 30
  return 0
}

export interface GeoTopologyLayerProps {
  sites: GeoSite[]
  links: GeoLink[]
  selected: GeoSite | null
  focus: { index: number; token: number; zoom?: number } | null
  onSelect: (index: number) => void
  typeLabel: (site: GeoSite) => string
  clusterLabel: (count: number) => string
  linkTypeLabel: (type: CoreLinkType) => string
}

export default function GeoTopologyLayer({
  sites, links, selected, focus, onSelect, typeLabel, clusterLabel, linkTypeLabel,
}: GeoTopologyLayerProps) {
  const map = useMap()

  /* ---------- 站点 + 聚合 ---------- */
  useEffect(() => {
    if (!isMapAlive(map)) return
    const group = L.layerGroup().addTo(map)
    const container = map.getContainer()
    const selectedIndex = selected?.index ?? -1

    const drawSite = (site: GeoSite) => {
      const kind = siteKind(site)
      const isSel = site.index === selectedIndex
      const html = `<div class="olibv2-site olibv2-site--${kind}${isSel ? ' is-selected' : ''}">`
        + '<span class="olibv2-dot"></span>'
        + `<span class="olibv2-name">${esc(site.name)}</span></div>`
      const icon = L.divIcon({ className: 'olibv2-site-icon', html, iconSize: [0, 0], iconAnchor: [0, 0] })
      const marker = L.marker([site.lat as number, site.lng as number], {
        icon, keyboard: false, riseOnHover: true, zIndexOffset: isSel ? 500 : 0,
      })
      marker.bindTooltip(
        `<strong>${esc(site.name)}</strong><br/>${esc(typeLabel(site))}`
        + (site.county ? ` · ${esc(site.county)}` : ''),
        { direction: 'top', offset: [0, -8], opacity: 0.96, className: 'olibv2-tip' },
      )
      marker.on('click', (ev: L.LeafletMouseEvent) => {
        L.DomEvent.stopPropagation(ev.originalEvent)
        onSelect(site.index)
      })
      marker.addTo(group)
    }

    const drawCluster = (members: GeoSite[]) => {
      const count = members.length
      const lat = members.reduce((n, s) => n + (s.lat as number), 0) / count
      const lng = members.reduce((n, s) => n + (s.lng as number), 0) / count
      const px = Math.min(62, 26 + Math.log2(count) * 7)
      const html = `<div class="olibv2-cluster" style="width:${px.toFixed(0)}px;height:${px.toFixed(0)}px">`
        + `<span>${count}</span></div>`
      const icon = L.divIcon({
        className: 'olibv2-cluster-icon', html,
        iconSize: [px, px], iconAnchor: [px / 2, px / 2],
      })
      const marker = L.marker([lat, lng], { icon, keyboard: false })
      marker.bindTooltip(clusterLabel(count), { direction: 'top', offset: [0, -px / 2], className: 'olibv2-tip' })
      marker.on('click', (ev: L.LeafletMouseEvent) => {
        L.DomEvent.stopPropagation(ev.originalEvent)
        const bounds = L.latLngBounds(members.map((s) => [s.lat as number, s.lng as number] as [number, number]))
        map.fitBounds(bounds, { padding: [52, 52], maxZoom: 13 })
      })
      marker.addTo(group)
    }

    const rebuild = () => {
      if (!isMapAlive(map)) return
      group.clearLayers()
      const zoom = map.getZoom()
      container.classList.toggle('olibv2-labels-major', zoom >= 9)
      container.classList.toggle('olibv2-labels-all', zoom >= 11)

      const size = bucketSizeOf(zoom)
      if (!size) {
        for (const site of sites) if (site.located) drawSite(site)
        return
      }
      const buckets = new Map<string, GeoSite[]>()
      for (const site of sites) {
        if (!site.located) continue
        /* 骨干核心永远单独画：国家尺度下它们是这张网的主干，不能被卷进聚合圈。
           其它站点（含 ISP/5G）参与网格聚合，由聚合圈展开。 */
        if (site.core) continue
        const p = map.latLngToContainerPoint([site.lat as number, site.lng as number])
        const key = `${Math.floor(p.x / size)}:${Math.floor(p.y / size)}`
        const list = buckets.get(key)
        if (list) list.push(site)
        else buckets.set(key, [site])
      }
      for (const members of buckets.values()) {
        if (members.length === 1) drawSite(members[0])
        else drawCluster(members)
      }
      /* 核心站点最后画，保证不被聚合圈盖住 */
      for (const site of sites) if (site.located && site.core) drawSite(site)
    }

    rebuild()
    map.on('moveend', rebuild)
    map.on('zoomend', rebuild)
    return () => {
      map.off('moveend', rebuild)
      map.off('zoomend', rebuild)
      container.classList.remove('olibv2-labels-major', 'olibv2-labels-all')
      group.remove()
    }
  }, [map, sites, selected?.index, onSelect, typeLabel, clusterLabel, selected])

  /* ---------- 骨干链路（示意直线） ---------- */
  useEffect(() => {
    if (!isMapAlive(map)) return
    const group = L.layerGroup().addTo(map)

    for (const link of links) {
      const aLat = link.a.lat as number
      const aLng = link.a.lng as number
      const bLat = link.b.lat as number
      const bLng = link.b.lng as number
      let a: [number, number] = [aLat, aLng]
      let b: [number, number] = [bLat, bLng]
      if (link.offset) {
        const dLat = bLat - aLat
        const dLng = bLng - aLng
        const len = Math.hypot(dLat, dLng) || 1
        const step = 0.004 * link.offset
        const oLat = (-dLng / len) * step
        const oLng = (dLat / len) * step
        a = [aLat + oLat, aLng + oLng]
        b = [bLat + oLat, bLng + oLng]
      }
      const label = linkTypeLabel(link.type)
      const line = L.polyline([a, b], {
        color: link.color,
        weight: 2.2,
        opacity: 0.78,
        dashArray: DASH[link.type],
        lineCap: 'round',
      })
      line.bindTooltip(
        `<strong>${esc(link.a.name)}</strong> ↔ <strong>${esc(link.b.name)}</strong><br/>${esc(label)}`,
        { sticky: true, className: 'olibv2-tip', opacity: 0.96 },
      )
      line.bindPopup(
        `<div class="olibv2-link-pop"><strong>${esc(link.a.name)}</strong> ↔ `
        + `<strong>${esc(link.b.name)}</strong><br/>${esc(label)}</div>`,
      )
      line.addTo(group)
    }

    return () => { group.remove() }
  }, [map, links, linkTypeLabel])

  /* ---------- 外部定位（搜索结果 / 详情按钮） ---------- */
  useEffect(() => {
    if (!focus || !isMapAlive(map)) return
    const site = sites.find((s) => s.index === focus.index && s.located)
    if (!site) return
    const zoom = focus.zoom ?? Math.max(map.getZoom(), 11)
    map.flyTo([site.lat as number, site.lng as number], Math.min(zoom, 13), { duration: 0.7 })
    // 只在 token 变化时飞一次；selected 同步更新不重复触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.token])

  return null
}
