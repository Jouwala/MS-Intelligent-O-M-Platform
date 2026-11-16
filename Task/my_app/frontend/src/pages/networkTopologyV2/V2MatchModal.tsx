/* ============================================================================
 * OLIB GIS 拓扑 —— 未定位站点名单
 * ============================================================================
 * 用户点「未定位 N」时打开：逐条列出没有上图的原因（主数据无同名 / 同名多坐标 /
 * 映射表与站点清单版本不一致）。这是数据异常清单，必须可查，不能只在角落写一句。
 * ========================================================================== */
import DraggableClickModal from '../../components/DraggableClickModal'
import { useI18n } from '../../i18n'
import { reasonLabel } from './labels'
import type { GeoSite } from './types'

export default function V2MatchModal({
  open, onClose, sites, anchor,
}: {
  open: boolean
  onClose: () => void
  sites: GeoSite[]
  anchor?: { x: number; y: number } | null
}) {
  const { t } = useI18n()

  return (
    <DraggableClickModal
      open={open}
      onClose={onClose}
      anchor={anchor}
      title={t('olibV2.match.title')}
      width="92%"
      maxWidth="52rem"
      headerExtra={<span className="olibv2-match-count">{t('olibV2.match.count', { n: sites.length })}</span>}
    >
      <div className="olibv2-match-hint">{t('olibV2.match.hint')}</div>
      <div className="olibv2-unmatched-list">
        {sites.map((s) => (
          <div key={`${s.index}-${s.name}`} className="olibv2-unmatched-row">
            <span className="olibv2-unmatched-name">{s.name}</span>
            <span className="olibv2-unmatched-sector">{s.sector}</span>
            <span className="olibv2-unmatched-reason">
              {reasonLabel(s.reason, t)}
              {s.candidates?.length ? `：${s.candidates.join(' / ')}` : ''}
            </span>
          </div>
        ))}
      </div>
    </DraggableClickModal>
  )
}
