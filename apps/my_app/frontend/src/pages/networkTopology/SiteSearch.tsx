/* ============================================================================
 * OLIB FO 网络拓扑 —— 站点查询（搜索 / 区域 / 快捷筛选 / 结果列表）
 * ============================================================================
 * 结果在页面层过滤（667 条静态数据，无需接口）；这里只负责展示与回调。
 * 列表最多渲染 300 行并在超出时明确提示，不做静默截断。
 * ========================================================================== */
import { Empty, Input, Select, Tag } from 'antd'
import { SearchOutlined } from '@ant-design/icons'

import { useI18n } from '../../i18n'
import { WH } from '../../theme'
import { CardBase } from '../../components/cards'
import {
  SECTORS, SITE_COLORS, sitePercent,
  type SiteFilter, type TopoSite,
} from './topologyData'

const MAX_ROWS = 300
const FILTERS: { value: SiteFilter; key: string }[] = [
  { value: 'all', key: 'olibTopo.search.chipAll' },
  { value: '5g', key: 'olibTopo.search.chip5g' },
  { value: 'isp', key: 'olibTopo.search.chipIsp' },
  { value: 'core', key: 'olibTopo.search.chipCore' },
]

export function SiteTags({ site }: { site: TopoSite }) {
  const { t } = useI18n()
  const tag = (text: string, color: string, dark = false) => (
    <span style={{
      fontSize: 10, lineHeight: '16px', padding: '0 6px', borderRadius: 999,
      background: dark ? '#111' : color, color: '#fff', marginLeft: 5, whiteSpace: 'nowrap',
    }}>{text}</span>
  )
  return (
    <>
      {site.core && tag(t('olibTopo.tag.core'), SITE_COLORS.core, true)}
      {site.is5g && tag(t('olibTopo.tag.5g'), SITE_COLORS.fiveG)}
      {site.isp && tag(t('olibTopo.tag.isp'), SITE_COLORS.isp)}
    </>
  )
}

interface Props {
  query: string
  onQuery: (v: string) => void
  sector: string
  onSector: (v: string) => void
  filter: SiteFilter
  onFilter: (f: SiteFilter) => void
  results: TopoSite[]
  total: number
  selected: TopoSite | null
  onPick: (s: TopoSite) => void
}

export default function SiteSearch({
  query, onQuery, sector, onSector, filter, onFilter, results, total, selected, onPick,
}: Props) {
  const { t } = useI18n()
  const shown = results.slice(0, MAX_ROWS)

  return (
    <CardBase title={t('olibTopo.search.title')} bodyStyle={{ padding: 'var(--wh-space-sm) var(--wh-space-md)' }}>
      <Input
        allowClear
        value={query}
        onChange={e => onQuery(e.target.value)}
        onPressEnter={() => { if (results[0]) onPick(results[0]) }}
        prefix={<SearchOutlined style={{ color: WH.textDim }} />}
        placeholder={t('olibTopo.search.placeholder')}
        aria-label={t('olibTopo.search.placeholder')}
      />
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 'var(--wh-space-sm)' }}>
        {FILTERS.map(f => (
          <Tag.CheckableTag
            key={f.value}
            checked={filter === f.value}
            onChange={() => onFilter(f.value)}
          >
            {t(f.key)}
          </Tag.CheckableTag>
        ))}
      </div>
      <Select
        allowClear
        value={sector || undefined}
        onChange={v => onSector(v || '')}
        placeholder={t('olibTopo.search.allSectors')}
        options={SECTORS.map(s => ({ value: s, label: s }))}
        style={{ width: '100%', marginTop: 'var(--wh-space-sm)' }}
        size="small"
      />
      <div style={{ fontSize: 12, color: WH.textMuted, marginTop: 'var(--wh-space-sm)' }}>
        {t('olibTopo.search.count', { n: results.length, total })}
        {results.length > MAX_ROWS && (
          <div style={{ color: 'var(--wh-warn-text)', marginTop: 2 }}>
            {t('olibTopo.search.truncated', { n: MAX_ROWS })}
          </div>
        )}
      </div>

      <div style={{ maxHeight: 300, overflow: 'auto', marginTop: 'var(--wh-space-xs)' }}>
        {shown.length === 0 && (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('olibTopo.search.empty')} />
        )}
        {shown.map(s => {
          const active = selected?.name === s.name
          return (
            <div
              key={s.name}
              role="button"
              tabIndex={0}
              onClick={() => onPick(s)}
              onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPick(s) } }}
              style={{
                padding: '7px 8px',
                cursor: 'pointer',
                borderLeft: active ? '3px solid var(--wh-teal)' : '3px solid transparent',
                background: active ? 'var(--wh-teal-20)' : undefined,
                borderRadius: 4,
              }}
            >
              <div style={{ fontSize: 13, color: WH.text }}>
                <b>{s.name}</b>
                <SiteTags site={s} />
              </div>
              <div style={{ fontSize: 11, color: WH.textDim, marginTop: 2 }}>
                {s.sector} · {t('olibTopo.site.pos', {
                  x: sitePercent(s.x).toFixed(1), y: sitePercent(s.y).toFixed(1),
                })}
              </div>
            </div>
          )
        })}
      </div>
    </CardBase>
  )
}
