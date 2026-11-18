#!/usr/bin/env node
/**
 * 通用目录打包：把一个目录原样打成 ZIP（供 runtime-build.yml 打运行时包）。
 *
 * 用法：node ops/deploy/lib/zip-dir.mjs <目录> <输出.zip> [--mtime <ISO 时间>]
 * 复用 zip-writer.mjs：固定时间戳、UTF-8 文件名、.sh 可执行位。目录本身不入包，
 * 只含其下的内容。符号链接会被拒绝（与 build-zip.mjs 同一规则）。
 */

import { lstatSync, mkdirSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { writeZip } from './zip-writer.mjs'

function main() {
  const args = process.argv.slice(2)
  const mtimeIdx = args.indexOf('--mtime')
  let mtimeMs = Date.UTC(1980, 0, 1)
  if (mtimeIdx !== -1) {
    const ms = Date.parse(args[mtimeIdx + 1])
    if (Number.isNaN(ms)) throw new Error(`--mtime 无法解析: ${args[mtimeIdx + 1]}`)
    mtimeMs = ms
    args.splice(mtimeIdx, 2)
  }
  const [dir, out] = args
  if (!dir || !out) throw new Error('用法: node ops/deploy/lib/zip-dir.mjs <目录> <输出.zip> [--mtime ISO]')
  const root = resolve(dir)
  const outPath = resolve(out)

  const entries = []
  const walk = (dirAbs, relBase) => {
    for (const ent of readdirSync(dirAbs, { withFileTypes: true })) {
      const rel = relBase ? `${relBase}/${ent.name}` : ent.name
      const abs = join(dirAbs, ent.name)
      const st = lstatSync(abs)
      if (st.isSymbolicLink()) throw new Error(`拒绝符号链接: ${rel}`)
      if (st.isDirectory()) {
        entries.push({ path: `${rel}/`, data: Buffer.alloc(0), mode: 0o755, dir: true })
        walk(abs, rel)
      } else if (st.isFile()) {
        entries.push({ path: rel, data: readFileSync(abs), mode: rel.endsWith('.sh') ? 0o755 : 0o644, dir: false })
      }
    }
  }
  walk(root, '')
  mkdirSync(dirname(outPath), { recursive: true })
  const { entries: count, bytes } = writeZip(entries, outPath, { mtimeMs })
  console.log(`[zip-dir] ${outPath}：${count} 个条目，${(bytes / 1048576).toFixed(1)} MiB`)
}

try {
  main()
} catch (err) {
  console.error(`错误：${err.message}`)
  process.exit(1)
}
