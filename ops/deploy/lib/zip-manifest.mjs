/**
 * ZIP 打包清单（纯数据）：白名单、拒绝规则、回归文件。
 *
 * 逻辑在 zip-package.mjs。改动这里 = 改动交付内容，须经评审。
 *
 * P0-1 教训（PR #151 评审）：拒绝规则不得按目录名一律生效——
 * apps/my_app/backend/services/db/、apps/my_app/backend/data/gis/、
 * apps/portal/src/data/ 是源码/运行时数据，曾被 db/data 目录名规则静默丢掉。
 * 数据目录只按完整相对路径拦（PATH_DENIED_DIRS，应用根级）；目录名规则
 * （BASENAME_DENIED_DIRS）只保留无歧义的缓存/垃圾/运行态名。
 */

export const APP_NAME = 'noc-soc-intelligent-platform'

// 运行时版本固定真相源：runtime/README.md 与 Phase 0 报告 §1.2。
export const RUNTIME_VERSIONS = { python: '3.12.10', node: '24.16.0', postgresql: '16', nginx: '1.28.0' }

// 目录：递归复制（受拒绝规则过滤）。requiredIn: full 仅在 full 模式要求存在。
export const INCLUDE_DIRS = [
  { src: 'apps/my_app/backend', requiredIn: 'any' },
  { src: 'apps/my_app/frontend', requiredIn: 'any' },
  { src: 'apps/my_app/alembic', requiredIn: 'any' },
  { src: 'apps/my_app/docs', requiredIn: 'any' },
  { src: 'apps/my_app/tests', requiredIn: 'any' },
  { src: 'apps/my_app/tools', requiredIn: 'any' },
  { src: 'apps/my_app/email_reports', requiredIn: 'any' },
  { src: 'apps/my_app/monitor_fixes', requiredIn: 'any' },
  { src: 'apps/my_app/nginx', requiredIn: 'any' },
  { src: 'apps/portal/src', requiredIn: 'any' },
  { src: 'apps/portal/server', requiredIn: 'any' },
  { src: 'apps/portal/assets', requiredIn: 'any' },
  { src: 'apps/portal/scripts', requiredIn: 'any' },
  { src: 'apps/portal/docs', requiredIn: 'any' },
  { src: 'apps/portal/MsPortal', requiredIn: 'any' },
  { src: 'apps/portal/OlrPortal', requiredIn: 'any' },
  { src: 'apps/portal/olbrportal', requiredIn: 'any' },
  { src: 'apps/portal/v1', requiredIn: 'any' },
  { src: 'apps/portal/v2', requiredIn: 'any' },
  { src: 'apps/portal/v3', requiredIn: 'any' },
  { src: 'apps/whatsapp-kb-bot/src', requiredIn: 'any' },
  { src: 'apps/whatsapp-kb-bot/scripts', requiredIn: 'any' },
  { src: 'apps/whatsapp-kb-bot/docs', requiredIn: 'any' },
  { src: 'apps/whatsapp-kb-bot/knowledge', requiredIn: 'any' },
  { src: 'apps/whatsapp-kb-bot/evals', requiredIn: 'any' },
  { src: 'apps/eflow_Q&A', requiredIn: 'any' },
  { src: 'apps/agent-worker', requiredIn: 'any' },
  { src: 'apps/agent-worker-linux', requiredIn: 'any' },
  { src: 'apps/control-plane', requiredIn: 'any' },
  { src: 'extensions/whatsapp_sender_extension', requiredIn: 'any' },
  { src: 'extensions/portal_login_extension', requiredIn: 'any' },
  { src: 'ops/smoke', requiredIn: 'any' }, // 冒烟引擎本体（smoke-local.ps1 只是薄壳），供目标机自检
  // 构建产物：仅 full 模式（工作流先 npm ci / npm run build / pip download）。
  // node_modules 只走显式条目（通用遍历里一律跳过，避免父目录是否入白名单造成的不一致）。
  { src: 'apps/my_app/frontend/node_modules', requiredIn: 'full' },
  { src: 'apps/portal/node_modules', requiredIn: 'full' },
  { src: 'apps/whatsapp-kb-bot/node_modules', requiredIn: 'full' },
  { src: 'apps/portal/scripts/whatsapp_analysis/node_modules', requiredIn: 'full' },
  { src: 'apps/_fe_dist', requiredIn: 'full' },
  { src: 'apps/portal/dist', requiredIn: 'full' },
  { src: 'wheels', requiredIn: 'full' },
]

