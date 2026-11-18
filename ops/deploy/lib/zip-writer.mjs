/**
 * 纯 Node ZIP 写出器（零第三方依赖）。
 *
 * 为什么不用系统 zip / Compress-Archive：双平台行为不一致（中文文件名、长路径、
 * 时间戳），且无法保证“同一输入 → 同一字节”。本写出器用固定时间戳、固定排序、
 * UTF-8 文件名（通用标志位 bit 11），两次构建同一内容得到的 ZIP 字节相同。
 *
 * 限制：不支持 zip64（单文件与总大小均须 < 4 GiB，超限会显式失败，不静默截断）。
 * 写入方式为边读边写（不把整个 ZIP 攒在内存里），但单个文件会整块读入。
 */

import { deflateRawSync } from 'node:zlib'
import { closeSync, openSync, writeSync } from 'node:fs'

// ─────────────────────────────────────────────────────────────────────────────
// CRC32（IEEE 802.3 多项式，ZIP 规范所用版本）
// ─────────────────────────────────────────────────────────────────────────────

const CRC_TABLE = buildCrcTable()

function buildCrcTable() {
  const table = new Int32Array(256)
  for (let n = 0; n < 256; n += 1) {
    let c = n
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c
  }
  return table
}

export function crc32(buf) {
  let c = 0xffffffff
  for (let i = 0; i < buf.length; i += 1) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

// ─────────────────────────────────────────────────────────────────────────────
// DOS 时间（ZIP 字段只能表示 1980-01-01 至 2107-12-31，超出则夹取）
// ─────────────────────────────────────────────────────────────────────────────

function dosDateTime(ms) {
  const d = new Date(ms)
  const year = Math.min(2107, Math.max(1980, d.getUTCFullYear()))
  const dosTime = ((d.getUTCHours() & 0x1f) << 11) | ((d.getUTCMinutes() & 0x3f) << 5) | ((d.getUTCSeconds() >> 1) & 0x1f)
  const dosDate = ((year - 1980) << 9) | ((d.getUTCMonth() + 1) << 5) | (d.getUTCDate() & 0x1f)
  return { dosTime, dosDate }
}

function checkPath(p) {
  if (typeof p !== 'string' || !p || p.startsWith('/') || p.includes('\\') || p.split('/').includes('..')) {
    throw new Error(`非法 ZIP 内路径: ${JSON.stringify(p)}（须为相对 posix 路径）`)
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 写出
// ─────────────────────────────────────────────────────────────────────────────

/**
 * @param entries [{ path, data: Buffer, mode = 0o644, dir = false }]
 *   path 为 ZIP 内相对路径（目录以 / 结尾）；mode 为 Unix 权限位（.sh 用 0o755）。
 * @param outPath 写出路径（父目录须已存在；存在则覆盖）。
 * @param mtimeMs 所有条目的统一时间戳（毫秒，UTC）。可重复构建靠它固定。
 * @returns {{ entries: number, bytes: number }}
 */
export function writeZip(entries, outPath, { mtimeMs = 0 } = {}) {
  const sorted = [...entries].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0))
  const { dosTime, dosDate } = dosDateTime(mtimeMs)
  const fd = openSync(outPath, 'w')
  let offset = 0
  const central = []

  const write = (buf) => {
    writeSync(fd, buf)
    offset += buf.length
    if (offset > 0xffffffff) {
      closeSync(fd)
      throw new Error('ZIP 总大小超过 4 GiB（不支持 zip64），请拆包')
    }
  }

  try {
    for (const entry of sorted) {
      checkPath(entry.path)
      const name = Buffer.from(entry.path, 'utf8')
      const data = entry.dir ? Buffer.alloc(0) : entry.data
      if (!Buffer.isBuffer(data)) throw new Error(`条目 ${entry.path} 的 data 不是 Buffer`)
      if (data.length > 0xffffffff) throw new Error(`文件过大（> 4 GiB）: ${entry.path}`)
      const crc = crc32(data)
      // 小文件或已压缩内容用存储（stored），否则 deflate；哪个小用哪个。
      let method = 0
      let payload = data
      if (!entry.dir && data.length > 0) {
        const deflated = deflateRawSync(data, { level: 9 })
        if (deflated.length < data.length) {
          method = 8
          payload = deflated
        }
      }
      const localHeader = Buffer.alloc(30)
      localHeader.writeUInt32LE(0x04034b50, 0) // local file header signature
      localHeader.writeUInt16LE(20, 4) // version needed to extract
      localHeader.writeUInt16LE(0x0800, 6) // 通用标志：bit 11 = 文件名 UTF-8
      localHeader.writeUInt16LE(method, 8)
      localHeader.writeUInt16LE(dosTime, 10)
      localHeader.writeUInt16LE(dosDate, 12)
      localHeader.writeUInt32LE(crc, 14)
      localHeader.writeUInt32LE(payload.length, 18) // compressed size
      localHeader.writeUInt32LE(data.length, 22) // uncompressed size
      localHeader.writeUInt16LE(name.length, 26)
      localHeader.writeUInt16LE(0, 28) // extra field length
      const headerOffset = offset
      write(localHeader)
      write(name)
      write(payload)
      central.push({ name, method, crc, compressedSize: payload.length, size: data.length, headerOffset, mode: entry.mode ?? 0o644, dir: !!entry.dir })
    }

    const centralStart = offset
    for (const c of central) {
      const header = Buffer.alloc(46)
      header.writeUInt32LE(0x02014b50, 0) // central file header signature
      header.writeUInt16LE((3 << 8) | 20, 4) // version made by: Unix + 2.0
      header.writeUInt16LE(20, 6) // version needed to extract
      header.writeUInt16LE(0x0800, 8)
      header.writeUInt16LE(c.method, 10)
      header.writeUInt16LE(dosTime, 12)
      header.writeUInt16LE(dosDate, 14)
      header.writeUInt32LE(c.crc, 16)
      header.writeUInt32LE(c.compressedSize, 20)
      header.writeUInt32LE(c.size, 24)
      header.writeUInt16LE(c.name.length, 28)
      header.writeUInt16LE(0, 30) // extra field length
      header.writeUInt16LE(0, 32) // comment length
      header.writeUInt16LE(0, 34) // disk number start
      header.writeUInt16LE(0, 36) // internal attributes
      // 外部属性：高 16 位为 Unix 权限位；目录再加 DOS 目录位。
      header.writeUInt32LE(((c.mode & 0xffff) << 16) | (c.dir ? 0x10 : 0), 38)
      header.writeUInt32LE(c.headerOffset, 42)
      write(header)
      write(c.name)
    }
    const centralSize = offset - centralStart

    const eocd = Buffer.alloc(22)
    eocd.writeUInt32LE(0x06054b50, 0) // end of central directory signature
    eocd.writeUInt16LE(0, 4) // disk number
    eocd.writeUInt16LE(0, 6) // central directory start disk
    eocd.writeUInt16LE(central.length, 8) // entries on this disk
    eocd.writeUInt16LE(central.length, 10) // total entries
    eocd.writeUInt32LE(centralSize, 12)
    eocd.writeUInt32LE(centralStart, 16)
    eocd.writeUInt16LE(0, 20) // comment length
    write(eocd)
  } finally {
    closeSync(fd)
  }
  return { entries: sorted.length, bytes: offset }
}
