/* ============================================================================
 * OLIB FO 网络拓扑 —— 图例
 * ============================================================================
 * 颜色是**数据分类色**（链路类型 / 站点类型），与主题令牌无关；
 * 与 topologyData.ts 的 LINK_COLORS / SITE_COLORS 共用同一份常量，避免两处漂移。
 * ========================================================================== */
import { useI18n } from '../../i18n'
import { WH } from '../../theme'
import { CardBase } from '../../components/cards'
import { LINK_COLORS, SITE_COLORS, type CoreLinkType } from './topologyData'

const LINKS: { type: CoreLinkType; key: string }[] = [
  { type: 'exist', key: 'olibTopo.legend.link.exist' },
  { type: 'rail', key: 'olibTopo.legend.link.rail' },
  { type: 'p25a', key: 'olibTopo.legend.link.p25a' },
  { type: 'dwdm', key: 'olibTopo.legend.link.dwdm' },
]

const SITES: { color: string; key: string; round: boolean }[] = [
  { color: SITE_COLORS.normal, key: 'olibTopo.legend.site.normal', round: true },
  { color: SITE_COLORS.fiveG, key: 'olibTopo.legend.site.5g', round: true },
  { color: SITE_COLORS.isp, key: 'olibTopo.legend.site.isp', round: true },
  { color: SITE_COLORS.core, key: 'olibTopo.legend.site.core', round: true },
]

export default function TopologyLegend() {
  const { t } = useI18n()
  return (
    <CardBase title={t('olibTopo.legend.title')} bodyStyle={{ padding: 'var(--wh-space-sm) var(--wh-space-md)' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {LINKS.map(l => (
          <div key={l.type} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: WH.textMuted }}>
            <span style={{
              width: 26, flex: 'none',
              borderTop: `4px ${l.type === 'dwdm' ? 'dashed' : 'solid'} ${LINK_COLORS[l.type]}`,
            }} />
            {t(l.key)}
          </div>
        ))}
        {SITES.map(s => (
          <div key={s.key} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: WH.textMuted }}>
            <span style={{
              width: 11, height: 11, flex: 'none', marginLeft: 7, marginRight: 7,
              borderRadius: s.round ? '50%' : 2, background: s.color,
            }} />
            {t(s.key)}
          </div>
        ))}
      </div>
      <div style={{ fontSize: 11, color: WH.textDim, marginTop: 'var(--wh-space-sm)', lineHeight: 1.6 }}>
        {t('olibTopo.legend.footnote')}
      </div>
    </CardBase>
  )
}