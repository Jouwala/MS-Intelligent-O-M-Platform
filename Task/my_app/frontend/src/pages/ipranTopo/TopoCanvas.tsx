/* ============================================================================
 * IP RAN 拓扑画布（SVG）
 * ============================================================================
 * 只负责「把算好的坐标画出来 + 悬停/点击交互」，布局算法在 ./layouts.ts，
 * 取数与口径在后端。这样布局能单独扫参数、单独回归，画布不用跟着改。
 *
 * 视觉约定（有实测依据，改之前先看 layouts.ts 顶部）：
 *   - 带宽只体现在**线宽**（100/25/10/其它四档），不用颜色 —— 颜色要留给故障态
 *   - 断链 = 红色虚线；有告警 = 橙色线；其余 = 中性灰且更淡（让故障自己跳出来）
 *   - critical 告警用**右下角小红点**而不是节点填色：80% 的网元都有 critical，
 *     填色等于全红（实测依据见后端 ipran_topo_service.py）
 *   - ×N 表示这对机框折叠了几条物理链路；「展开平行链路」时按扇面错开画
 * ========================================================================== */
import { useMemo, useState } from 'react'
import { useI18n } from '../../i18n'
import { WH, useTheme } from '../../theme'
import type { ColorMode, LayoutResult, Pt, Sev, TopoEdge, TopoIndex, TopoNode } from './types'
import { radiusOf, widthOf } from './layouts'

interface Props {
  nodes: TopoNode[]
  edges: TopoEdge[]
  idx: TopoIndex
  layout: LayoutResult
  colorMode: ColorMode
  focus: number
  showAllLabels: boolean
  foldParallel: boolean
  onToggleFocus: (i: number) => void
}

const W = 760
const H = 540

interface TipState {
  x: number
  y: number
  node?: number
  edge?: number
}

const SEV_VAR: Record<Sev, string> = {
  crit: WH.sev.Critical,
  major: WH.sev.Major,
  minor: WH.sev.Warning,
  warn: WH.sev.Info,
  ok: WH.ok,
}

