/**
 * ZIP 打包：清单、组装、断言、扫描、规范化（build-zip.mjs 的实现层）。
 *
 * 设计依据：Phase 0 报告（docs/workspace/zip-delivery-phase0-review.md）§6.1–§6.6。
 * 白名单思想沿用 ops/deploy/sync-policy.mjs，但清单是独立的——打包 ≠ 同步，
 * 不得 git archive 全量（会带入 .agent-work/、TMP/、*.bak* 等无关内容）。
 *
 * 零第三方依赖（Node 内置模块 + git CLI），与 agent-checks.mjs 同一约束。
 * 所有失败都带可定位的文件路径与原因；凭据扫描只报“文件:行:规则”，绝不打印命中内容。
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import {
  copyFileSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync,
} from 'node:fs'
import { dirname, join, resolve } from 'node:path'

export const APP_NAME = 'noc-soc-intelligent-platform'

// 运行时版本固定真相源：runtime/README.md 与 Phase 0 报告 §1.2。
export const RUNTIME_VERSIONS = { python: '3.12.10', node: '24.16.0', postgresql: '16', nginx: '1.28.0' }

/** 门禁类失败（排除断言 / 凭据扫描 / full 模式缺件）：调用方以退出码 2 区分。 */
export class GateError extends Error {
  constructor(message) {
    super(message)
    this.name = 'GateError'
    this.code = 2
  }
}

const posix = (p) => p.split('\\').join('/')

// ─────────────────────────────────────────────────────────────────────────────
// 白名单（显式列出；改动这里 = 改动交付内容，须经评审）
// ─────────────────────────────────────────────────────────────────────────────

