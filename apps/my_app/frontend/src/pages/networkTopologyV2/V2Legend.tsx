/* ============================================================================
 * OLIB GIS 拓扑 —— 图例
 * ============================================================================
 * 三块：站点类型、骨干链路类型、PDF 第 1 页数字化出来的链路。
 * 后两块都带一条限制说明：骨干是「站到站关系」，PDF 链路是「原图折线的示意投影」——
 * 两者都不是光缆实际走向。数字（已绘制/跳过）由页面传入，图例不自己算。
 * ========================================================================== */
import { useI18n } from '../../i18n'
import { CardBase } from '../../components/cards'
import { LINK_COLORS, type CoreLinkType } from '../networkTopology/topologyData'
import { FIBER_LEGEND, FIBER_STYLES } from './fiberMeta'

const LINK_TYPES: { type: CoreLinkType; key: string; dash?: string }[] = [
  { type: 'exist', key: 'olibV2.legend.link.exist' },
  { type: 'dwdm', key: 'olibV2.legend.link.dwdm', dash: '7 5' },
  { type: 'rail', key: 'olibV2.legend.link.rail', dash: '3 5' },
  { type: 'p25a', key: 'olibV2.legend.link.p25a', dash: '10 4 3 4' },
]

export default function V2Legend({
  drawnSites, totalSites, drawnLinks, skippedLinks,
  fiberLinks, fiberSites, fiberError,
}: {
  drawnSites: number
  totalSites: number
  drawnLinks: number
  skippedLinks: number
  /** 当前画在地图上的 PDF 链路条数（关闭图层时为 0） */
  fiberLinks: number
  /** 这些 PDF 链路涉及多少个已定位站点 */
  fiberSites: number
  fiberError: string | null
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

      <div className="olibv2-legend-sec">{t('olibV2.fiber.section')}</div>
      <div className="olibv2-legend-links">
        {FIBER_LEGEND.map((l) => {
          const style = FIBER_STYLES[l.group]
          return (
            <span key={l.group} className="olibv2-legend-item">
              <i
                className="olibv2-legend-line"
                style={{
                  borderTopColor: style.color,
                  borderTopStyle: style.dash ? 'dashed' : 'solid',
                  borderTopWidth: l.group === 'base' ? 1 : 2,
                  opacity: l.group === 'base' ? 0.7 : 1,
                }}
              />
              {t(l.key)}
            </span>
          )
        })}
      </div>
      <div className="olibv2-legend-stats">
        {t('olibV2.fiber.stats', { n: fiberLinks, sites: fiberSites })}
      </div>
      <div className="olibv2-legend-note">{t('olibV2.fiber.note')}</div>
      {fiberError && <div className="olibv2-legend-warn">{t('olibV2.fiber.error')}</div>}
    </CardBase>
  )
}
