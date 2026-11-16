/**
 * 全站统一的 ECharts React 包装（2026-11-12 首屏瘦身）。
 *
 * `echarts-for-react` 的默认入口内部 `import * as echarts from 'echarts'`，
 * 整包 1.16MB 会跟进每一个用图的页面 chunk；改用它的 core 版
 * `echarts-for-react/lib/core` + `../utils/echarts` 的按需实例，props 与
 * 默认入口完全一致（option / notMerge / opts / onEvents / onChartReady 等
 * 全部透传），各页只改 import 行、JSX 不用动。
 *
 * ref 会透传给 core 实例（Dashboard.tsx 用 ref 存图表实例，与改前一致）。
 *
 * ⚠ 不要改回 `from 'echarts-for-react'`：那会立刻把整包 echarts 拖回来。
 * 新增图/组件时去 `../utils/echarts` 登记，不要在这里另起实例。
 */
import { forwardRef } from 'react'
import EChartsReactCoreImport from 'echarts-for-react/lib/core'
import type { EChartsReactProps } from 'echarts-for-react/lib/types'
import echarts from '../utils/echarts'

export type { EChartsReactProps }

/* ⚠️ 不要把这里改回「直接 JSX 用 import 进来的默认导出」（2026-11-16 实测白屏）。
 *
 * echarts-for-react 的 `lib/core` 是 **CJS**（文件里是 `exports.default = ...`），
 * 而它没有 `package.json` 的 `exports` 字段做条件导出。于是不同链路对它的
 * 「默认导出」解释不一致：
 *   · vite dev 的依赖预构建 → `default` 是 **{ default: Component }**（双层）
 *   · 生产构建（rolldown）        → `default` 是 **Component**（单层）
 *
 * 直接 `<EChartsReactCore />` 时，dev 下 createElement 收到的是一个普通 object，
 * 抛 React #130「Element type is invalid ... got: object」。因为报错发生在 antd/
 * echarts 内部的 ForwardRef，堆栈里看不到本文件，表现为**所有带图的页面整页白屏**、
 * 而列表类页面正常（2026-11-16 实测 21 个页面白屏 / 46 个正常）。
 *
 * 显式归一：先取一层 `.default`（若存在），否则用模块本身。两种链路都能拿到组件。 */
const EChartsReactCore =
  (EChartsReactCoreImport as unknown as { default?: typeof EChartsReactCoreImport }).default
  ?? EChartsReactCoreImport

/* 归一之后 EChartsReactCore 是「值」，不能再用在类型位置（TS2749）。
 * ref 的实例类型仍从原导入的类推导，语义与归一前一致。 */
type EChartsReactCoreInstance = InstanceType<typeof EChartsReactCoreImport>

const ReactECharts = forwardRef<EChartsReactCoreInstance, Omit<EChartsReactProps, 'echarts'>>(
  (props, ref) => <EChartsReactCore ref={ref} echarts={echarts} {...props} />,
)

export default ReactECharts
