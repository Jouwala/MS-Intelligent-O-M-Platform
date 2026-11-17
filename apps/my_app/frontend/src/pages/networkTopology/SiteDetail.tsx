/* ============================================================================
 * OLIB FO 网络拓扑 —— 选中站点详情
 * ============================================================================
 * 两项操作与参考页一致：全图定位 / 放大看周边。
 * （2026-11-16 去掉「在分布图上查看」—— 站点分布视图已按用户要求移除。）
 * 坐标是原图相对位置（近似），不是经纬度 —— 文案里明确写出来。
 * ========================================================================== */
import { Button, Empty, Space } from 'antd'
import { AimOutlined, ExpandOutlined } from '@ant-design/icons'

import { useI18n } from '../../i18n'
import { WH } from '../../theme'
import { CardBase } from '../../components/cards'
import { sitePercent, type TopoSite } from './topologyData'
import { SiteTags } from './SiteSearch'

interface Props {
  site: TopoSite | null
  onLocate: (s: TopoSite) => void
  onZoom: (s: TopoSite) => void
}

export default function SiteDetail({ site, onLocate, onZoom }: Props) {
  const { t } = useI18n()

  const typeKey = site
    ? (site.core ? 'olibTopo.detail.type.core' : site.isp ? 'olibTopo.detail.type.isp' : 'olibTopo.detail.type.access')
    : ''

  return (
    <CardBase title={t('olibTopo.detail.title')} bodyStyle={{ padding: 'var(--wh-space-sm) var(--wh-space-md)' }}>
      {!site ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('olibTopo.detail.empty')} />
      ) : (
        <>
          <div style={{ fontSize: 16, fontWeight: 700, color: WH.text, lineHeight: 1.4 }}>
            {site.name}
            <SiteTags site={site} />
          </div>
          <div style={{ fontSize: 13, color: WH.textMuted, lineHeight: 1.9, marginTop: 'var(--wh-space-xs)' }}>
            <div>{t('olibTopo.detail.region')}：<b style={{ color: WH.text }}>{site.sector}</b></div>
            <div>
              {t('olibTopo.detail.position')}：
              <b style={{ color: WH.text }}>
                {t('olibTopo.detail.positionValue', {
                  x: sitePercent(site.x).toFixed(1), y: sitePercent(site.y).toFixed(1),
                })}
              </b>
            </div>
            <div>
              {t('olibTopo.detail.type')}：<b style={{ color: WH.text }}>{t(typeKey)}</b>
              {site.is5g && <span style={{ color: WH.textDim }}> · {t('olibTopo.detail.fiveG')}</span>}
            </div>
          </div>
          <Space wrap size={6} style={{ marginTop: 'var(--wh-space-sm)' }}>
            <Button size="small" type="primary" icon={<AimOutlined />} onClick={() => onLocate(site)}>
              {t('olibTopo.detail.locate')}
            </Button>
            <Button size="small" icon={<ExpandOutlined />} onClick={() => onZoom(site)}>
              {t('olibTopo.detail.zoom')}
            </Button>
          </Space>
          <div style={{ fontSize: 11, color: WH.textDim, marginTop: 'var(--wh-space-xs)', lineHeight: 1.6 }}>
            {t('olibTopo.detail.note')}
          </div>
        </>
      )}
    </CardBase>
  )
}