// 单文件：required=false 的缺失只警告（P1-4 交付物、部署文档子集）。
export const INCLUDE_FILES = [
  { src: 'apps/my_app/requirements.txt', required: true },
  { src: 'apps/my_app/requirements.lock.txt', required: true },
  { src: 'apps/my_app/pyproject.toml', required: true },
  { src: 'apps/my_app/alembic.ini', required: true },
  { src: 'apps/my_app/frontend/package.json', required: true },
  { src: 'apps/my_app/frontend/package-lock.json', required: true },
  { src: 'apps/portal/package.json', required: true },
  { src: 'apps/portal/package-lock.json', required: true },
  { src: 'apps/portal/index.html', required: true },
  { src: 'apps/portal/vite.config.ts', required: true },
  { src: 'apps/portal/tsconfig.json', required: true },
  { src: 'apps/portal/scripts/whatsapp_analysis/package.json', required: true },
  { src: 'apps/portal/scripts/whatsapp_analysis/package-lock.json', required: true },
  { src: 'apps/whatsapp-kb-bot/package.json', required: true },
  { src: 'apps/whatsapp-kb-bot/package-lock.json', required: true },
  // ops/deploy 只发 §6.3 清单 + 冒烟 + 构建脚本自身；生产控制（my_portal_ctl.ps1）
  // 与已停用的同步脚本不进包。
  { src: 'ops/deploy/start_all.ps1', required: true },
  { src: 'ops/deploy/stop_all.ps1', required: true },
  { src: 'ops/deploy/noc-paths.ps1', required: true },
  { src: 'ops/deploy/write_nginx_conf.ps1', required: true },
  { src: 'ops/deploy/env_template.txt', required: true },
  { src: 'ops/deploy/agent-done-check.mjs', required: true },
  { src: 'ops/deploy/lib/agent-checks.mjs', required: true },
  { src: 'ops/deploy/smoke-local.ps1', required: true },
  { src: 'ops/deploy/smoke-watcher.ps1', required: true },
  { src: 'ops/deploy/install-smoke-watcher.ps1', required: true },
  { src: 'ops/deploy/bundle_data.ps1', required: true },
  { src: 'ops/deploy/local_authority_tools.py', required: true },
  { src: 'ops/deploy/build-zip.mjs', required: true },
  { src: 'ops/deploy/lib/zip-package.mjs', required: true },
  { src: 'ops/deploy/lib/zip-manifest.mjs', required: true },
  { src: 'ops/deploy/lib/zip-writer.mjs', required: true },
  { src: 'ops/deploy/lib/zip-dir.mjs', required: true },
  // 根入口与模板。
  { src: 'nginx.conf.template', required: true },
  { src: 'start.bat', required: true },
  { src: 'stop.bat', required: true },
  { src: 'start.sh', required: false, note: 'P1-4 交付，缺失只警告' },
  { src: 'stop.sh', required: false, note: 'P1-4 交付，缺失只警告' },
  // 部署文档子集（§6.3：不含全量历史文档）。
  { src: 'docs/workspace/部署说明.md', required: false },
  { src: 'docs/architecture/data-source-registry.md', required: false },
]

// full 模式下暂存区内必须存在的路径（工作流须先装好依赖、建好产物、下载好 wheels）。
export const FULL_REQUIRED = [
  { rel: 'apps/my_app/frontend/node_modules', kind: 'dir' },
  { rel: 'apps/portal/node_modules', kind: 'dir' },
  { rel: 'apps/whatsapp-kb-bot/node_modules', kind: 'dir' },
  { rel: 'apps/portal/scripts/whatsapp_analysis/node_modules', kind: 'dir' },
  { rel: 'apps/_fe_dist/index.html', kind: 'file' },
  { rel: 'apps/portal/dist/index.html', kind: 'file' },
  { rel: 'wheels', kind: 'wheels' },
]

// 按目录名跳过：只保留无歧义的缓存/垃圾/运行态名。db/data 不在此列（见文件头 P0-1 教训）。
// node_modules/dist 由 assemble() 另行加入拒绝集（显式条目除外，见 P1-1）。
export const BASENAME_DENIED_DIRS = new Set([
  '__pycache__', '.git', '.idea', '.vscode', '.venv', 'venv', 'coverage',
  '.pytest_cache', '.ruff_cache', '.mypy_cache', 'auth-data', 'auth_cache',
  'log', 'logs', 'tmp', 'temp', 'TMP', 'runtime', '.cache',
  'backup', 'backups', 'outputs', 'output', 'sessions', 'whatsapp_session',
  'credentials', 'playwright-report', 'test-results', '.agent-work', '.workbuddy-ai',
])

// 按完整相对路径拦：仅应用根级数据目录。嵌套的同名源码目录（如 backend/data/gis）不受影响。
export const PATH_DENIED_DIRS = [
  'apps/my_app/db',
  'apps/my_app/data',
  'apps/whatsapp-kb-bot/data',
  'apps/portal/auth-data',
]

// 回归断言（P0-1）：组装后暂存区内必须存在的文件。缺失即门禁失败，防白名单规则回退。
export const REQUIRED_STAGING_FILES = [
  'apps/my_app/backend/services/db/__init__.py',
  'apps/my_app/backend/services/db/db_freshness_check.py',
  'apps/my_app/backend/services/db/db_writer_client.py',
  'apps/my_app/backend/services/db/pg_schema_freshness.py',
  'apps/my_app/backend/data/gis/liberia_counties.geojson',
  'apps/my_app/backend/data/gis/liberia_country.geojson',
  'apps/portal/src/data/initialDashboards.ts',
  'apps/portal/src/data/orangeTopologySites.json',
]
