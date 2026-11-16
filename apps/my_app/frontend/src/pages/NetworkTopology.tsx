/* ============================================================================
 * OLIB FO 网络拓扑（?page=olibTopo）
 * ============================================================================
 * 页面定位：把 Orange Liberia《Orange_network_topology_20251226.pdf》的两页原图
 * 和 553 个提取站点做成一张可缩放、可搜索、可定位的参考图。**纯静态参考页**，
 * 不取接口、不写库，所以 AR_PAGE_DEFAULTS 取最松的 1800 秒（见 App.tsx）。
 *
 * 组成：
 *   · 4 张统计卡（站点 / 骨干核心 / 骨干光纤 / AML 数据中心）；
 *   · 收起的数据口径说明（CalcChainNote，默认不占首屏）；
 *   · 左：画布（物理图 + 站点点位 / 逻辑图 / 骨干示意 / 站点分布）；
 *   · 右：站点模糊查询、选中站点详情、图例。
 *   · ?maponly=1（门户 IP 页签）：只留物理图，搜索框浮在画布左上角。
 *
 * 数据与坐标口径见 networkTopology/topologyData.ts 文件头。
 * ========================================================================== */
import { useCallback, useMemo, useRef, useState } from 'react'
import { Col, Row } from 'antd'

import { useI18n } from '../i18n'
import { CardBase, StatCard } from '../components/cards'
import CalcChainNote from '../components/CalcChainNote'
import {
  AML_DC_COUNT, CORE_SITE_COUNT, SITES,
  type SiteFilter, type TopoSite, type TopoView,
} from './networkTopology/topologyData'
import TopologyViewer, { type TopologyViewerHandle } from './networkTopology/TopologyViewer'
import SiteSearch from './networkTopology/SiteSearch'
import MapSiteSearch from './networkTopology/MapSiteSearch'
import SiteDetail from './networkTopology/SiteDetail'
import TopologyLegend from './networkTopology/TopologyLegend'
import { matchSites } from './networkTopology/siteMatch'

/** 门户 GIS Map 的 IP 页签（`?maponly=1`）只留物理图，搜索框浮在画布上。 */
function readMapOnlyMode(): boolean {
  try {
    const v = new URLSearchParams(window.location.search).get('maponly')
    return v === '1' || v === 'true'
  } catch { return false }
}

function readInitialView(): TopoView {
  const v = new URLSearchParams(window.location.search).get('view')
  if (v === 'physical' || v === 'logical' || v === 'core' || v === 'scatter') return v
  return 'physical'
}

const MAPONLY_CSS = `
.olib-maponly{position:relative;height:100vh;min-height:0;display:flex;flex-direction:column;overflow:hidden}
.olib-maponly .topo-views,.olib-maponly .topo-hint{display:none!important}
.olib-maponly .topo-toolbar{flex:0 0 auto}
.olib-maponly .topo-viewer{flex:1 1 auto;height:auto!important;min-height:0}
.olib-finder{position:absolute;z-index:20;left:10px;top:52px;width:min(340px,calc(100% - 20px))}
`

export default function NetworkTopology() {
  const { t } = useI18n()
  const viewer = useRef<TopologyViewerHandle>(null)
  const [mapOnly] = useState(readMapOnlyMode)

  const [view, setView] = useState<TopoView>(() => (readMapOnlyMode() ? 'physical' : readInitialView()))
  const [selected, setSelected] = useState<TopoSite | null>(null)
  const [sitesVisible, setSitesVisible] = useState(true)
  const [query, setQuery] = useState('')
  const [sector, setSector] = useState('')
  const [filter, setFilter] = useState<SiteFilter>('all')

  const results = useMemo(
    () => matchSites(SITES, query, sector, filter),
    [query, sector, filter],
  )

  /* 搜索命中后放大到该站，方便在 553 个点里看清是哪一个 */
  const pick = useCallback((s: TopoSite) => { viewer.current?.zoomTo(s) }, [])
  const locate = useCallback((s: TopoSite) => { viewer.current?.locate(s) }, [])
  const zoomTo = useCallback((s: TopoSite) => { viewer.current?.zoomTo(s) }, [])
  const showScatter = useCallback(() => setView('scatter'), [])

  if (mapOnly) {
    return (
      <div className="olib-maponly">
        <style>{MAPONLY_CSS}</style>
        <TopologyViewer
          ref={viewer}
          view="physical"
          onViewChange={() => {}}
          sitesVisible={sitesVisible}
          onSitesVisibleChange={setSitesVisible}
          selected={selected}
          onSelect={setSelected}
        />
        <MapSiteSearch
          query={query}
          onQuery={setQuery}
          results={results}
          total={SITES.length}
          onPick={pick}
        />
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--wh-space-md)' }}>
      <Row gutter={[12, 12]}>
        <Col xs={12} lg={6}>
          <StatCard
            title={t('olibTopo.kpi.sites')}
            value={SITES.length}
            subText={t('olibTopo.kpi.sitesSub')}
            explainText={t('olibTopo.explain.sites', { n: SITES.length })}
          />
        </Col>
        <Col xs={12} lg={6}>
          <StatCard
            title={t('olibTopo.kpi.core')}
            value={CORE_SITE_COUNT}
            subText={t('olibTopo.kpi.coreSub')}
            explainText={t('olibTopo.explain.core', { n: CORE_SITE_COUNT })}
          />
        </Col>
        <Col xs={12} lg={6}>
          <StatCard
            title={t('olibTopo.kpi.backbone')}
            value="40G / 10G"
            subText={t('olibTopo.kpi.backboneSub')}
            explainText={t('olibTopo.explain.backbone')}
          />
        </Col>
        <Col xs={12} lg={6}>
          <StatCard
            title={t('olibTopo.kpi.dc')}
            value={AML_DC_COUNT}
            subText={t('olibTopo.kpi.dcSub')}
            explainText={t('olibTopo.explain.dc')}
          />
        </Col>
      </Row>

      <CalcChainNote
        title={t('olibTopo.note.title')}
        sections={[
          { title: t('olibTopo.note.sec.source'), lines: [t('olibTopo.note.source.1'), t('olibTopo.note.source.2')] },
          { title: t('olibTopo.note.sec.coords'), lines: [t('olibTopo.note.coords.1'), t('olibTopo.note.coords.2')] },
          { title: t('olibTopo.note.sec.core'), lines: [t('olibTopo.note.core.1'), t('olibTopo.note.core.2')] },
          { title: t('olibTopo.note.sec.limits'), lines: [t('olibTopo.note.limits.1')] },
        ]}
      />

      <Row gutter={[12, 12]}>
        <Col xs={24} xl={16}>
          <CardBase bodyStyle={{ padding: 0, overflow: 'hidden' }}>
            <TopologyViewer
              ref={viewer}
              view={view}
              onViewChange={setView}
              sitesVisible={sitesVisible}
              onSitesVisibleChange={setSitesVisible}
              selected={selected}
              onSelect={setSelected}
            />
          </CardBase>
        </Col>
        <Col xs={24} xl={8}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--wh-space-md)' }}>
            <SiteSearch
              query={query}
              onQuery={setQuery}
              sector={sector}
              onSector={setSector}
              filter={filter}
              onFilter={setFilter}
              results={results}
              total={SITES.length}
              selected={selected}
              onPick={pick}
            />
            <SiteDetail site={selected} onLocate={locate} onZoom={zoomTo} onScatter={showScatter} />
            <TopologyLegend />
          </div>
        </Col>
      </Row>
    </div>
  )
}