/* ==================== 计算链路说明条（默认收起） ====================
 * 各看板顶部用来说明"这个数字是怎么算出来的"：数据源 → 筛选 → 分子/分母 → 口径。
 *
 * 交互与全站既有约定一致（BmMeta.tsx 的 scopeOpen / BpReport.tsx 的 caliberOpen）：
 *   - **默认收起**，只留一行标题 + 「展开/收起」入口，不占首屏；
 *   - 点标题或右侧「展开」即展开完整说明，再点收起。
 *
 * 两种入口（2026-11-16 新增 `trigger`）：
 *   - `alert`（默认）= 既有形态：一条可展开的说明条，占一行；
 *   - `icon` = 只留一个**问号**，点开在气泡里读说明 —— 给「连这一行都不想占」的页面
 *     （如 olibTopo 参考图页：首屏全给画布，口径说明放页面最下面）。
 *   两种形态共用同一份 `sections` 与同一套渲染，不要各页自己拼 Popover。
 *
 * 与 components/MetricExplain 的区别：
 *   - MetricExplain 是**悬停气泡**（单个指标的小注解，鼠标移开就没了）；
 *   - 本组件是**可点击展开的段落**，用于承载多段、带小标题的完整链路说明
 *     （悬停气泡放不下这么多内容，且用户无法复制/对读）。
 * ================================================================ */
import { useState, type ReactNode } from 'react'
import { Alert, Button, Popover, Space } from 'antd'
import { DownOutlined, InfoCircleOutlined, QuestionCircleOutlined, UpOutlined } from '@ant-design/icons'
import { WH } from '../theme'
import { useI18n } from '../i18n'
import { renderRich } from '../utils/richText'

export interface CalcChainSection {
  /** 小节标题（如「口径」「分子」「分母」），可省略 */
  title?: string
  /** 小节正文：一行一段，直接给字符串数组即可 */
  lines: string[]
}

interface Props {
  /** 收起态的入口文案（如「NUR 计算链路说明」）；`icon` 形态下作为气泡标题与 aria-label */
  title: string
  sections: CalcChainSection[]
  /** 额外的 JSX 内容（如公式、示意表格），渲染在小节之后 */
  children?: ReactNode
  /** 右上角常驻告警（如后端 warnings / 口径不完整）——有值时置顶，不随收起隐藏 */
  warning?: ReactNode
  style?: React.CSSProperties
  /** 入口形态：alert = 一行可展开说明条（默认）；icon = 只留一个问号，点开看说明 */
  trigger?: 'alert' | 'icon'
  /** `icon` 形态下气泡的展开方向（页面底部一般向上弹，默认 topRight） */
  placement?: 'top' | 'topLeft' | 'topRight' | 'bottom' | 'bottomLeft' | 'bottomRight'
}

export default function CalcChainNote({
  title, sections, children, warning, style, trigger = 'alert', placement = 'topRight',
}: Props) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)

  /* 小节正文：两种形态共用一份，避免 `icon` 形态漂移成另一套排版 */
  const body = (
    <div style={{ fontSize: 12, lineHeight: 1.75, color: WH.textMuted }}>
      {sections.map((sec, i) => (
        <div key={i} style={{ marginTop: i === 0 ? 0 : 'var(--wh-space-sm)' }}>
          {sec.title && (
            <div style={{ color: WH.text, fontWeight: 600, marginBottom: 2 }}>{sec.title}</div>
          )}
          {sec.lines.map((ln, j) => (
            <div key={j} style={{ paddingLeft: 'var(--wh-space-sm)', whiteSpace: 'pre-line' }}>
              {renderRich(ln)}
            </div>
          ))}
        </div>
      ))}
      {children}
    </div>
  )

  if (trigger === 'icon') {
    return (
      <div style={style}>
        {warning}
        <Popover
          trigger="click"
          placement={placement}
          title={<span style={{ fontSize: 13 }}>{title}</span>}
          content={
            <div style={{ maxWidth: 'min(88vw, 30rem)', maxHeight: 'min(70vh, 30rem)', overflowY: 'auto', paddingRight: 4 }}>
              {body}
            </div>
          }
        >
          <Button
            type="text"
            size="small"
            shape="circle"
            aria-label={title}
            title={title}
            icon={<QuestionCircleOutlined style={{ fontSize: 14, color: WH.textDim }} />}
          />
        </Popover>
      </div>
    )
  }

  return (
    <div style={style}>
      {/* 常驻告警：口径异常（如"某站点找不到小区信息"）必须始终可见，
          藏进收起区等于让用户带着错误的口径看数字（沿用 BpReport 的判断）。 */}
      {warning}

      <Alert
        type="info"
        showIcon
        icon={<InfoCircleOutlined />}
        style={{ marginBottom: 'var(--wh-space-md)' }}
        message={
          <span
            onClick={() => setOpen(v => !v)}
            style={{ cursor: 'pointer', userSelect: 'none' }}
            role="button"
            aria-expanded={open}
            tabIndex={0}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(v => !v) } }}
          >
            {title}
            <a style={{ marginLeft: 8, fontSize: 12 }}>
              {open ? t('calcNote.collapse') : t('calcNote.expand')}
              {open
                ? <UpOutlined style={{ fontSize: 10, marginLeft: 4 }} />
                : <DownOutlined style={{ fontSize: 10, marginLeft: 4 }} />}
            </a>
          </span>
        }
        description={open ? (
          <div>
            {body}
            <Space style={{ marginTop: 'var(--wh-space-sm)' }}>
              <Button type="link" size="small" style={{ padding: 0 }}
                onClick={() => setOpen(false)}>
                {t('calcNote.collapse')}
              </Button>
            </Space>
          </div>
        ) : null}
      />
    </div>
  )
}
