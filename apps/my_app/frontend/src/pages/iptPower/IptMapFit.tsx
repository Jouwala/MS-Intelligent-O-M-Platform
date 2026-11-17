/* ============================================================================
 * 地图容器尺寸变化时让 Leaflet 重新量尺寸（2026-11-16 新增）
 * ----------------------------------------------------------------------------
 * 起因：本页地图高度改为「吃掉卡片剩余高度」（flex 分配，见 IptPowerQuery.tsx
 *   的布局注释）。容器高度不再是一个固定 clamp 值 —— 窗口缩放、口径说明展开/
 *   收起、KPI 卡片换行都会改变它。Leaflet 只在建图那一刻量一次容器尺寸：
 *   容器变大后右下角会露出没画瓦片的灰底，变小则瓦片与圆点被裁掉。
 *
 * 做法：监听地图容器本身（不是 window），ResizeObserver 回调里用 rAF 合并成
 *   一次 invalidateSize。不要改成 window.resize —— 上面列的变化里有两项与窗口
 *   尺寸无关，window 事件抓不到（改了就会「展开说明后地图下半截空白」）。
 *
 * pan:false —— 只重算尺寸，不要顺手平移视图；本页没有「容器变大后仍保持
 *   原中心居中」的需求，开着 pan 会在拖过地图后突然回跳。
 *
 * ⚠️ 必须是 <MapContainer> 的子组件（内部 useMap()）：放到容器外会抛
 *   「useMap 必须在 <MapContainer> 内使用」并打崩整页。
 * ========================================================================== */
import { useEffect } from 'react'

import { useMap } from '../../components/LeafletMap'

export function IptMapFit() {
  const map = useMap()

  useEffect(() => {
    const el = map.getContainer()
    if (!el || typeof ResizeObserver === 'undefined') return

    let raf = 0
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        // 组件卸载 / 地图被 remove() 后容器已摘掉，不再调
        if (map.getContainer() !== el) return
        map.invalidateSize({ pan: false })
      })
    })
    ro.observe(el)
    return () => {
      ro.disconnect()
      cancelAnimationFrame(raf)
    }
  }, [map])

  return null
}
