/* RAN 资产台账 —— 网元明细（下钻弹窗内容）。
 *
 * 拆出本文件的原因：页面本身已有 KPI / 5 张图 / 台账表 / 两套导出，
 * 把「一屏三表 + 构成条」塞回同一文件会超过单文件 500 行的工程约定。
 * 组件只负责渲染，数据与导出动作仍由页面传入，避免两处各取一次数。 */
import { Col, Row, Space, Table, Typography } from 'antd'

import { useI18n } from '../../i18n'
import { WH } from '../../theme'
import { CardBase } from '../../components/cards'
import {
  UNIT_COLOR, UNIT_LABEL, fmt,
  type Detail, type HwUnit, type NeRow, type OptUnit, type SwPkg, type UnitTotal,
} from './ranInventoryTypes'

const { Text } = Typography

interface Props {
  ne: NeRow
  detail: Detail | null
  loading: boolean
  unitTotals: UnitTotal[]
}

export default function RanInventoryDetail({ ne, detail, loading, unitTotals }: Props) {
  const { t } = useI18n()

  const hwCols = [
    { title: t('ranInv.dhw.type'), dataIndex: 't', width: 84 },
    { title: t('ranInv.dhw.model'), dataIndex: 'mt', width: 160, ellipsis: true, render: (v: string, r: HwUnit) => fmt(v || r.m) },
    { title: t('ranInv.dhw.sn'), dataIndex: 'sn', width: 150, render: (v: string) => <Text style={{ fontFamily: 'monospace', fontSize: 12 }}>{fmt(v)}</Text> },
    { title: t('ranInv.dhw.pos'), dataIndex: 'p', ellipsis: true, render: (v: string) => <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 11 }}>{fmt(v)}</Text> },
    { title: t('ranInv.dhw.date'), dataIndex: 'd', width: 104 },
  ]

  const optCols = [
    { title: t('ranInv.dopt.vendor'), dataIndex: 'v', width: 110 },
    { title: t('ranInv.dopt.pn'), dataIndex: 'pn', width: 150, render: (v: string) => <Text style={{ fontFamily: 'monospace', fontSize: 12 }}>{fmt(v)}</Text> },
    { title: t('ranInv.dopt.sn'), dataIndex: 'sn', width: 150, render: (v: string) => <Text style={{ fontFamily: 'monospace', fontSize: 12 }}>{fmt(v)}</Text> },
    { title: t('ranInv.dopt.pos'), dataIndex: 'p', ellipsis: true, render: (v: string) => <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 11 }}>{fmt(v)}</Text> },
    { title: t('ranInv.dhw.date'), dataIndex: 'd', width: 104 },
  ]

  const swCols = [
    { title: t('ranInv.dsw.type'), dataIndex: 't', width: 170 },
    { title: t('ranInv.dsw.ver'), dataIndex: 'v', width: 170, render: (v: string) => <Text style={{ fontFamily: 'monospace', fontSize: 12 }}>{fmt(v)}</Text> },
    { title: t('ranInv.dsw.main'), dataIndex: 'm', width: 150, render: (v: string) => <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 12 }}>{fmt(v)}</Text> },
    { title: t('ranInv.dsw.time'), dataIndex: 'a', width: 110 },
  ]

  const meta: [string, string][] = [
    [t('ranInv.detail.ip'), ne.ip],
    [t('ranInv.detail.moc'), `${ne.moc} / ${ne.sub}`],
    [t('ranInv.detail.ver'), ne.ver],
    [t('ranInv.detail.proto'), ne.proto],
    [t('ranInv.detail.launch'), ne.launch],
    [t('ranInv.detail.oldest'), ne.oldest],
    [t('ranInv.detail.maint'), ne.maint],
    [t('ranInv.detail.uuid'), ne.id],
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--wh-space-md)' }}>
      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: 'var(--wh-space-sm) var(--wh-space-md)',
      }}>
        {meta.map(([k, v]) => (
          <div key={k} style={{ minWidth: 0 }}>
            <div style={{ fontSize: 11, color: WH.textDim }}>{k}</div>
            <div style={{
              fontSize: 12, color: WH.text,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>
              {fmt(v)}
            </div>
          </div>
        ))}
      </div>

      <Row gutter={[12, 12]}>
        <Col xs={24} md={12}>
          <CardBase as="div" title={t('ranInv.detail.compo')}>
            <div style={{ display: 'grid', gap: 6 }}>
              {unitTotals.map(u => {
                const n = ne.u[u.key] || 0
                // 条长按**该网元自己**的构成取比例：用全网量级当分母的话，
                // 二十几台的小站每根条都只有 0.1%，看起来像没画出来
                const max = Math.max(...unitTotals.map(x => ne.u[x.key] || 0), 1)
                return (
                  <div key={u.key} style={{
                    display: 'grid', gridTemplateColumns: '72px 1fr 40px',
                    alignItems: 'center', gap: 8,
                  }}>
                    <Text style={{ fontSize: 12 }}>{t(UNIT_LABEL[u.key] || 'ranInv.unit.pack')}</Text>
                    <span style={{
                      height: 6, borderRadius: 999, background: WH.cardBorder,
                      overflow: 'hidden', display: 'block',
                    }}>
                      <i style={{
                        display: 'block', height: '100%', width: `${(n / max) * 100}%`,
                        background: UNIT_COLOR[u.key] || WH.teal,
                      }} />
                    </span>
                    <Text style={{ fontSize: 12, textAlign: 'right' }}>{n}</Text>
                  </div>
                )
              })}
            </div>
          </CardBase>
        </Col>
        <Col xs={24} md={12}>
          <CardBase as="div" title={t('ranInv.detail.summary')}>
            <Space direction="vertical" size={4} style={{ fontSize: 12 }}>
              <Text>{t('ranInv.detail.hwCount', { n: ne.hw })}</Text>
              <Text>{t('ranInv.detail.optCount', { n: ne.opt })}</Text>
              <Text>{t('ranInv.detail.swCount', { n: ne.sw, p: ne.patch })}</Text>
              {(ne.hw_bad > 0 || ne.opt_bad > 0) && (
                <Text type="warning">
                  {t('ranInv.detail.state', { hw: ne.hw_bad, opt: ne.opt_bad })}
                </Text>
              )}
            </Space>
          </CardBase>
        </Col>
      </Row>

      <CardBase as="div" title={`${t('ranInv.detail.hw')} · ${detail?.hw.length ?? 0}`} bodyStyle={{ padding: 0 }}>
        <Table<HwUnit>
          rowKey={(r, i) => `${r.sn}-${i}`}
          size="small"
          loading={loading}
          columns={hwCols}
          dataSource={detail?.hw || []}
          pagination={{ pageSize: 8, size: 'small' }}
          scroll={{ x: 760 }}
          locale={{ emptyText: t('ranInv.detail.noHw') }}
        />
      </CardBase>

      <CardBase as="div" title={`${t('ranInv.detail.opt')} · ${detail?.opt.length ?? 0}`} bodyStyle={{ padding: 0 }}>
        <Table<OptUnit>
          rowKey={(r, i) => `${r.sn}-${i}`}
          size="small"
          loading={loading}
          columns={optCols}
          dataSource={detail?.opt || []}
          pagination={{ pageSize: 6, size: 'small' }}
          scroll={{ x: 760 }}
          locale={{ emptyText: t('ranInv.detail.noOpt') }}
        />
      </CardBase>

      <CardBase as="div" title={`${t('ranInv.detail.sw')} · ${detail?.sw.length ?? 0}`} bodyStyle={{ padding: 0 }}>
        <Table<SwPkg>
          rowKey={(r, i) => `${r.v}-${i}`}
          size="small"
          loading={loading}
          columns={swCols}
          dataSource={detail?.sw || []}
          pagination={false}
          scroll={{ x: 660 }}
          locale={{ emptyText: t('ranInv.detail.noSw') }}
        />
      </CardBase>
    </div>
  )
}
