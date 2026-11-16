# OSS 智能运维平台前端现代化改造计划

> 状态：Phase 0 静态审计已启动；当前提交只建立审计基线和实施计划，不修改业务代码、不部署生产。
> 仓库：`Jouwala/MS-Intelligent-O-M-Platform`
> 审计基线：`master`，2026-11-16
> 工作分支：`docs/frontend-modernization-phase0`

## 1. 改造目标

将 OSS 门户与 my_app 前端升级为统一品牌体验下的现代化智能运维平台。视觉参考小米小爱官网的科技感、渐变、层次、精致动效和产品化叙事，但不复制其页面结构，也不牺牲 NOC/SOC 的数据密度、操作效率、可读性和实时性。

采用两种页面体验、一个共享设计原则：

- **Portal / Brand Experience**：登录、门户、系统入口，以产品展示和品牌识别为主。
- **Operations Experience**：CXO、GIS、拓扑、告警、性能、表格、报表，以专业运维和高效操作为主。
- 两个前端独立部署、独立构建；通过设计 Token 和视觉规范统一体验，不强行合并为一个 React 应用。

## 2. Phase 0 静态审计发现

### 2.1 仓库与部署边界

- 本仓库 README 将其定义为便携部署副本；配套主仓库为 `Jouwala/my_tools_Liberia_noc1-rpa`。
- 本仓库包含统一门户、`Task/my_app`、WhatsApp bot、部署与运维脚本。
- README 说明本目录自 2026-11-11 起转为生产运行目录；源码同步、前端构建产物、常驻后端进程是不同环节。
- 因此本改造先在独立 Git 分支进行；未经构建、回归、显式部署审批，不触碰生产运行副本、不执行同步脚本、不重启服务。

### 2.2 前端一：统一门户

路径：`liberia-noc_soc-数据看板统一门户/`

已确认技术：
- React 19、Vite 6、TypeScript。
- Tailwind CSS v4。
- `motion` 12 已安装，可用于动效；`lucide-react` 已安装。
- 不使用 Ant Design；不得把 my_app 的 Ant Design 方案照搬到门户。
- 已有中英文 i18n、认证上下文、主题切换、看板收藏/置顶/排序、TaskSyncWidget、卡片/清单/矩阵视图。
- `src/index.css` 已有品牌色、深浅主题、间距、圆角、断点、基础动画、减少动态效果适配。
- `src/App.tsx` 已实现多个次要视图和弹窗的 lazy loading。

初步判断：
- 门户不是从零开始的视觉系统。应在现有 Tailwind Token、深浅主题和 Motion 基础上做增量升级。
- 首屏主要改造对象为 Navbar、Hero/欢迎区域、QuickStatsBar、DashboardCard、分类与视图切换。
- 必须保留现有的看板目录、收藏/置顶、排序、二维码、预览、环境切换、用户认证和 TaskSyncWidget 行为。
- README 与代码中的看板数量描述可能存在历史口径差异；正式改文案前先运行现有文档一致性守卫，以代码和脚本输出为准。

### 2.3 前端二：my_app

路径：`Task/my_app/frontend/`

已确认技术：
- React 19、TypeScript、Vite 8。
- Ant Design 6.5。
- ECharts 6、echarts-for-react。
- Leaflet 1.9、protomaps-leaflet。
- 已有自定义 `theme.ts` / CSS Variables（`WH`）主题体系。
- `src/index.css` 已有深色默认主题、浅色主题变量、液态玻璃层级、状态色、字体本地化和降级策略。
- `App.tsx` 已按页面 lazy loading，并包含鉴权启动、菜单、权限和页面分发逻辑。
- `vite.config.ts` 的构建输出目录、分包策略、module preload 和严格执行顺序都承载了线上稳定性约束。

初步判断：
- 不得另起一套与 `WH` 冲突的主题系统；应先盘点现有 Token 和 Ant Design ConfigProvider 的实际映射。
- 不得随意更换 Vite 构建输出目录、清理策略、chunk 策略或 API 代理。
- 大型 `App.tsx`、`index.css` 不做一次性重写；新增样式应分模块、低风险、可回滚。
- 卡片、导出、弹窗、口径说明和 i18n 必须遵循 `Task/my_app/AGENTS.md` 的既有项目规范。

### 2.4 地图与拓扑：必须按现有实现分开处理

