/* ============================================================================
 * OLIB GIS 拓扑 —— PDF 链路数据加载（第 1 页数字化）
 * ============================================================================
 * 数据是 public/olib/olib-fiber-links.json（约 40KB，500+ 条站点对边），由
 * backend/scripts/gen_olib_fiber_links.py 生成。和站点 GPS 映射表一样放 public：
 * 它是数据资产，改链路表不需要重建 JS bundle。
 *
 * 加载失败不阻断页面：只关掉这一层，图例里写明失败原因（骨干链路照常显示）。
 * ========================================================================== */
import { useCallback, useEffect, useState } from 'react'
import type { FiberFile } from './types'

const URL = `${import.meta.env.BASE_URL}olib/olib-fiber-links.json`

function assertFile(value: unknown): FiberFile {
  const v = value as FiberFile | null
  if (!v || !Array.isArray(v.edges)) {
    throw new Error('olib-fiber-links.json 结构不正确：缺少 edges 数组')
  }
  if (!v.sites || typeof v.sites !== 'object') {
    throw new Error('olib-fiber-links.json 结构不正确：缺少 sites 映射')
  }
  return v
}

export function useOlibFiberLinks() {
  const [file, setFile] = useState<FiberFile | null>(null)
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
        if (alive) setFile(assertFile(json))
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => { alive = false }
  }, [tick])

  return { file, loading, error, reload }
}
