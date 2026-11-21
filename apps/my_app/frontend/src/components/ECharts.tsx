/**
 * 全站统一的 ECharts React 包装（2026-11-12 首屏瘦身）。
 *
 * `echarts-for-react` 的默认入口内部 `import * as echarts from 'echarts'`，
 * 整包 1.16MB 会跟进每一个用图的页面 chunk；改用它的 core 版
 * `echarts-for-react/esm/core` + `../utils/echarts` 的按需实例，props 与
 * 默认入口完全一致（option / notMerge / opts / onEvents / onChartReady 等
 * 全部透传），各页只改 import 行、JSX 不用动。
 *
 * ref 会透传给 core 实例（Dashboard.tsx 用 ref 存图表实例，与改前一致）。
 *
 * ⚠ 不要改回 `from 'echarts-for-react'`：那会立刻把整包 echarts 拖回来。
 * 新增图/组件时去 `../utils/echarts` 登记，不要在这里另起实例。
 */
import { forwardRef } from 'react'
/* ⚠ 必须用 `esm/` 下的入口，不能用 `lib/`（2026-11-16 全站白屏事故）。
 *
 * 背景：`echarts-for-react` 没有 `exports` 字段，`lib/core.js` 是 tsc 编译出来的
 * CommonJS（`exports.__esModule = true; exports.default = 类`）。rolldown（vite 8）
 * 对这个模块的 CJS→ESM 互操作产出的是 `export default require_core()`，也就是
 * **整个 module.exports 对象**，而不是 `.default`。于是默认导入拿到的是
 * `{__esModule, default}` 对象，`<EChartsReactCore />` 在渲染时立刻抛
 * React #130「Element type is invalid … got: object」，整个 React 根节点被卸载
 * → 页面纯白（无任何报错 UI）。
 *
 * 症状特征：所有用图表的页面（alarm / cn_kpi / bp_report / bm_meta / person /
 * avail_county / fme_ranking / packet_loss …）打开即空白，纯表格/iframe 页正常；
 * 控制台只有一行 Minified React error #130。dev 模式下报
 * "Check the render method of `ForwardRef(_c)`"，`_c` 就是本包装。
 *
 * `esm/core.js` 是真正的 ESM（`export default EChartsReactCore`），不经过 CJS 互操作，
 * 且同样只依赖 react/size-sensor，不会把整包 echarts 拖回来。旁边还有 `esm/core.d.ts`，
 * 类型不受影响。改回 `lib/` 会立刻复现白屏 —— 别改。
 *
 * ⚠ 下面仍保留一层运行时归一（`.default ?? 模块本身`）：它来自 02 仓库的
 * PR #101（修「ECharts 包装的 CJS 互操作致 21 个图表页白屏」），是**兜底**，
 * 不是主路径 —— `esm/` 是真 ESM，正常情况下 `.default` 不存在，归一后就是模块
 * 本身。留着它是为了万一构建链路的互操作行为再变一次，不会静默变成白屏；
 * 代价只是几行，且不影响 `esm/` 路径。 */
import EChartsReactCoreImport from 'echarts-for-react/esm/core'
import type { EChartsReactProps } from 'echarts-for-react/esm/types'
import echarts from '../utils/echarts'

export type { EChartsReactProps }

/* 显式归一：先取一层 `.default`（若存在），否则用模块本身。
 * 两种构建链路（dev 预构建 / rolldown 生产构建）都能拿到组件。 */
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