| 页面入口 | 当前实现 | 改造策略 |
|---|---|---|
| `?page=olibTopo` | `NetworkTopology.tsx`；Orange Liberia 两页 PDF 原图作为物理拓扑底图，叠加 SVG 站点层；约 553 个提取站点；支持缩放、搜索、定位、物理/逻辑/核心视图 | 保留原始地理/拓扑依据，优先改善底图清晰度、SVG 叠加、控件、搜索、详情、图例和全屏布局；不得简单把底图替换成通用地图 |
| `?page=mwUmeTopology` | `MwUmeDashboard.tsx` 通过 `LegacyToolFrame` 加载 `public/mw_ume_topology.html`；HTML 内有 SVG 拓扑绘制和 Leaflet 地理模式 | 先检查 iframe 高度、主题同步、消息通信和 HTML 内部交互；优先改进原页面自身样式与地图体验，不要未经评估重写成 React |
| `?page=gis` / `realtime_alarm` | my_app 现有 GISMap 页面，Leaflet 生态 | 保持现有地图能力和数据契约，统一控件、图例、筛选和详情面板视觉 |

**重要纠偏：** `olibTopo` 虽然以 PDF 原图为底图，但并非简单的静态图片页面；现有实现有 SVG 站点叠加和搜索/定位交互。它适合做“高质量底图 + 可交互矢量图层”的专项优化。MapLibre 不能作为默认替换方案，只有在明确需要矢量底图、数据量和离线瓦片能力并完成验证后才考虑。

### 2.5 当前审计限制

本轮通过 GitHub 源码读取完成静态审计；当前环境没有本地克隆、Node 依赖和浏览器运行会话。因此以下尚未验证：
- `npm ci` / `npm run lint` / `npm run build` 实际结果。
- 浏览器实际截图、移动端布局、交互与视觉回归。
- 生产机器当前运行版本和实际页面表现。
- 所有页面的完整路由清单与每个页面的视觉状态。

这些项目必须由可访问开发环境的 Muse 在 Phase 0.2 完成，不得将“静态读取未发现问题”表述为“构建/运行测试已通过”。

## 3. 目标设计规范

### 3.1 视觉原则

- 品牌色以电光蓝、青色、少量紫色为主；红/橙/黄仅表达真实告警语义。
- Portal 可以采用深色渐变 Hero、柔和光效和更强的展示感。
- Operations 默认延续现有深色运维主题，并审慎评估浅色主题一致性；不得全站强制换成深色或浅色。
- 卡片、表格、地图和弹窗统一圆角、间距、边框、阴影和焦点状态。
- 避免过度玻璃拟态、过度模糊、全屏粒子、无意义持续动画。
- 保证中文/英文文本长度、缩放比例和高密度数据的可读性。

### 3.2 动效原则

- Hover / focus：约 160–220ms。
- 普通页面与卡片进入：约 220–360ms。
- 仅展示型 Hero 可使用低频背景动画。
- 地图只对活动链路、选中对象和重点告警使用动画。
- 必须尊重 `prefers-reduced-motion`。
- 数据刷新不得重播整个页面入场动画；组件卸载必须清理监听器、动画和定时任务。

### 3.3 共享 Token 与双前端映射

先形成设计 Token 规范表，再分别映射：
- Portal：Tailwind v4 `@theme` / CSS Variables。
- my_app：现有 `WH` / `--wh-*` / Ant Design ConfigProvider。

不得为了“统一”而在两个独立应用之间增加运行时共享包或引入新的构建耦合。

## 4. 实施阶段

### Phase 0.2：完成运行态审计（下一步）

由指挥官安排：
1. 在开发机检查两个前端的 Node 版本、依赖安装状态和现有 Git 工作区。
2. 分别运行门户和 my_app 现有 lint / i18n / docs / test / build 守卫。
3. 收集 Portal、my_app 首页、CXO、`olibTopo&mwembed`、`mwUmeTopology`、GIS、告警、表格页面截图。
4. 记录 1366×768、1920×1080、移动窄屏三档布局。
5. 检查真实 API、鉴权、iframe、FineBI 跳转和页面深链。
6. 完成全量页面清单和问题分级（P0/P1/P2）。
7. 将运行态证据补充到本计划，不通过的检查明确记录，不得跳过。

### Phase 1：设计系统和 Portal 首屏

交付：
- Portal / Operations 双主题视觉规范。
- 现有 Token 对照表，不重复造 Token。
- Portal 首页和登录页改造。
- 10 个系统入口原名称、原跳转和权限行为完整保留（以运行态盘点为准）。
- 前后对比截图和构建报告。

### Phase 2：CXO Dashboard

