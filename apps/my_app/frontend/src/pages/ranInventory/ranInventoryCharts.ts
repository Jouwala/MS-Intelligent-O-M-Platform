/* RAN 资产台账 —— 五张分布图的 ECharts 配置。
 *
 * 单独成文件的两个原因：
 *   1. 单文件 500 行的工程约定（页面自己已有 KPI / 台账表 / 两套导出）；
 *   2. 取色分两层最容易出错 —— DOM 用 WH 令牌，ECharts 必须用 DARK/LIGHT 实值
 *      （canvas 解析不了 CSS 变量），集中在这里只需面向一处 review。
 */
import { axisLabel, axisLine, darkTooltip, splitLine } from '../../theme'
import { UNIT_LABEL, type Ledger } from './ranInventoryTypes'

type T = (key: string, params?: Record<string, string | number>) => string

const usd = (n: number) => n.toLocaleString('en-US')

/** 横向条形图（硬件构成 / 厂商份额 / 板卡型号共用同一套版式） */
function hBar(
  items: { name: string; n: number }[],
  isDark: boolean,
  color?: string,
) {
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, ...darkTooltip(isDark) },
    grid: { left: 8, right: 44, top: 8, bottom: 8, containLabel: true },
    xAxis: { type: 'value', axisLabel: axisLabel(isDark), splitLine: splitLine(isDark) },
    yAxis: {
      type: 'category',
      data: items.map(i => i.name).reverse(),
      axisLabel: { ...axisLabel(isDark), fontSize: 11 },
      axisLine: axisLine(isDark),
      axisTick: { show: false },
    },
    series: [{
      type: 'bar',
      barMaxWidth: 14,
      data: items.map(i => i.n).reverse(),
      itemStyle: { color: color || (isDark ? '#00d9ff' : '#0d9488'), borderRadius: [0, 3, 3, 0] },
      label: {
        show: true, position: 'right', fontSize: 11,
        color: isDark ? '#aebbd4' : '#3f5468',
        formatter: (p: { value: number }) => usd(p.value),
      },
    }],
  }
}

export default function buildCharts(
  data: Ledger | null,
  isDark: boolean,
  colors: string[],
  t: T,
) {
  // 网元类型：扇区上不挂标签（四个小扇区会挤在一起），数量改由图例带出
  const typeItems = (data?.by_type || []).map(b => ({ name: b.moc, value: b.ne }))
  const byName = new Map(typeItems.map(i => [i.name, i.value]))
  const type = {
    tooltip: { trigger: 'item', ...darkTooltip(isDark) },
    legend: {
      bottom: 0,
      formatter: (name: string) => `${name} ${usd(byName.get(name) || 0)}`,
      textStyle: { color: isDark ? '#aebbd4' : '#3f5468', fontSize: 11 },
    },
    series: [{
      type: 'pie',
      radius: ['48%', '72%'],
      center: ['50%', '44%'],
      itemStyle: { borderWidth: 2, borderColor: isDark ? '#08122c' : '#fff' },
      label: { show: false },
      labelLine: { show: false },
      data: typeItems,
    }],
    color: colors,
  }

  const unit = hBar(
    (data?.unit_totals || []).map(u => ({
      name: t(UNIT_LABEL[u.key] || 'ranInv.unit.pack'), n: u.n,
    })), isDark)

  const vendor = hBar(
    (data?.opt_vendors || []).map(v => ({
      // 后端把尾部厂商并成「其他」，英文界面要跟着翻，否则图例里冒出中文
      name: v.name === '其他' ? t('ranInv.other') : v.name, n: v.n,
    })), isDark, isDark ? '#0bc78c' : '#15803d')

  const year = {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, ...darkTooltip(isDark) },
    grid: { left: 8, right: 8, top: 16, bottom: 8, containLabel: true },
    xAxis: {
      type: 'category',
      data: (data?.years || []).map(y => y.year.slice(2)),
      axisLabel: { ...axisLabel(isDark), fontSize: 10 },
      axisLine: axisLine(isDark),
      axisTick: { show: false },
    },
    yAxis: { type: 'value', axisLabel: axisLabel(isDark), splitLine: splitLine(isDark) },
    series: [{
      type: 'bar',
      data: (data?.years || []).map(y => y.n),
      itemStyle: { color: isDark ? '#258cff' : '#0e7490', borderRadius: [3, 3, 0, 0] },
      barMaxWidth: 22,
    }],
  }

  const model = hBar(
    (data?.pack_models || []).map(m => ({ name: m.name, n: m.n })),
    isDark, isDark ? '#7c8ff0' : '#6366f1')

  return { type, unit, vendor, year, model }
}