export default function TopoCanvas({
  nodes, edges, idx, layout, colorMode, focus, showAllLabels, foldParallel, onToggleFocus,
}: Props) {
  const { t } = useI18n()
  const { colors } = useTheme()
  const [tip, setTip] = useState<TipState | null>(null)
  const { pos, bands } = layout

  const tierColors = useMemo(
    () => [WH.textDim, colors[0], colors[1], colors[2]],
    [colors],
  )

  const nbSet = useMemo(() => {
    if (focus < 0) return null
    const s: Record<number, 1> = { [focus]: 1 }
    idx.adj[focus]?.forEach((a) => { s[a.n] = 1 })
    return s
  }, [focus, idx])

  const colorOf = (v: TopoNode) => {
    if (v.tier === 0) return WH.textDim
    if (colorMode === 'up') return v.up ? WH.ok : WH.danger
    if (colorMode === 'sev') return SEV_VAR[v.sev]
    return tierColors[v.tier]
  }

  // 显式 switch 而非拼字符串：动态 key 会被 check:i18n 当成 `ipranTopo.tier.` 报缺键
  const tierName = (tier: TopoNode['tier']) => {
    switch (tier) {
      case 0: return t('ipranTopo.tier.0')
      case 1: return t('ipranTopo.tier.1')
      case 2: return t('ipranTopo.tier.2')
      default: return t('ipranTopo.tier.3')
    }
  }

  const showTip = (e: React.MouseEvent, patch: Omit<TipState, 'x' | 'y'>) => {
    const box = ((e.currentTarget as SVGElement).ownerSVGElement as SVGSVGElement).getBoundingClientRect()
    setTip({ x: e.clientX - box.left, y: e.clientY - box.top, ...patch })
  }

  const tipNode = tip?.node != null ? nodes[tip.node] : null
  const tipEdge = tip?.edge != null ? edges[tip.edge] : null

  return (
    <div style={{ position: 'relative', width: '100%' }}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }}
        onClick={() => { if (focus >= 0) onToggleFocus(-1) }}>
        {/* 分带标注 */}
        <g>
          {bands.map((b) => (
            <g key={`${b.labelKey}-${b.y}`}>
              <text x={12} y={b.y - 14} fontSize={11} fill={WH.textMuted}>
                {t(b.labelKey, b.count != null ? { n: b.count } : undefined)}
                {b.rows ? ` · ${t('ipranTopo.band.rows', { n: b.rows })}` : ''}
              </text>
              <line x1={12} y1={b.y - 8} x2={W - 12} y2={b.y - 8}
                stroke={WH.textDim} strokeOpacity={0.35} strokeWidth={1} />
            </g>
          ))}
        </g>

        {/* 边：先画真实曲线，再叠一条透明宽路径接住鼠标 */}
        <g>
          {edges.map((e, i) => {
            const a = pos[idx.byId[e.s]]
            const b = pos[idx.byId[e.t]]
            if (!a || !b) return null
            const copies = !foldParallel && e.n > 1 ? Math.min(e.n, 6) : 1
            const active = !nbSet || (nbSet[idx.byId[e.s]] && nbSet[idx.byId[e.t]])
            const stroke = e.down ? WH.danger : e.crit ? WH.sev.Major : WH.textDim
            const list = []
            for (let m = 0; m < copies; m++) {
              const off = copies > 1 ? (m - (copies - 1) / 2) * 5 : 0
              const mx = (a.x + b.x) / 2
              const my = (a.y + b.y) / 2
              const dx = b.x - a.x
              const dy = b.y - a.y
              const len = Math.sqrt(dx * dx + dy * dy) || 1
              const nx = (-dy / len) * off
              const ny = (dx / len) * off
              const d = `M${a.x},${a.y} Q${mx + nx},${my + ny} ${b.x},${b.y}`
              list.push(
                <path key={`e${i}-${m}`} d={d} fill="none" stroke={stroke}
                  strokeWidth={widthOf(e.gb)}
                  strokeOpacity={active ? (e.down || e.crit ? 0.95 : 0.4) : 0.07}
                  strokeDasharray={e.down ? '4 3' : undefined} />,
              )
            }
            const mx = (a.x + b.x) / 2
            const my = (a.y + b.y) / 2
            list.push(
              <path key={`hit${i}`} d={`M${a.x},${a.y} Q${mx},${my} ${b.x},${b.y}`} fill="none"
                stroke="transparent" strokeWidth={9} style={{ cursor: 'pointer' }}
                onMouseEnter={(ev) => showTip(ev, { edge: i })}
                onMouseMove={(ev) => showTip(ev, { edge: i })}
                onMouseLeave={() => setTip(null)}
                onClick={(ev) => ev.stopPropagation()} />,
            )
            if (foldParallel && e.n > 1 && active) {
              list.push(
                <text key={`n${i}`} x={mx} y={my - 2} fontSize={10} textAnchor="middle"
                  fill={WH.textMuted} pointerEvents="none">×{e.n}</text>,
              )
            }
            return <g key={i}>{list}</g>
          })}
        </g>

        {/* 节点 */}
        <g>
          {nodes.map((v, i) => {
            const p: Pt = pos[i]
            const r = radiusOf(v.tier)
            const dim = nbSet && !nbSet[i] ? 0.16 : 1
            const up = v.up === 1
            return (
              <g key={v.id} opacity={dim} style={{ cursor: 'pointer' }}
                onMouseEnter={(ev) => showTip(ev, { node: i })}
                onMouseMove={(ev) => showTip(ev, { node: i })}
                onMouseLeave={() => setTip(null)}
                onClick={(ev) => { ev.stopPropagation(); onToggleFocus(i) }}>
                {v.tier === 0
                  ? <rect x={p.x - r} y={p.y - r} width={r * 2} height={r * 2} rx={1}
                      fill={colorOf(v)} stroke={WH.textDim} strokeWidth={1} />
                  : <circle cx={p.x} cy={p.y} r={r} fill={colorOf(v)}
                      stroke={up ? WH.textDim : WH.danger}
                      strokeWidth={!up ? 2.5 : 1} />}
                {i === focus && (
                  <circle cx={p.x} cy={p.y} r={r + 4} fill="none"
                    stroke={WH.teal} strokeWidth={2} />
                )}
                {v.tier > 0 && v.crit > 0 && colorMode !== 'sev' && (
                  <circle cx={p.x + r * 0.95} cy={p.y - r * 0.95} r={2.6}
                    fill={WH.sev.Critical} stroke={WH.cardBg} strokeWidth={0.8} />
                )}
                {/* 透明大圆：小节点也有可点的命中区 */}
                <circle cx={p.x} cy={p.y} r={Math.max(r + 5, 9)} fill="transparent" />
              </g>
            )
          })}
        </g>

        {/* 标签：核心/汇聚常显；接入按「显示全部标签」开关 */}
        <g pointerEvents="none">
          {nodes.map((v, i) => {
            const p = pos[i]
            const r = radiusOf(v.tier)
            let want = v.tier <= 2 || showAllLabels
            if (nbSet && !nbSet[i]) want = want && v.tier <= 1
            if (!want) return null
            const short0 = v.tier === 3 ? v.site : v.name
            const short = short0.length > 20 ? `${short0.slice(0, 19)}…` : short0
            return (
              <text key={v.id} x={p.x} y={p.y - r - 4} fontSize={11} textAnchor="middle"
                fill={WH.textMuted} opacity={nbSet && !nbSet[i] ? 0.16 : 1}>{short}</text>
            )
          })}
        </g>
      </svg>

      {/* 悬停明细 */}
      {tip && (tipNode || tipEdge) && (
        <div style={{
          position: 'absolute',
          left: Math.min(tip.x + 14, 520),
          top: Math.max(8, tip.y - 10),
          pointerEvents: 'none', zIndex: 5,
          background: WH.cardBg, border: WH.cardBorder, borderRadius: 'var(--wh-radius-sm)',
          padding: 'var(--wh-space-sm) var(--wh-space-md)', fontSize: 12,
          color: WH.text, boxShadow: 'var(--wh-card-shadow)', maxWidth: 380, lineHeight: 1.7,
        }}>
          {tipNode && (
            <>
              <div style={{ fontWeight: 600 }}>{tipNode.name}</div>
              <div style={{ color: WH.textMuted }}>
                {tierName(tipNode.tier)} · {tipNode.model || '-'}
              </div>
              {tipNode.ip && <div style={{ color: WH.textMuted }}>IP {tipNode.ip}</div>}
              <div style={{ color: WH.textMuted }}>
                {t('ipranTopo.tip.links', { n: tipNode.deg, k: idx.adj[tip.node as number].length })}
              </div>
              <div style={{ color: tipNode.up ? WH.ok : WH.danger }}>
                {tipNode.up ? t('ipranTopo.tip.up') : t('ipranTopo.tip.down')}
              </div>
              {tipNode.tier > 0 && (
                <div style={{ color: WH.textMuted }}>
                  {t('ipranTopo.tip.alarms', {
                    crit: tipNode.crit, major: tipNode.major,
                    minor: tipNode.minor, warn: tipNode.warn,
                  })}
                </div>
              )}
              {tipNode.tier === 3 && idx.parent[tip.node as number] >= 0 && (
                <div style={{ color: WH.textMuted }}>
                  {t('ipranTopo.tip.parent', { name: nodes[idx.parent[tip.node as number]].name })}
                </div>
              )}
              {tipNode.geo === 'miss' && (
                <div style={{ color: WH.warn }}>{t('ipranTopo.tip.noGeo')}</div>
              )}
              {tipNode.geo === 'fuzzy' && (
                <div style={{ color: WH.warn }}>
                  {t('ipranTopo.tip.fuzzyGeo', { name: tipNode.geoName })}
                </div>
              )}
            </>
          )}
          {tipEdge && (
            <>
              <div style={{ fontWeight: 600 }}>
                {nodes[idx.byId[tipEdge.s]].name} ↔ {nodes[idx.byId[tipEdge.t]].name}
              </div>
              <div style={{ color: WH.textMuted }}>
                {t('ipranTopo.tip.edgeMeta', { n: tipEdge.n, gb: tipEdge.gb ?? '-' })}
              </div>
              {tipEdge.down > 0 && (
                <div style={{ color: WH.danger }}>{t('ipranTopo.tip.edgeDown', { n: tipEdge.down })}</div>
              )}
              {(tipEdge.crit > 0 || tipEdge.major > 0) && (
                <div style={{ color: WH.sev.Major }}>
                  {t('ipranTopo.tip.edgeAlarms', { crit: tipEdge.crit, major: tipEdge.major })}
                </div>
              )}
              <div style={{ color: WH.textDim, fontSize: 11, whiteSpace: 'pre-line' }}>
                {tipEdge.ports.slice(0, 4).join('\n')}
                {tipEdge.ports.length > 4 ? `\n… ${t('ipranTopo.tip.more', { n: tipEdge.ports.length - 4 })}` : ''}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