// 目录：递归复制（受拒绝名录过滤）。requiredIn: full 仅在 full 模式要求存在。
const INCLUDE_DIRS = [
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

// 单文件：dest 省略时与 src 相同。required=false 的缺失只警告（P1-4 交付物、部署文档子集）。
const INCLUDE_FILES = [
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
const FULL_REQUIRED = [
  { rel: 'apps/my_app/frontend/node_modules', kind: 'dir' },
  { rel: 'apps/portal/node_modules', kind: 'dir' },
  { rel: 'apps/whatsapp-kb-bot/node_modules', kind: 'dir' },
  { rel: 'apps/portal/scripts/whatsapp_analysis/node_modules', kind: 'dir' },
  { rel: 'apps/_fe_dist/index.html', kind: 'file' },
  { rel: 'apps/portal/dist/index.html', kind: 'file' },
  { rel: 'wheels', kind: 'wheels' },
]

// 递归复制时跳过的目录名（source 模式另加 node_modules/dist，见 assemble）。
const DENIED_DIR_NAMES = new Set([
  '__pycache__', '.git', '.idea', '.vscode', '.venv', 'venv', 'coverage',
  '.pytest_cache', '.ruff_cache', '.mypy_cache', 'auth-data', 'auth_cache',
  'db', 'data', 'log', 'logs', 'tmp', 'temp', 'TMP', 'runtime', '.cache',
  'backup', 'backups', 'outputs', 'output', 'sessions', 'whatsapp_session',
  'credentials', 'playwright-report', 'test-results', '.agent-work', '.workbuddy-ai',
])

// 文件名级拒绝（组装时过滤 + 断言时复核，双保险）。返回命中原因或 null。
function deniedFileReason(rel) {
  const base = rel.split('/').pop()
  if (base === 'PG_VERSION') return 'PG 数据目录标记'
  if (/^\.env(\..+)?$/.test(base) && base !== '.env.example') return '.env 文件'
  if (/\.(key|pem)$/i.test(base)) return '密钥/证书文件（*.key/*.pem 一律不进包，含 CA 证书——外部 config 提供，待 P1-6 确认）'
  if (/session.*\.json$/i.test(base)) return '会话文件'
  if (/^auth.*\.json$/i.test(base)) return '鉴权文件'
  if (/credential/i.test(base)) return '凭据文件'
  if (rel.startsWith('apps/whatsapp-kb-bot/') && base === 'config.json') return 'bot 含密钥配置'
  if (/\.(log|pyc|pyo|sqlite3?|db|sql|dump)$/i.test(base)) return '日志/数据库/快照文件'
  if (/\.bak(_.*)?$|_bak_/i.test(base)) return '手工快照'
  if (base === '.DS_Store' || base === 'Thumbs.db') return '系统垃圾文件'
  if (/\.whl$/i.test(base) && !rel.startsWith('wheels/')) return 'wheels/ 之外的 wheel 包'
  if (rel.includes('/pgdata/') || rel.startsWith('pgdata/')) return 'pgdata 数据目录'
  return null
}

// ─────────────────────────────────────────────────────────────────────────────
// 组装
// ─────────────────────────────────────────────────────────────────────────────

function walkFiles(dirAbs, relBase, deniedDirs, out) {
  for (const ent of readdirSync(dirAbs, { withFileTypes: true })) {
    const rel = relBase ? `${relBase}/${ent.name}` : ent.name
    const abs = join(dirAbs, ent.name)
    const st = lstatSync(abs)
    if (st.isSymbolicLink()) throw new Error(`拒绝符号链接（ZIP 跨平台不可靠）: ${rel}`)
    if (st.isDirectory()) {
      if (deniedDirs.has(ent.name) || ent.name.startsWith('.')) continue
      walkFiles(abs, rel, deniedDirs, out)
    } else if (st.isFile()) {
      out.push(rel)
    }
  }
  return out
}

/**
 * 按白名单把文件复制到暂存区。返回 { copied: [{rel, bytes}], warnings: [string] }。
 * mode: full（含 node_modules/dist/wheels）或 source（仅源码，跳过构建产物）。
 */
export function assemble({ root, staging, mode }) {
  // node_modules / dist 只走显式白名单条目（full 模式），通用遍历一律跳过——
  // 否则“父目录是否入白名单”会让同一产物时有时无（实测抓到过此缺口）。
  const deniedDirs = new Set(DENIED_DIR_NAMES)
  deniedDirs.add('node_modules')
  deniedDirs.add('dist')
  const copied = []
  const warnings = []
  const copyOne = (rel) => {
    const src = join(root, rel)
    const dst = join(staging, rel)
    const reason = deniedFileReason(rel)
    if (reason) throw new Error(`白名单命中拒绝规则（${reason}）: ${rel}——请从白名单移除或修正规则`)
    mkdirSync(dirname(dst), { recursive: true })
    copyFileSync(src, dst)
    copied.push({ rel, bytes: statSync(src).size })
  }

  for (const d of INCLUDE_DIRS) {
    if (d.requiredIn === 'full' && mode !== 'full') continue
    const abs = join(root, d.src)
    if (!existsSync(abs)) {
      if (d.requiredIn === 'full') throw new GateError(`full 模式缺构建产物目录: ${d.src}（工作流须先构建）`)
      throw new Error(`白名单目录不存在: ${d.src}`)
    }
    for (const rel of walkFiles(abs, d.src, deniedDirs, [])) {
      if (deniedFileReason(rel)) continue
      copyOne(rel)
    }
  }
  for (const f of INCLUDE_FILES) {
    const abs = join(root, f.src)
    if (!existsSync(abs)) {
      if (f.required) throw new Error(`白名单文件不存在: ${f.src}`)
      warnings.push(`可选文件缺失，已跳过: ${f.src}${f.note ? `（${f.note}）` : ''}`)
      continue
    }
    copyOne(f.src)
  }

  if (mode === 'full') {
    const missing = []
    for (const req of FULL_REQUIRED) {
      const abs = join(staging, req.rel)
      if (req.kind === 'dir' && !(existsSync(abs) && statSync(abs).isDirectory())) missing.push(`${req.rel}（目录）`)
      if (req.kind === 'file' && !existsSync(abs)) missing.push(`${req.rel}（文件）`)
      if (req.kind === 'wheels') {
        const wheels = existsSync(abs) ? readdirSync(abs).filter((n) => n.endsWith('.whl')) : []
        if (wheels.length === 0) missing.push('wheels/（须含至少一个 .whl）')
      }
    }
    if (missing.length) {
      throw new GateError(`full 模式缺件（工作流须先装依赖/构建/下载 wheels）:\n  - ${missing.join('\n  - ')}`)
    }
  }
  return { copied, warnings }
}

// ─────────────────────────────────────────────────────────────────────────────
// 排除断言（组装后复核：白名单过滤失效时也能拦住）
// ─────────────────────────────────────────────────────────────────────────────

export function assertExclusions(staging) {
  const violations = []
  const all = walkFiles(staging, '', new Set(), [])
  for (const rel of all) {
    const reason = deniedFileReason(rel)
    if (reason) violations.push(`${rel}（${reason}）`)
    if (rel.split('/').includes('pgdata')) violations.push(`${rel}（pgdata 路径）`)
  }
  // 目录级：pgdata 目录即使为空也不许出现。
  const stack = [staging]
  while (stack.length) {
    const dir = stack.pop()
    for (const ent of readdirSync(dir, { withFileTypes: true })) {
      if (!ent.isDirectory()) continue
      if (ent.name === 'pgdata') violations.push(`${posix(join(dir, ent.name)).slice(staging.length + 1)}/（pgdata 目录）`)
      stack.push(join(dir, ent.name))
    }
  }
  if (violations.length) {
    throw new GateError(`排除断言失败（${violations.length} 项）：\n  - ${violations.slice(0, 20).join('\n  - ')}`)
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 凭据扫描（只报文件:行:规则，绝不打印命中内容）
// ─────────────────────────────────────────────────────────────────────────────

const PLACEHOLDER_VALUES = new Set(['', 'xxx', 'xxxx', 'changeme', 'example', 'placeholder', 'test', 'dummy', 'todo'])
const isPlaceholder = (v) => {
  const t = v.toLowerCase()
  return PLACEHOLDER_VALUES.has(t) || t.startsWith('your-') || t.startsWith('your_') || t.startsWith('<') || t.includes('example')
}

const SCAN_RULES_STRICT = [
  { name: '私钥块', re: /-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----/ },
  { name: 'JSESSIONID 会话值', re: /JSESSIONID\s*=\s*[A-Za-z0-9]{16,}/ },
  { name: 'API 密钥赋值', re: /\b(api[_-]?key|secret[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['"]([A-Za-z0-9_\-./+=]{16,})['"]/i, valueGroup: 2 },
  { name: '口令赋值', re: /\bpassword\s*[:=]\s*['"]([^\s'"]{8,})['"]/i, valueGroup: 1 },
]
const SCAN_RULES_LOOSE = SCAN_RULES_STRICT.slice(0, 2) // node_modules/dist/_fe_dist 只查前两条（避免压缩产物误报）

function isBinary(buf) {
  return buf.subarray(0, 8192).includes(0)
}

export function scanCredentials(staging) {
  const hits = []
  const all = walkFiles(staging, '', new Set(), [])
  for (const rel of all) {
    const loose = rel.includes('/node_modules/') || rel.includes('/dist/') || rel.startsWith('apps/_fe_dist/')
    const buf = readFileSync(join(staging, rel))
    if (isBinary(buf)) continue
    const rules = loose ? SCAN_RULES_LOOSE : SCAN_RULES_STRICT
    const lines = buf.toString('utf8').split('\n')
    lines.forEach((line, idx) => {
      for (const rule of rules) {
        const m = line.match(rule.re)
        if (!m) continue
        if (rule.valueGroup && isPlaceholder(m[rule.valueGroup])) continue
        hits.push(`${rel}:${idx + 1}（${rule.name}）`)
        break
      }
    })
    if (hits.length >= 20) break
  }
  if (hits.length) {
    throw new GateError(`凭据扫描命中 ${hits.length} 处（只列位置，内容已隐去；误报请人工复核后收窄规则）：\n  - ${hits.join('\n  - ')}`)
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 行尾规范化（.ps1 原样保留——字节级要求，见 .gitattributes 头注释）
// ─────────────────────────────────────────────────────────────────────────────

const TEXT_EXTS = new Set(['.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.json', '.md', '.yml', '.yaml', '.toml', '.txt', '.py', '.pyi', '.html', '.htm', '.css', '.scss', '.xml', '.conf', '.template', '.sh', '.bat', '.cmd', '.ini', '.cfg', '.csv'])

export function normalizeStaging(staging) {
  let normalized = 0
  let skipped = 0
  const all = walkFiles(staging, '', new Set(), [])
  for (const rel of all) {
    const ext = rel.slice(rel.lastIndexOf('.')).toLowerCase()
    if (ext === '.ps1' || ext === '.psm1') { skipped += 1; continue } // 字节保留
    if (!TEXT_EXTS.has(ext)) { skipped += 1; continue }
    const abs = join(staging, rel)
    const buf = readFileSync(abs)
    if (isBinary(buf)) { skipped += 1; continue }
    let body = buf.toString('utf8')
    let bom = ''
    if (body.charCodeAt(0) === 0xfeff) { bom = '\ufeff'; body = body.slice(1) }
    const lf = body.replace(/\r\n?/g, '\n')
    const out = ext === '.bat' || ext === '.cmd' ? lf.replace(/\n/g, '\r\n') : lf
    if (bom + out !== buf.toString('utf8')) {
      writeFileSync(abs, bom + out, 'utf8')
      normalized += 1
    }
  }
  return { normalized, skipped }
}

// ─────────────────────────────────────────────────────────────────────────────
// Git 信息与 build-info
// ─────────────────────────────────────────────────────────────────────────────

function git(root, args) {
  const r = spawnSync('git', args, { cwd: root, encoding: 'utf8' })
  return r.status === 0 ? r.stdout.trim() : null
}

export function gitInfo(root) {
  const sha = git(root, ['rev-parse', 'HEAD'])
  const date = git(root, ['log', '-1', '--format=%cI'])
  const tag = git(root, ['describe', '--tags', '--exact-match', 'HEAD'])
  return { sha, date, tag }
}

export function resolveVersion({ version, gitTag, sha }) {
  if (version) return version
  if (gitTag) return gitTag
  return `0.0.0-dev-${(sha ?? 'unknown').slice(0, 7)}`
}

export function resolveMtimeMs({ mtime, gitDate }) {
  if (mtime) {
    const ms = Date.parse(mtime)
    if (Number.isNaN(ms)) throw new Error(`--mtime 无法解析: ${mtime}`)
    return ms
  }
  if (gitDate) {
    const ms = Date.parse(gitDate)
    if (!Number.isNaN(ms)) return ms
  }
  return Date.UTC(1980, 0, 1) // ZIP 时间戳下限；无 git 信息时保证可重复
}

/** 解析 alembic 版本链，找“从未当过 down_revision”的头（多头/无头返回 null 并警告）。 */
export function resolveAlembicHead(root) {
  const dir = join(root, 'apps/my_app/alembic/versions')
  if (!existsSync(dir)) return { head: null, warning: 'alembic versions 目录不存在' }
  const revisions = new Set()
  const downRevisions = new Set()
  for (const name of readdirSync(dir).filter((n) => n.endsWith('.py'))) {
    const text = readFileSync(join(dir, name), 'utf8')
    for (const line of text.split('\n')) {
      const rev = line.match(/^\s*revision\s*(?::[^=]*)?=\s*['"]([^'"]+)['"]/)
      if (rev) revisions.add(rev[1])
      const down = line.match(/^\s*down_revision\s*(?::[^=]*)?=\s*(.+)$/)
      if (down) {
        for (const m of down[1].matchAll(/['"]([^'"]+)['"]/g)) downRevisions.add(m[1])
      }
    }
  }
  const heads = [...revisions].filter((r) => !downRevisions.has(r))
  if (heads.length === 1) return { head: heads[0], warning: null }
  return { head: null, warning: `alembic 头不唯一（${heads.length} 个：${heads.join(', ') || '无'}），build-info 置空` }
}

export function zipFileName(version, platform) {
  if (!/^[A-Za-z0-9._-]+$/.test(version)) throw new Error(`版本号含非法字符: ${version}`)
  return `noc-soc-platform-${version}-${platform}.zip`
}

/**
 * build-info.json（Phase 0 报告 §6.2 模式）。
 * built_at 取源码提交时间（即统一 mtime），而非墙上时钟——否则两次构建字节不同，
 * “可重复构建”不成立。此语义已在 zip-build-release.md 中写明。
 */
export function makeBuildInfo({ version, platform, sha, mtimeMs, alembicHead, zipName }) {
  return {
    name: APP_NAME,
    version,
    commit: sha ?? 'unknown',
    commit_short: (sha ?? 'unknown').slice(0, 7),
    built_at: new Date(mtimeMs).toISOString(),
    platform,
    runtime: RUNTIME_VERSIONS,
    alembic_head: alembicHead,
    artifacts: [zipName],
  }
}

const KEY_FILES = [
  'build-info.json', 'start.bat', 'stop.bat', 'start.sh', 'stop.sh',
  'nginx.conf.template', 'ops/deploy/start_all.ps1', 'ops/deploy/stop_all.ps1',
  'apps/_fe_dist/index.html', 'apps/portal/dist/index.html',
]

export function writeSha256Sums(staging) {
  const lines = []
  for (const rel of KEY_FILES) {
    const abs = join(staging, rel)
    if (!existsSync(abs)) continue
    const hex = createHash('sha256').update(readFileSync(abs)).digest('hex')
    lines.push(`${hex}  ${rel}`)
  }
  const content = `${lines.join('\n')}\n`
  writeFileSync(join(staging, 'SHA256SUMS'), content, 'utf8')
  return lines.length
}

// ─────────────────────────────────────────────────────────────────────────────
// 收集 ZIP 条目（排序 + 权限位；目录也要入条目，保证空目录结构）
// ─────────────────────────────────────────────────────────────────────────────

export function collectEntries(staging) {
  const entries = []
  const walk = (dirAbs, relBase) => {
    for (const ent of readdirSync(dirAbs, { withFileTypes: true })) {
      const rel = relBase ? `${relBase}/${ent.name}` : ent.name
      const abs = join(dirAbs, ent.name)
      const st = lstatSync(abs)
      if (st.isSymbolicLink()) throw new Error(`暂存区出现符号链接: ${rel}`)
      if (st.isDirectory()) {
        entries.push({ path: `${rel}/`, data: Buffer.alloc(0), mode: 0o755, dir: true })
        walk(abs, rel)
      } else if (st.isFile()) {
        entries.push({ path: rel, data: readFileSync(abs), mode: rel.endsWith('.sh') ? 0o755 : 0o644, dir: false })
      }
    }
  }
  walk(staging, '')
  entries.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0))
  return entries
}

export function sha256Hex(buf) {
  return createHash('sha256').update(buf).digest('hex')
}

export { resolve as resolvePath }
