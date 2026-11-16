/* ============================================================================
 * OLIB GIS 拓扑 —— 图例
 * ============================================================================
 * 站点类型、链路类型、以及本页最重要的两条限制说明：
 *   · 链路是连接关系示意直线，不是光缆实际走向；
 *   · 端点没有 GPS 的骨干链路不绘制（显示跳过了多少条）。
 * ========================================================================== */
import { useI18n } from '../../i18n'
import { CardBase } from '../../components/cards'
import { LINK_COLORS, type CoreLinkType } from '../networkTopology/topologyData'

const LINK_TYPES: { type: CoreLinkType; key: string; dash?: string }[] = [
  { type: 'exist', key: 'olibV2.legend.link.exist' },
  { type: 'dwdm', key: 'olibV2.legend.link.dwdm', dash: '7 5' },
  { type: 'rail', key: 'olibV2.legend.link.rail', dash: '3 5' },
  { type: 'p25a', key: 'olibV2.legend.link.p25a', dash: '10 4 3 4' },
]

export default function V2Legend({
  drawnSites, totalSites, drawnLinks, skippedLinks,
}: {
  drawnSites: number
  totalSites: number
  drawnLinks: number
  skippedLinks: number
}) {
  const { t } = useI18n()

  return (
    <CardBase title={t('olibV2.legend.title')}>
      <div className="olibv2-legend-sec">{t('olibV2.legend.siteSection')}</div>
      <div className="olibv2-legend-grid">
        <span className="olibv2-legend-item"><i className="olibv2-kind-dot is-core" />{t('olibV2.type.core')}</span>
        <span className="olibv2-legend-item"><i className="olibv2-kind-dot is-isp" />{t('olibV2.type.isp')}</span>
        <span className="olibv2-legend-item"><i className="olibv2-kind-dot is-5g" />{t('olibV2.type.5g')}</span>
        <span className="olibv2-legend-item"><i className="olibv2-kind-dot is-normal" />{t('olibV2.type.normal')}</span>
      </div>

      <div className="olibv2-legend-sec">{t('olibV2.legend.linkSection')}</div>
      <div className="olibv2-legend-links">
        {LINK_TYPES.map((l) => (
          <span key={l.type} className="olibv2-legend-item">
            <i
              className="olibv2-legend-line"
              style={{ borderTopColor: LINK_COLORS[l.type], borderTopStyle: l.dash ? 'dashed' : 'solid' }}
            />
            {t(l.key)}
          </span>
        ))}
      </div>

      <div className="olibv2-legend-stats">
        {t('olibV2.legend.stats', { sites: drawnSites, total: totalSites, links: drawnLinks })}
      </div>
      <div className="olibv2-legend-note">{t('olibV2.legend.note')}</div>
      {skippedLinks > 0 && (
        <div className="olibv2-legend-warn">{t('olibV2.legend.skipped', { n: skippedLinks })}</div>
      )}
    </CardBase>
  )
}
