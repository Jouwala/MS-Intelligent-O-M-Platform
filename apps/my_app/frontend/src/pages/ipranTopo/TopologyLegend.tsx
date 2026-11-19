/* 图例。着色模式切换时跟着变，线宽/三态链路的说明常驻一行。 */
import { useMemo } from 'react'

import { WH, useTheme } from '../../theme'
import { useI18n } from '../../i18n'
import type { ColorMode, Tier } from './types'
import { TRAFFIC_BINS, TRAFFIC_NA_COLOR } from './trafficScale'

interface Props {
  colorMode: ColorMode
  tierCounts?: Record<string, number>
}

export default function TopologyLegend({ colorMode, tierCounts }: Props) {
  const { t } = useI18n()
  const { colors } = useTheme()
  const tierName = (tier: Tier) => {
    switch (tier) {
      case 0: return t('ipranTopo.tier.0')
      case 1: return t('ipranTopo.tier.1')
      case 2: return t('ipranTopo.tier.2')
      default: return t('ipranTopo.tier.3')
    }
  }
  const items = useMemo(() => {
    const out: { c: string; label: string }[] = []
    const tierColor = [WH.textDim, colors[0], colors[1], colors[2]]
    if (colorMode === 'up') {
      out.push({ c: WH.ok, label: t('ipranTopo.legend.upOn') })
      out.push({ c: WH.danger, label: t('ipranTopo.legend.upOff') })
      out.push({ c: WH.textDim, label: tierName(0) })
    } else if (colorMode === 'sev') {
      out.push({ c: WH.sev.Critical, label: t('ipranTopo.sev.crit') })
      out.push({ c: WH.sev.Major, label: t('ipranTopo.sev.major') })
      out.push({ c: WH.sev.Warning, label: t('ipranTopo.sev.minor') })
      out.push({ c: WH.sev.Info, label: t('ipranTopo.sev.warn') })
      out.push({ c: WH.ok, label: t('ipranTopo.sev.ok') })
    } else {
      ;([0, 1, 2, 3] as Tier[]).forEach((k) => {
        const n = tierCounts?.[String(k)] ?? 0
        out.push({ c: tierColor[k], label: `${tierName(k)}（${n}）` })
      })
    }
    return out
  }, [colorMode, colors, tierCounts, t])

  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center',
      marginTop: 'var(--wh-space-sm)', fontSize: 12, color: WH.textMuted }}>
      {colorMode === 'traffic' ? (
        <>
          <span style={{ color: WH.text, fontWeight: 600 }}>{t('ipranTopo.legend.traffic')}</span>
          {TRAFFIC_BINS.map((b) => (
            <span key={b.label} style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
              <span style={{ width: 14, height: 9, background: b.color,
                border: `1px solid ${WH.cardBorder}`, display: 'inline-block' }} />
              {b.label}
            </span>
          ))}
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
            <span style={{ width: 14, height: 0, borderTop: `2px dashed ${TRAFFIC_NA_COLOR}`,
              display: 'inline-block' }} />
            {t('ipranTopo.legend.trafficNa')}
          </span>
          <span style={{ color: WH.textDim }}>{t('ipranTopo.legend.trafficDir')}</span>
        </>
      ) : items.map((it, i) => (
        <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 10, height: 10, borderRadius: 5, background: it.c,
            border: `1px solid ${WH.cardBorder}`, display: 'inline-block' }} />
          {it.label}
        </span>
      ))}
      {colorMode !== 'traffic' && (
        <span style={{ color: WH.textDim }}>{t('ipranTopo.legend.line')}</span>
      )}
    </div>
  )
}
