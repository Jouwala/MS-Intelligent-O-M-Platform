/* ============================================================================
 * UME 四层拓扑（?page=ipranTopo&layer=ipran|ipcore|metro|dwdm）
 * ============================================================================
 * 数据源：UME（ZTE NetNumen）四张逻辑拓扑层，端点序列相同、只有 lid 与口径不同。
 * 8 次上游调用由后端 services/ipran_topo 聚合成一次响应并**按层**缓存 120s
 * （层清单与口径见后端 layers.py、前端 ipranTopo/layerDefs.ts）。
 * 本页只负责「怎么画得好看、点得动」：
 *
 *   布局 3 种（力导向默认 / 地理 / 分层），可读性依据见 ipranTopo/layouts.ts 顶部：
 *     力导向 42 次边交叉（默认）、地理 221 次（回答「影响哪个城市」）、分层 390 次（导出用）。
 *   着色 3 种：按层级（默认）/ 按在线状态 / 按最高告警级 —— 告警色**不是默认**：
 *     实测 80% 网元都带紧急告警，按告警级填色会全红、失去区分度（后端 warnings 也会说明）。
 *   告警只做节点右上角小红点；带宽只做线宽；断链红色虚线。颜色全部服务于故障态。
 *
 * 交互：点节点聚焦（其余变暗、只显示一跳邻居）；点边看物理链路明细；
 * 统计卡点开是「构成这个数的那些行」（TopoDrillModal），与卡上数字同一份数据。
 * 切页签会清空旧层数据与聚焦态，并用请求序号丢弃「切走后迟到的旧层响应」；
 * 自动刷新：已登记 AR_INPLACE_REFRESH_PAGES，监听 myapp-refresh 只静默重取当前层。
 * ========================================================================== */
