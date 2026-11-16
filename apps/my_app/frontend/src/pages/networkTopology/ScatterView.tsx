/* ============================================================================
 * OLIB FO 网络拓扑 —— 站点分布散点图
 * ============================================================================
 * 每个点 = 第 1 页提取名单里的一个站点；X 用原图宽度千分比，Y 压缩到 863/1000
 * （与参考页一致），颜色按区域分类，骨干核心用黑色实心点。
 * ========================================================================== */
import { useI18n } from '../../i18n'
import { SECTOR_COLORS, SITES, type TopoSite } from './topologyData'

export default function ScatterView({
  selectedIndex, onSelect, width = 1000, height = 863,
}: {
  selectedIndex: number
  onSelect: (s: TopoSite) => void
  /** 显示尺寸（viewer 按缩放倍率传入，矢量重绘保持清晰） */
  width?: number
  height?: number
}) {
  const { t } = useI18n()
  return (
    <svg
      width={width}
      height={height}
      viewBox="0 0 1000 863"
      style={{ background: '#fff', display: 'block' }}
      aria-label={t('olibTopo.view.scatter')}
    >
      {SITES.map((s, i) => (
        <circle
          key={s.name}
          className="topo-site"
          cx={s.x}
          cy={s.y * 0.863}
          r={i === selectedIndex ? 6 : (s.core ? 5 : 3)}
          fill={s.core ? '#111' : (SECTOR_COLORS[s.sector] || '#888')}
          opacity={0.85}
          style={i === selectedIndex ? { stroke: '#ff7900', strokeWidth: 2.4 } : undefined}
          onClick={e => { e.stopPropagation(); onSelect(s) }}
        >
          <title>{s.name}</title>
        </circle>
      ))}
    </svg>
  )
}