交付：
- KPI、趋势图、地图、告警摘要统一视觉。
- 地图在宽屏布局中目标约占 2/3 主内容宽度；侧栏信息可折叠。
- 数据刷新不闪烁、不重置筛选、不丢失状态。
- 缺失数据仍显示缺失态，不得用 0 或假数据替代。

### Phase 3：GIS / Topology 专项

交付：
- OLIB PDF 底图 + SVG 图层清晰度与交互优化。
- MW UME iframe/HTML 拓扑布局、主题同步、全屏、缩放和详情优化。
- GIS 控件、图例、筛选、详情面板视觉统一。
- KML/PDF 导出保留原有地理坐标、方向、图层语义。
- 性能测试覆盖典型站点数和高密度场景。

### Phase 4：业务页面统一

交付：
- 告警、工单、性能、数据表格、报表、系统管理逐组改造。
- 统一 PageHeader、查询区、表格、状态标签、详情抽屉、空态/错误态。
- 保留现有字段、API、权限、导出和下钻行为。
- 遵守项目统一 Card、Excel、Modal、MetricExplain、CalcChainNote 和 i18n 约定。

### Phase 5：回归与部署验收

交付：
- 全页面功能回归、视觉截图回归。
- 构建与类型检查报告。
- 性能与地图压力测试。
- Windows / Linux 便携部署验证。
- 变更清单、已知问题、回滚步骤。
- 未经用户显式批准，不执行生产同步、服务重启或发布。

## 5. Muse 分工与文件边界

| 角色 | 职责 | 文件边界建议 |
|---|---|---|
| 指挥官 Muse | 审计、任务拆解、接口协调、PR 验收 | 计划、Issue、集成与验收文档 |
| Muse 2：Design System | Token 对照、Portal 主题、公共视觉组件 | Portal 的 token / shared UI；不得同时改 my_app 全局 CSS |
| Muse 3：Portal | 登录、导航、Hero、DashboardCard、首页布局 | Portal 页面与组件；不改 my_app |
| Muse 4：Operations / GIS | CXO、OLIB、MW UME、GIS 专项 | my_app 地图与目标业务页面；先与指挥官确定文件清单 |
| Muse 5：QA | 截图、Playwright、功能回归、性能检查 | 测试脚本、截图与报告；默认不直接改业务组件 |

并行规则：
- Portal 和 my_app 是两个独立前端，允许分开分支和 PR。
- 同一阶段 Design Token 文件只能有一个负责人。
- 修改 `App.tsx`、`index.css`、`vite.config.ts`、主题入口前必须先协调。
- 每个 PR 附文件清单、截图、测试结果、风险和回滚方式。
- 不允许多个 Muse 在同一个共享工作目录直接互相覆盖未提交文件。

## 6. PR 与质量门槛

建议分支：
- `feature/portal-visual-system`
- `feature/portal-home-modernization`
- `feature/cxo-visual-modernization`
- `feature/olib-topology-visual`
- `feature/mw-ume-topology-visual`
- `feature/operations-pages-visual`
- `test/frontend-visual-regression`

每个 PR 必须：
1. 明确修改范围与不修改范围。
2. 保留原页面 URL、API、鉴权和权限。
3. 通过对应项目已有 lint、i18n、docs、test、build 守卫。
4. 附修改前后相同视口截图。
5. 提供至少一个真实页面交互回归证据。
6. 说明性能影响和回滚方式。

性能目标（需在现有基线基础上验证）：
- LCP ≤ 2.5s、CLS ≤ 0.1、INP ≤ 200ms（适用页面与稳定测试环境）。
- 地图缩放/拖动无明显卡顿；记录数据规模、设备、浏览器和帧率/响应时间。
- 不允许通过隐藏内容、减少真实数据或降低刷新频率“优化”指标。

## 7. 下一步可执行任务

**TASK-FE-001：完成 Phase 0.2 运行态审计。**

指挥官必须先提交：
- `FRONTEND_PAGE_INVENTORY.md`
- `FRONTEND_RUNTIME_BASELINE.md`
- `FRONTEND_VISUAL_BASELINE/` 截图
- `FRONTEND_RISK_REGISTER.md`
- 更新后的本计划

完成并审核后，才分派 Phase 1 的编码任务。

## 8. 变更纪律

- 本分支只提交计划和审计文档；没有用户授权前不直接合并 master。
- 不改后端 API、数据库、认证、调度、实时代理和部署脚本。
- 不更改线上服务配置，不执行生产构建/同步/重启。
- 不删除或替换现有 GIS、SVG、PDF 底图、Leaflet 或 iframe 实现，除非专项验证证明必要且有回滚方案。
- 所有新增文案遵循现有 zh/en i18n 守卫。
