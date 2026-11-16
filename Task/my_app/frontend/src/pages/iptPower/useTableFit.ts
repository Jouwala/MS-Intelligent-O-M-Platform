/* ============================================================================
 * 表格「吃掉卡片剩余高度」的测量钩子（2026-11-16 新增）
 * ----------------------------------------------------------------------------
 * 背景：ipt_power 页要求右侧「Sites & latest power snapshot」卡与左侧地图卡
 *   **等高**，且表体填满卡片剩余高度（用户 2026-11-16：「地图高度自适应铺满
 *   剩余高度，不要有空白，Sites & latest power snapshot 的高度和地图对齐」）。
 *
 * 为什么必须量像素：antd Table 的纵向滚动只认 `scroll.y` 这个数值 ——
 *   rc-table 把它写成 .ant-table-body 的 max-height。写 '100%' 时百分比相对
 *   的是「含表头的容器」，表体会多出一整段表头高度，分页也会被挤出卡片。
 *   故这里按「可视区高度 − 表头 − 分页(含其上外边距)」算 body 可用高度，
 *   再用它反推一页能放多少行（避免 15 行之外留一大片空白）。
 *
 * 为什么用 ResizeObserver 而不是 window.resize：高度变化不只是视口缩放 ——
 *   口径说明展开/收起、KPI 卡片换行都会改变这个区域的高度，window 事件抓不到。
 *
 * ⚠️ 表头高度取 .ant-table-thead，**不取 .ant-table-header**：后者只在
 *   scroll.y > 0（固定表头生效）时才有，首轮量不到会让表体多出一段表头高度，
 *   而且量到/量不到之间反复切换会让 scroll.y 抖动（渲染循环）。
 *   thead 在两种状态下都存在且同高，一次就能量准，不依赖第二轮。
 * ========================================================================== */
import { useCallback, useLayoutEffect, useState } from 'react'

export type TableFit = {
  /** 表体最大高度（px）；0 = 还没量到（交给 antd 默认渲染） */
  scrollY: number
  /** 一页能放下多少行（下限见 MIN_ROWS） */
  rows: number
}

/** 极矮容器（门户里的窄 iframe）下也别只显示一两行；正好是分页器的标准档位，
    免得出现「7 / page」这种不在下拉清单里的怪值（宽屏下分页器只读这里，不允许改） */
const MIN_ROWS = 10

/** 表头/分页之外的余量：表格描边 + 亚像素，避免最后一行被裁掉半截 */
const SAFETY_PX = 3

/** 把表体高度钉成量出来的可用高度（理由见 measure 里的注释） */
function applyBodyHeight(area: HTMLElement, height: number) {
  const body = area.querySelector('.ant-table-body') as HTMLElement | null
  if (body) body.style.height = `${height}px`
}

export function useTableFit(enabled: boolean, rowCount: number) {
  const [area, setArea] = useState<HTMLDivElement | null>(null)
  const [fit, setFit] = useState<TableFit>({ scrollY: 0, rows: 15 })

  const measure = useCallback(() => {
    if (!enabled || !area) return
    const areaH = area.clientHeight
    if (areaH <= 0) return

    const thead = area.querySelector('.ant-table-thead') as HTMLElement | null
    const pag = area.querySelector('.ant-pagination') as HTMLElement | null
    const row = area.querySelector('.ant-table-tbody .ant-table-row') as HTMLElement | null

    const headH = thead ? thead.getBoundingClientRect().height : 0
    const pagH = pag
      ? pag.getBoundingClientRect().height + (parseFloat(getComputedStyle(pag).marginTop) || 0)
      : 0
    const rowH = row ? row.getBoundingClientRect().height : 0

    const avail = areaH - headH - pagH - SAFETY_PX
    if (avail < 40) return

    const rowsFit = rowH > 0 ? Math.max(MIN_ROWS, Math.floor((avail - 2) / rowH)) : null
    const scrollY = Math.floor(avail)

    // 量出来的值没变就 bail-out：ResizeObserver + setState 共处一室，
    // 每次渲染都写一遍新对象会自己把自己重新触发。
    setFit(prev => (prev.scrollY === scrollY && (rowsFit == null || prev.rows === rowsFit)
      ? prev
      : { scrollY, rows: rowsFit ?? prev.rows }))

    /* 表体「撑满」而不是「按内容高」：rc-table 只把 scroll.y 写成 max-height，
       一页行数装不满时 .ant-table-body 会缩到内容高度，分页就被顶到卡片中间、
       下面留一截空白（2026-11-16 实测 1080 高视口留 50px、1440 高留 40px）。
       这里直接把可用高度写成 height，空出来的部分落在表体内部（最后一行之下），
       分页始终贴卡片底边。React 只 diff 自己写过的 style 键，不会把 height 清掉。 */
    applyBodyHeight(area, scrollY)
  }, [enabled, area])

  useLayoutEffect(() => {
    if (!enabled || !area || !fit.scrollY) return
    // .ant-table-body 是 rc-table 在 scroll.y > 0 时才渲染的，首轮量的时候还不存在，
    // 故等到 scroll.y 落地后补一次。
    applyBodyHeight(area, fit.scrollY)
  }, [enabled, area, fit.scrollY, rowCount])

  useLayoutEffect(() => {
    if (!enabled || !area) return
    measure()
    if (typeof ResizeObserver === 'undefined') return

    let raf = 0
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(measure)
    })
    ro.observe(area)
    return () => {
      ro.disconnect()
      cancelAnimationFrame(raf)
    }
    /* rowCount 进依赖：首屏数据未到时表格只有表头 + 空态，行高量不到；
       数据落地后必须重新量一次，否则 pageSize 会停在上一次的兜底值。 */
  }, [enabled, area, measure, rowCount])

  return { areaRef: setArea, fit }
}
