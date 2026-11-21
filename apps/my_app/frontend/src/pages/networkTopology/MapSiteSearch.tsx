/* ============================================================================
 * OLIB FO 网络拓扑 —— 画布上的站点模糊搜索（门户 maponly 用）
 * ============================================================================
 * 门户 GIS Map 栏只留物理图，右侧查询栏不在画面里，所以搜索框浮在画布左上角。
 * 列表最多 8 条：门户默认锁住滚轮，长列表滚不动，多出来的明确提示继续输入。
 * ========================================================================== */
import { useEffect, useId, useRef, useState } from 'react'
import { Input } from 'antd'
import { SearchOutlined } from '@ant-design/icons'

import { useI18n } from '../../i18n'
import { WH } from '../../theme'
import type { TopoSite } from './topologyData'
import { SiteTags } from './SiteSearch'

const VISIBLE = 8

interface Props {
  query: string
  onQuery: (v: string) => void
  results: TopoSite[]
  total: number
  onPick: (s: TopoSite) => void
}

export default function MapSiteSearch({ query, onQuery, results, total, onPick }: Props) {
  const { t } = useI18n()
  const listId = useId()
  const boxRef = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const shown = results.slice(0, VISIBLE)
  const q = query.trim()

  useEffect(() => { setActive(0) }, [query])

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', onDown)
    return () => document.removeEventListener('pointerdown', onDown)
  }, [])

  const pick = (s: TopoSite) => {
    onPick(s)
    onQuery(s.name)
    setOpen(false)
  }

  return (
    <div
      ref={boxRef}
      className="olib-finder"
      onPointerDown={e => e.stopPropagation()}
    >
      <Input
        allowClear
        value={query}
        role="combobox"
        aria-expanded={open && q.length > 0}
        aria-controls={listId}
        aria-autocomplete="list"
        prefix={<SearchOutlined style={{ color: WH.textDim }} />}
        placeholder={t('olibTopo.search.placeholder')}
        aria-label={t('olibTopo.search.placeholder')}
        onChange={e => { onQuery(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onKeyDown={e => {
          if (e.key === 'Escape') { setOpen(false); return }
          if (!shown.length) return
          if (e.key === 'ArrowDown') {
            e.preventDefault()
            setOpen(true)
            setActive(i => Math.min(shown.length - 1, i + 1))
          } else if (e.key === 'ArrowUp') {
            e.preventDefault()
            setActive(i => Math.max(0, i - 1))
          } else if (e.key === 'Enter' && open) {
            e.preventDefault()
            pick(shown[Math.min(active, shown.length - 1)])
          }
        }}
      />
      {open && q.length > 0 && (
        <div
          id={listId}
          role="listbox"
          style={{
            marginTop: 6, maxHeight: 280, overflow: 'auto',
            background: WH.cardBg, color: WH.text,
            border: WH.cardBorder, borderRadius: 'var(--wh-radius-md)',
            boxShadow: 'var(--wh-shadow-2)',
          }}
        >
          <div style={{ fontSize: 12, color: WH.textMuted, padding: '6px 8px 2px' }}>
            {t('olibTopo.search.count', { n: results.length, total })}
          </div>
          {shown.length === 0 && (
            <div style={{ padding: '8px 10px 10px', color: WH.textDim, fontSize: 13 }}>
              {t('olibTopo.search.empty')}
            </div>
          )}
          {shown.map((s, i) => (
            <div
              key={`${s.name}-${s.x}-${s.y}`}
              role="option"
              aria-selected={i === active}
              onMouseEnter={() => setActive(i)}
              onClick={() => pick(s)}
              style={{
                padding: '6px 8px', cursor: 'pointer',
                background: i === active ? 'var(--wh-teal-20)' : undefined,
              }}
            >
              <div style={{ fontSize: 13 }}>
                <b>{s.name}</b>
                <SiteTags site={s} />
              </div>
              <div style={{ fontSize: 11, color: WH.textDim }}>{s.sector}</div>
            </div>
          ))}
          {results.length > VISIBLE && (
            <div style={{ fontSize: 12, color: 'var(--wh-warn-text)', padding: '4px 8px 8px' }}>
              {t('olibTopo.search.more', { n: results.length - VISIBLE })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