import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { Alert, Button, Col, Empty, Row, Segmented, Space, Spin, Switch, Table } from 'antd'
import { DownloadOutlined, ReloadOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'

import { useI18n } from '../i18n'
import { WH, useTheme } from '../theme'
import { CardBase, StatCard } from '../components/cards'
import { ExplainIcon, ExplainPopover } from '../components/MetricExplain'
import CalcChainNote, { type CalcChainSection } from '../components/CalcChainNote'
import { exportReport } from '../utils/excelReport'
import { formatDateTime } from '../datetime'
import { getIpranTopoGraph } from '../api'
import TopoCanvas from './ipranTopo/TopoCanvas'
import TopoDrillModal, { type DrillKind } from './ipranTopo/TopoDrillModal'
import { buildIndex, LAYOUT_FNS } from './ipranTopo/layouts'
import { LAYER_KEYS, LAYER_LABEL_KEY, calcSectionKeys, explainKeys, isLayerKey } from './ipranTopo/layerDefs'
import type { ColorMode, LayerKey, LayoutKey, Tier, TopoEdge, TopoNode, TopoResp } from './ipranTopo/types'

type Drill = { kind: DrillKind; anchor: { x: number; y: number } }
type NbRow = { node: TopoNode; edge: TopoEdge }

export default function IpranTopo() {
  const { t } = useI18n()
  const { colors } = useTheme()

  /* 层：URL 的 &layer= 可深链到单层，非法值回落到 ipran（与后端白名单一致） */
  const [layer, setLayer] = useState<LayerKey>(() => {
    const v = new URLSearchParams(window.location.search).get('layer')
    return isLayerKey(v) ? v : 'ipran'
  })
  const [resp, setResp] = useState<TopoResp | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [layoutKey, setLayoutKey] = useState<LayoutKey>('force')
  const [colorMode, setColorMode] = useState<ColorMode>('tier')
  const [showAllLabels, setShowAllLabels] = useState(false)
  const [foldParallel, setFoldParallel] = useState(true)
  const [focus, setFocus] = useState(-1)
  const [drill, setDrill] = useState<Drill | null>(null)
  // 快速连切页签时先发的请求可能后返回，只认最后一次请求的响应
  const reqSeq = useRef(0)

  /* 取数：silent 供原地自动刷新（不清 loading、失败保留旧数据），force 绕过后端 120s 缓存 */
  const load = useCallback(async (opts?: { silent?: boolean; force?: boolean }) => {
    const seq = ++reqSeq.current
    const silent = !!opts?.silent
    if (!silent) { setLoading(true); setError('') }
    try {
      const r = await getIpranTopoGraph(layer, opts?.force ? 1 : 0)
      if (seq !== reqSeq.current) return
      setResp(r.data as TopoResp)
      if (!silent) setError('')
    } catch (e: any) {
      if (seq !== reqSeq.current) return
      setError(e?.message || String(e))
    } finally {
      if (seq === reqSeq.current && !silent) setLoading(false)
    }
  }, [layer])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    const onInplaceRefresh = () => { void load({ silent: true }) }
    document.addEventListener('myapp-refresh', onInplaceRefresh)
    return () => document.removeEventListener('myapp-refresh', onInplaceRefresh)
  }, [load])
  /* 地址栏保留当前层，便于分享单层链接；replaceState 不产生多余历史，App.tsx 的
     page 同步用 set('page')，不会丢掉 layer 参数 */
  useEffect(() => {
    const url = new URL(window.location.href)
    url.searchParams.set('layer', layer)
    window.history.replaceState(window.history.state, '', url)
  }, [layer])

  const changeLayer = (k: LayerKey) => {
    if (k === layer) return
    // 旧层数据/聚焦/下钻不能带到新层：先清空，load 因 layer 变化会自动取新层
    setLayer(k)
    setResp(null)
    setError('')
    setLoading(true)
    setFocus(-1)
    setDrill(null)
  }
  const explain = explainKeys(layer)

  const nodes = useMemo(() => resp?.nodes ?? [], [resp])
  const edges = useMemo(() => resp?.edges ?? [], [resp])
  const idx = useMemo(() => buildIndex(nodes, edges), [nodes, edges])
  const layout = useMemo(() => LAYOUT_FNS[layoutKey](nodes, idx, edges), [layoutKey, nodes, idx, edges])
  const focusSafe = focus >= 0 && focus < nodes.length ? focus : -1

  /* ---- 统计（卡片与下钻弹窗取同一份数组，口径不会对不上） ---- */
  const meta = resp?.meta
  const ne = meta?.ne ?? 0
  const ext = meta?.ext ?? 0
  const rawLinks = meta?.rawLinks ?? 0
  const folded = meta?.foldedLinks ?? 0
  const critNe = meta?.critNe ?? 0
  const critPct = ne ? Math.round((critNe * 100) / ne) : 0
  const downNe = useMemo(() => nodes.filter((v) => v.tier > 0 && !v.up).length, [nodes])

  /* 告警按页面已知的口径就地重建成中英双语；后端未知前缀（如 load_failed）原样显示，不吞 */
  const warnLines = useMemo(() => {
    if (!meta) return []
    const out: string[] = []
    if (meta.stale) out.push(t('ipranTopo.warn.stale'))
    if (ne && critNe / ne > 0.5) {
      out.push(t('ipranTopo.warn.alarmSaturated', { crit: critNe, ne, pct: critPct }))
    }
    const miss = meta.geo?.miss ?? 0
    if (miss) out.push(t('ipranTopo.warn.geoMiss', { n: miss }))
    const fuzzy = meta.geo?.fuzzy ?? 0
    if (fuzzy) out.push(t('ipranTopo.warn.geoFuzzy', { n: fuzzy }))
    for (const w of meta.warnings || []) {
      if (!/^(alarm_saturated|geo_miss|geo_fuzzy|upstream_unreachable):/.test(w)) out.push(w)
    }
    return out
  }, [meta, ne, critNe, critPct, t])

  const tierName = (tier: Tier) => {
    switch (tier) {
      case 0: return t('ipranTopo.tier.0')
      case 1: return t('ipranTopo.tier.1')
      case 2: return t('ipranTopo.tier.2')
      default: return t('ipranTopo.tier.3')
    }
  }

  const legendItems = useMemo(() => {
    const items: { c: string; label: string }[] = []
    const tierColor = [WH.textDim, colors[0], colors[1], colors[2]]
    if (colorMode === 'up') {
      items.push({ c: WH.ok, label: t('ipranTopo.legend.upOn') })
      items.push({ c: WH.danger, label: t('ipranTopo.legend.upOff') })
      items.push({ c: WH.textDim, label: tierName(0) })
    } else if (colorMode === 'sev') {
      items.push({ c: WH.sev.Critical, label: t('ipranTopo.sev.crit') })
      items.push({ c: WH.sev.Major, label: t('ipranTopo.sev.major') })
      items.push({ c: WH.sev.Warning, label: t('ipranTopo.sev.minor') })
      items.push({ c: WH.sev.Info, label: t('ipranTopo.sev.warn') })
      items.push({ c: WH.ok, label: t('ipranTopo.sev.ok') })
    } else {
      ;([0, 1, 2, 3] as Tier[]).forEach((k) => {
        const n = meta?.tierCounts?.[String(k)] ?? 0
        items.push({ c: tierColor[k], label: `${tierName(k)}（${n}）` })
      })
    }
    return items
  }, [colorMode, colors, meta, t])

  /* ---- 告警 Top 10：点网元名聚焦到画布 ---- */
  const topRows = useMemo(
    () => nodes
      .filter((v) => v.tier > 0 && (v.crit > 0 || v.major > 0))
      .sort((a, b) => b.crit - a.crit || b.major - a.major)
      .slice(0, 10),
    [nodes],
  )

  const topCols = useMemo(() => [
    {
      title: t('ipranTopo.col.name'), dataIndex: 'name', key: 'name',
      render: (v: string, r: TopoNode) => <a onClick={() => setFocus(idx.byId[r.id])}>{v}</a>,
    },
    {
      title: t('ipranTopo.col.tier'), dataIndex: 'tier', key: 'tier', width: 120,
      render: (v: Tier) => tierName(v),
    },
    { title: t('ipranTopo.col.site'), dataIndex: 'site', key: 'site', width: 180 },
    {
      title: <span>{t('ipranTopo.col.crit')} <ExplainIcon text={t('ipranTopo.critExplain')} /></span>,
      dataIndex: 'crit', key: 'crit', width: 100,
      render: (v: number, r: TopoNode) => (
        <ExplainPopover text={t('ipranTopo.critTip', {
          name: r.name, crit: r.crit, major: r.major, minor: r.minor, warn: r.warn,
        })}>
          <b style={{ color: v > 0 ? WH.sev.Critical : WH.textMuted }}>{v}</b>
        </ExplainPopover>
      ),
    },
    { title: t('ipranTopo.col.major'), dataIndex: 'major', key: 'major', width: 90 },
    { title: t('ipranTopo.col.minor'), dataIndex: 'minor', key: 'minor', width: 90 },
    { title: t('ipranTopo.col.warn'), dataIndex: 'warn', key: 'warn', width: 90 },
  ], [t, idx])

  /* ---- 聚焦节点的邻居 ---- */
  const focusNode = focusSafe >= 0 ? nodes[focusSafe] : null
  const neighbors = useMemo(() => {
    if (focusSafe < 0) return []
    return (idx.adj[focusSafe] || [])
      .map((a) => ({ node: nodes[a.n], edge: edges[a.e] }))
      .sort((x, y) => x.node.tier - y.node.tier || x.node.name.localeCompare(y.node.name))
  }, [focusSafe, idx, nodes, edges])

  const nbCols = useMemo(() => [
    {
      title: t('ipranTopo.col.peer'), key: 'peer',
      render: (_: unknown, r: NbRow) => (
        <a onClick={() => setFocus(idx.byId[r.node.id])}>{r.node.name}</a>
      ),
    },
    {
      title: t('ipranTopo.col.tier'), key: 'tier', width: 120,
      render: (_: unknown, r: NbRow) => tierName(r.node.tier),
    },
    {
      title: t('ipranTopo.col.physLinks'), key: 'n', width: 110,
      render: (_: unknown, r: NbRow) => r.edge.n,
    },
    {
      title: t('ipranTopo.col.gb'), key: 'gb', width: 100,
      render: (_: unknown, r: NbRow) => (r.edge.gb == null ? '-' : `${r.edge.gb}G`),
    },
    {
      title: t('ipranTopo.col.downLinks'), key: 'down', width: 110,
      render: (_: unknown, r: NbRow) => (r.edge.down
        ? <span style={{ color: WH.danger }}>{r.edge.down}</span> : '0'),
    },
  ], [t, idx])

  /* ---- 导出：网元 + 链路两张 sheet（统一走 excelReport） ---- */
  const doExport = useCallback(() => {
    if (!resp) return
    const nodeRows = resp.nodes.map((v) => [
      v.name, tierName(v.tier), v.site, v.model, v.ip,
      v.up ? t('ipranTopo.status.up') : t('ipranTopo.status.down'),
      v.crit, v.major, v.minor, v.warn, v.deg,
      v.lat ?? '', v.lon ?? '', v.geo,
    ])
    const linkRows = resp.edges.map((e) => [
      resp.nodes[idx.byId[e.s]]?.name ?? e.s,
      resp.nodes[idx.byId[e.t]]?.name ?? e.t,
      e.n, e.gb ?? '', e.down, e.crit, e.major, e.ports.join(' | '),
    ])
    void exportReport([
      {
        name: t('ipranTopo.export.nodes'), title: exportTitle,
        header: [
          t('ipranTopo.col.name'), t('ipranTopo.col.tier'), t('ipranTopo.col.site'), t('ipranTopo.col.model'),
          t('ipranTopo.col.ip'), t('ipranTopo.col.status'), t('ipranTopo.col.crit'), t('ipranTopo.col.major'),
          t('ipranTopo.col.minor'), t('ipranTopo.col.warn'), t('ipranTopo.col.deg'),
          t('ipranTopo.col.lat'), t('ipranTopo.col.lon'), t('ipranTopo.col.geo'),
        ],
        rows: nodeRows, numericCols: [6, 7, 8, 9, 10],
      },
      {
        name: t('ipranTopo.export.links'), title: exportTitle,
        header: [
          t('ipranTopo.col.peerA'), t('ipranTopo.col.peerB'), t('ipranTopo.col.physLinks'),
          t('ipranTopo.col.gb'), t('ipranTopo.col.downLinks'), t('ipranTopo.col.crit'),
          t('ipranTopo.col.major'), t('ipranTopo.col.ports'),
        ],
        rows: linkRows, numericCols: [2, 3, 4, 5, 6], autoWidth: false,
        columnWidths: [28, 28, 12, 12, 12, 10, 10, 60],
      },
    ], `UME_Topo_${layer}_${dayjs().format('YYYYMMDD_HHmmss')}.xlsx`)
  }, [resp, idx, t, layer])

  /* 口径说明按层切换：ipran 用实测版，其余三层用通用版（不套用 93/167 这类实测数字） */
  const calcSections = useMemo<CalcChainSection[]>(
    () => calcSectionKeys(layer).map((s) => ({
      title: t(s.title), lines: s.lines.map((k) => t(k)),
    })),
    [layer, t],
  )

  const openDrill = (kind: DrillKind) => (e: ReactMouseEvent<HTMLDivElement>) =>
    setDrill({ kind, anchor: { x: e.clientX, y: e.clientY } })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--wh-space-md)' }}>

      {/* 拓扑层页签：四层共用同一份画布 / 统计卡 / 下钻 / 导出，切层只换数据源 */}
      <CardBase bodyStyle={{ padding: 'var(--wh-space-sm) var(--wh-space-md)' }}>
        <Space wrap size={12} align="center">
          <span style={{ color: WH.textMuted, fontSize: 13 }}>{t('ipranTopo.toolbar.layer')}</span>
          <Segmented className="wh-seg" size="small" value={layer}
            onChange={(v) => changeLayer(v as LayerKey)}
            options={LAYER_KEYS.map((k) => ({ label: t(LAYER_LABEL_KEY[k]), value: k }))} />
          <span style={{ color: WH.textDim, fontSize: 12 }}>{t('ipranTopo.layer.hint')}</span>
        </Space>
      </CardBase>

      {/* ① 工具条：布局 / 着色 / 开关 / 重新取数 / 导出 */}
      <CardBase bodyStyle={{ padding: 'var(--wh-space-sm) var(--wh-space-md)' }}>
        <Space wrap size={12} align="center">
          <span style={{ color: WH.textMuted, fontSize: 13 }}>{t('ipranTopo.toolbar.layout')}</span>
          <Segmented className="wh-seg" size="small" value={layoutKey}
            onChange={(v) => setLayoutKey(v as LayoutKey)}
            options={[
              { label: t('ipranTopo.layout.force'), value: 'force' },
              { label: t('ipranTopo.layout.geo'), value: 'geo' },
              { label: t('ipranTopo.layout.tier'), value: 'tier' },
            ]} />
          <span style={{ color: WH.textMuted, fontSize: 13 }}>{t('ipranTopo.toolbar.color')}</span>
          <Segmented className="wh-seg" size="small" value={colorMode}
            onChange={(v) => setColorMode(v as ColorMode)}
            options={[
              { label: t('ipranTopo.color.tier'), value: 'tier' },
              { label: t('ipranTopo.color.up'), value: 'up' },
              { label: t('ipranTopo.color.sev'), value: 'sev' },
            ]} />
          <Switch size="small" checked={showAllLabels} onChange={setShowAllLabels}
            checkedChildren={t('ipranTopo.opt.allLabels')} unCheckedChildren={t('ipranTopo.opt.allLabels')} />
          <Switch size="small" checked={foldParallel} onChange={setFoldParallel}
            checkedChildren={t('ipranTopo.opt.fold')} unCheckedChildren={t('ipranTopo.opt.fold')} />
          <Button icon={<ReloadOutlined />} loading={loading}
            onClick={() => void load({ force: true })}>{t('ipranTopo.btn.refresh')}</Button>
          <Button icon={<DownloadOutlined />} disabled={!resp?.nodes.length}
            onClick={doExport}>{t('ipranTopo.btn.export')}</Button>
          {meta && (
            <span style={{ color: WH.textDim, fontSize: 12 }}>
              {t('ipranTopo.meta.line', {
                sec: (meta.elapsedSec ?? 0).toFixed(2),
                at: formatDateTime(meta.fetchedAt),
                cached: meta.cached ? t('ipranTopo.meta.cached') : '',
              })}
            </span>
          )}
        </Space>
      </CardBase>

      {/* ② 异常必须常驻可见：取数降级、无坐标、近似匹配、告警饱和 */}
      {error && <Alert type="error" showIcon message={error} />}
      {warnLines.length > 0 && (
        <Alert type="warning" showIcon message={
          <div>{warnLines.map((w, i) => <div key={i}>{w}</div>)}</div>
        } />
      )}

      {/* ③ 统计卡：点开是「构成这个数的那些行」 */}
      <Row gutter={[12, 12]}>
        <Col xs={24} sm={12} lg={6}>
          <StatCard title={t('ipranTopo.kpi.ne')} value={ne} accentColor={WH.teal}
            subText={t('ipranTopo.kpi.neSub', { n: ext })}
            explainText={t(explain.ne)} onClick={openDrill('ne')} />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard title={t('ipranTopo.kpi.links')} value={folded} accentColor={colors[1]}
            subText={t('ipranTopo.kpi.linksSub', { n: rawLinks })}
            explainText={t(explain.links)} onClick={openDrill('links')} />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard title={t('ipranTopo.kpi.down')} value={downNe}
            accentColor={downNe ? WH.danger : WH.ok}
            subText={t('ipranTopo.kpi.downSub')}
            explainText={t(explain.down)} onClick={openDrill('down')} />
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <StatCard title={t('ipranTopo.kpi.crit')} value={critNe}
            accentColor={WH.sev.Major}
            subText={t('ipranTopo.kpi.critSub', { pct: critPct })}
            explainText={t(explain.crit)} onClick={openDrill('crit')} />
        </Col>
      </Row>

      {/* ④ 口径说明：默认收起（异常已在 ② 常驻） */}
      <CalcChainNote title={t('ipranTopo.calc.title')} sections={calcSections} />

      {/* ⑤ 画布 */}
      <CardBase title={t('ipranTopo.canvas.title')}
        extra={<span style={{ color: WH.textDim, fontSize: 12 }}>{t('ipranTopo.canvas.hint')}</span>}>
        {loading && !resp ? <Spin /> : nodes.length === 0 ? <Empty description={t('ipranTopo.empty')} /> : (
          <>
            <TopoCanvas
              nodes={nodes} edges={edges} idx={idx} layout={layout}
              colorMode={colorMode} focus={focusSafe}
              showAllLabels={showAllLabels} foldParallel={foldParallel}
              onToggleFocus={(i) => setFocus((prev) => (prev === i ? -1 : i))}
            />
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center',
              marginTop: 'var(--wh-space-sm)', fontSize: 12, color: WH.textMuted }}>
              {legendItems.map((it, i) => (
                <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                  <span style={{ width: 10, height: 10, borderRadius: 5, background: it.c,
                    border: `1px solid ${WH.cardBorder}`, display: 'inline-block' }} />
                  {it.label}
                </span>
              ))}
              <span style={{ color: WH.textDim }}>{t('ipranTopo.legend.line')}</span>
            </div>
          </>
        )}
      </CardBase>

      {/* ⑥ 聚焦面板 + 告警 Top 10 */}
      <Row gutter={[12, 12]}>
        {focusNode && (
          <Col xs={24} lg={12}>
            <CardBase title={t('ipranTopo.focus.title', { name: focusNode.name })}
              extra={<Button size="small" type="link"
                onClick={() => setFocus(-1)}>{t('ipranTopo.focus.clear')}</Button>}>
              <div style={{ fontSize: 12, color: WH.textMuted, marginBottom: 8, lineHeight: 1.8 }}>
                <div>{t('ipranTopo.focus.info', {
                  tier: tierName(focusNode.tier), model: focusNode.model || '-', ip: focusNode.ip || '-',
                })}</div>
                <div>{t('ipranTopo.focus.alarms', {
                  crit: focusNode.crit, major: focusNode.major,
                  minor: focusNode.minor, warn: focusNode.warn,
                })}</div>
                {focusNode.tier === 3 && idx.parent[focusSafe] >= 0 && (
                  <div>{t('ipranTopo.focus.parent', { name: nodes[idx.parent[focusSafe]].name })}</div>
                )}
              </div>
              <Table rowKey={(r: NbRow) => r.node.id} size="small" columns={nbCols}
                dataSource={neighbors} pagination={false} scroll={{ y: 260 }} />
            </CardBase>
          </Col>
        )}
        <Col xs={24} lg={focusNode ? 12 : 24}>
          <CardBase title={t('ipranTopo.alarmTitle')}
            extra={<span style={{ color: WH.textDim, fontSize: 12 }}>{t('ipranTopo.alarmHint')}</span>}>
            {topRows.length
              ? <Table rowKey="id" size="small" columns={topCols} dataSource={topRows}
                  pagination={false} scroll={{ x: 760 }} />
              : <Empty description={t('ipranTopo.alarmEmpty')} />}
          </CardBase>
        </Col>
      </Row>

      <TopoDrillModal
        kind={drill?.kind ?? null}
        anchor={drill?.anchor ?? null}
        nodes={nodes} edges={edges} idx={idx}
        onClose={() => setDrill(null)}
        onFocus={(i) => setFocus(i)}
      />
    </div>
  )
}
