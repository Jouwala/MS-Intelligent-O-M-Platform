/* ============================================================================
 * OLIB GIS 拓扑 —— 右侧站点详情
 * ============================================================================
 * 选中站点后显示：PDF 侧信息（区域/类型）+ GPS 匹配结果（主数据站点、县、
 * 匹配方式、核验状态）+ 与该站相连的骨干链路。
 *
 * 「未定位」是**数据异常**，按项目约定常驻可见（Alert warning），不折叠；
 * 只有解释「怎么匹配的」的口径说明才收起（页面底部 CalcChainNote）。
 * ========================================================================== */
import { Alert, Button } from 'antd'
import { AimOutlined, CloseOutlined, ZoomInOutlined } from '@ant-design/icons'
import { useI18n } from '../../i18n'
import { CardBase } from '../../components/cards'
import { LINK_COLORS } from '../networkTopology/topologyData'
import { siteKind } from './geoData'
import { methodLabel, reasonLabel, siteTypeLabel } from './labels'
import type { GeoLink, GeoSite } from './types'

export default function V2SiteDetail({
  site, links, onLocate, onZoom, onSelect, onClear,
}: {
  site: GeoSite | null
  links: GeoLink[]
  onLocate: (s: GeoSite) => void
  onZoom: (s: GeoSite) => void
  onSelect: (index: number) => void
  onClear: () => void
}) {
  const { t } = useI18n()

  if (!site) {
    return (
      <CardBase title={t('olibV2.detail.title')}>
        <div className="olibv2-empty">{t('olibV2.detail.empty')}</div>
      </CardBase>
    )
  }

  const kind = siteKind(site)
  const typeLabel = siteTypeLabel(site, t)

  return (
    <CardBase
      title={t('olibV2.detail.title')}
      extra={(
        <Button
          size="small"
          type="text"
          aria-label={t('olibV2.detail.close')}
          icon={<CloseOutlined />}
          onClick={onClear}
        />
      )}
    >
      <div className="olibv2-detail-head">
        <span className="olibv2-detail-name" title={site.name}>{site.name}</span>
        <span className={`olibv2-kind-dot is-${kind}`} aria-hidden="true" />
      </div>

      <div className="olibv2-kv"><span>{t('olibV2.detail.type')}</span><b>{typeLabel}</b></div>
      <div className="olibv2-kv"><span>{t('olibV2.detail.region')}</span><b>{site.sector || '—'}</b></div>
      <div className="olibv2-kv">
        <span>{t('olibV2.detail.pdfPos')}</span>
        <b>{`${(site.x / 10).toFixed(1)}% × ${(site.y / 10).toFixed(1)}%`}</b>
      </div>

      {site.located ? (
        <>
          <div className="olibv2-kv">
            <span>{t('olibV2.detail.gps')}</span>
            <b className="olibv2-mono">{site.lat?.toFixed(5)}, {site.lng?.toFixed(5)}</b>
          </div>
          <div className="olibv2-kv">
            <span>{t('olibV2.detail.county')}</span>
            <b>{[site.county, site.region].filter(Boolean).join(' · ') || '—'}</b>
          </div>
          <div className="olibv2-kv">
            <span>{t('olibV2.detail.gisSite')}</span>
            <b title={site.gisName}>{site.gisName || '—'}{site.siteId ? ` (${site.siteId})` : ''}</b>
          </div>
          <div className="olibv2-kv">
            <span>{t('olibV2.detail.match')}</span>
            <b>
              {methodLabel(site.method, t)}
              <span className={`olibv2-verified${site.verified ? ' is-ok' : ''}`}>
                {site.verified ? t('olibV2.detail.verified') : t('olibV2.detail.unverified')}
              </span>
            </b>
          </div>
          {site.geoNote && <div className="olibv2-detail-note">{site.geoNote}</div>}
        </>
      ) : (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 8 }}
          message={t('olibV2.detail.noGpsTitle')}
          description={(
            <>
              {reasonLabel(site.reason, t)}
              {site.candidates?.length ? (
                <div className="olibv2-cands">{t('olibV2.detail.candidates')}: {site.candidates.join(' / ')}</div>
              ) : null}
            </>
          )}
        />
      )}

      <div className="olibv2-detail-actions">
        <Button size="small" type="primary" icon={<AimOutlined />} disabled={!site.located}
          onClick={() => onLocate(site)}>
          {t('olibV2.detail.locate')}
        </Button>
        <Button size="small" icon={<ZoomInOutlined />} disabled={!site.located}
          onClick={() => onZoom(site)}>
          {t('olibV2.detail.zoom')}
        </Button>
      </div>

      <div className="olibv2-sec-title">{t('olibV2.detail.links', { n: links.length })}</div>
      {links.length === 0 ? (
        <div className="olibv2-empty is-tight">{t('olibV2.detail.linksEmpty')}</div>
      ) : (
        <div className="olibv2-link-list">
          {links.map((l) => {
            const other = l.a.index === site.index ? l.b : l.a
            return (
              <button key={l.id} type="button" className="olibv2-link-row"
                onClick={() => onSelect(other.index)}>
                <span className="olibv2-link-line" style={{ borderColor: LINK_COLORS[l.type] }} />
                <span className="olibv2-link-name">{other.name}</span>
                <span className="olibv2-link-type">{t(linkTypeKey(l.type))}</span>
              </button>
            )
          })}
        </div>
      )}
    </CardBase>
  )
}

/** 链路类型 → 字面量 i18n 键（禁止动态拼 key）。 */
function linkTypeKey(type: GeoLink['type']): string {
  switch (type) {
    case 'dwdm': return 'olibV2.legend.link.dwdm'
    case 'rail': return 'olibV2.legend.link.rail'
    case 'p25a': return 'olibV2.legend.link.p25a'
    default: return 'olibV2.legend.link.exist'
  }
}
