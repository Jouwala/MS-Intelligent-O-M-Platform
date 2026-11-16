/* ============================================================================
 * OLIB GIS 拓扑 —— 站点 GPS 映射表加载
 * ============================================================================
 * 映射表是 public/olib/olib-site-geo.json（约 92KB，553 条），由
 * scripts/gen-olib-site-geo.mjs 生成，来源是 /api/gis/sites 的 master_site_db
 * 快照。页面**不**在运行时做名称匹配：匹配口径必须可复查、可 diff。
 *
 * 为什么要单独 fetch 而不是 import：
 *   553 条映射是数据资产，不是代码；放 public 后不进入 JS bundle，改映射表
 *   不需要重建整个前端 chunk（但仍需 build/刷新，见 vite public 的复制时机）。
 * ========================================================================== */
import { useCallback, useEffect, useState } from 'react'
import type { GeoFile } from './types'

const URL = `${import.meta.env.BASE_URL}olib/olib-site-geo.json`

function assertFile(value: unknown): GeoFile {
  const v = value as GeoFile | null
  if (!v || !Array.isArray(v.records)) {
    throw new Error('olib-site-geo.json 结构不正确：缺少 records 数组')
  }
  if (!v.summary || typeof v.summary.total !== 'number') {
    throw new Error('olib-site-geo.json 结构不正确：缺少 summary')
  }
  return v
}

export function useOlibSiteGeo() {
  const [file, setFile] = useState<GeoFile | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  const reload = useCallback(() => setTick((n) => n + 1), [])

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError(null)
    fetch(URL, { headers: { Accept: 'application/json' } })
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
        return res.json()
      })
      .then((json) => {
        if (!alive) return
        setFile(assertFile(json))
      })
      .catch((err: unknown) => {
        if (!alive) return
        setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => { alive = false }
  }, [tick])

  return { file, loading, error, reload }
}
