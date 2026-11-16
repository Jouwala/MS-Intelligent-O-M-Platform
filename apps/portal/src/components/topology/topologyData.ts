// 网络拓扑视图的静态数据与纯计算（站点坐标来自 Orange Liberia 拓扑 PDF 文字层提取）
// 站点原始数据在 src/data/orangeTopologySites.json：n=站名，
// x/y=原图相对坐标（**千分比 0-1000**，不是百分比——第一版按百分比用曾导致定位整体错位 10 倍），
// s=区域代码（见 SECTORS），g=原图旁有 5G 标记，i=站名含 ISP。
import rawSites from '../../data/orangeTopologySites.json';

export interface Site {
  name: string;
  x: number; // 原图相对坐标（千分比 0-1000）
  y: number;
  sector: string; // 区域代码
  is5g: boolean;
  isp: boolean;
  core: boolean;
  sx: number; // 分布图坐标（1000 x 863 画布，与千分比同尺）
  sy: number;
}

interface RawSite {
  n: string;
  x: number;
  y: number;
  s: string;
  g?: number;
  i?: number;
}

// 骨干核心节点（第 2 页逻辑图 IPRAN/DWDM 节点），按站名归一化匹配
const CORE_NAMES = [
  'orange',
  'kakata city',
  'gbarnga 2',
  'sanniquellie 2',
  'saniquellie 2',
  'epcm',
  'danane',
  'r.i.a',
  'buchanan2',
  'botota',
];

const norm = (s: string) => s.toLowerCase().replace(/\s+/g, '');

export const SITES: Site[] = (rawSites as RawSite[]).map((r) => ({
  name: r.n,
  x: r.x,
  y: r.y,
  sector: r.s,
  is5g: !!r.g,
  isp: !!r.i,
  core: CORE_NAMES.includes(norm(r.n)) || CORE_NAMES.includes(r.n.toLowerCase()),
  sx: r.x,
  sy: r.y * 0.863,
}));

// 区域代码（与 orangeTopologySites.json 的 s 字段、i18n 的 topo.sector.* 键一一对应）
export const SECTORS = [
  'lofa',
  'bomi',
  'caldwell',
  'monrovia',
  'kakata',
  'nimba',
  'buchanan',
  'southeast',
  'corridor',
];

// 分布图按区域着色（与单文件版口径一致）
export const SECTOR_COLORS: Record<string, string> = {
  lofa: '#7b2d8b',
  bomi: '#b7791f',
  caldwell: '#2563eb',
  monrovia: '#d33',
  kakata: '#1a9e6b',
  nimba: '#0e7490',
  buchanan: '#ff7900',
  southeast: '#9333ea',
  corridor: '#666',
};

// 原图点位叠加层按类型着色（与单文件版口径一致）：骨干核心紫、ISP 蓝、5G 红、普通深灰
export function siteColor(s: Site): string {
  if (s.core) return '#7c3aed';
  if (s.isp) return '#0ea5e9';
  if (s.is5g) return '#ef4444';
  return '#263238';
}

// ---- 骨干示意（第 2 页逻辑图人工梳理，仅核心层） ----
export interface CoreNode {
  id: string;
  label: string;
  x: number;
  y: number;
  site?: string; // 对应提取名单中的站名（可点击查看）
}

export const CORE_NODES: CoreNode[] = [
  { id: 'hq', label: 'HQ MSC1', x: 70, y: 300 },
  { id: 'odc', label: 'ODC MSC2', x: 70, y: 470 },
  { id: 'orange', label: 'Orange (HQ)', x: 230, y: 300, site: 'Orange' },
  { id: 'kakata', label: 'Kakata City', x: 420, y: 190, site: 'Kakata city' },
  { id: 'ria', label: 'R.I.A', x: 420, y: 410, site: 'R.I.A' },
  { id: 'buch', label: 'Buchanan 2', x: 640, y: 410, site: 'Buchanan2' },
  { id: 'botota', label: 'Botota', x: 700, y: 270, site: 'Botota' },
  { id: 'gbarnga', label: 'Gbarnga 2', x: 620, y: 190, site: 'Gbarnga 2' },
  { id: 'sanniq', label: 'Sanniquellie 2', x: 830, y: 190, site: 'Saniquellie 2' },
  { id: 'tokadeh', label: 'EPCM (Tokadeh)', x: 900, y: 90, site: 'EPCM' },
  { id: 'danane', label: 'Danane', x: 960, y: 270, site: 'Danane' },
  { id: 'yekepa', label: 'AML Data Center (Yekepa)', x: 985, y: 150 },
  { id: 'amltok', label: 'AML Data Center (Tokadeh)', x: 800, y: 60 },
];

export type CoreLinkType = 'exist' | 'dwdm' | 'rail' | 'p25a';

export const CORE_LINKS: [string, string, CoreLinkType][] = [
  ['hq', 'kakata', 'exist'],
  ['hq', 'ria', 'exist'],
  ['hq', 'odc', 'exist'],
  ['kakata', 'ria', 'exist'],
  ['ria', 'buch', 'exist'],
  ['buch', 'botota', 'exist'],
  ['gbarnga', 'sanniq', 'exist'],
  ['sanniq', 'danane', 'exist'],
  ['kakata', 'gbarnga', 'dwdm'],
  ['orange', 'kakata', 'dwdm'],
  ['orange', 'ria', 'dwdm'],
  ['gbarnga', 'botota', 'dwdm'],
  ['sanniq', 'tokadeh', 'dwdm'],
  ['botota', 'gbarnga', 'rail'],
  ['sanniq', 'tokadeh', 'rail'],
  ['tokadeh', 'danane', 'p25a'],
  ['tokadeh', 'amltok', 'dwdm'],
  ['sanniq', 'yekepa', 'dwdm'],
];

export const CORE_LINK_COLORS: Record<CoreLinkType, string> = {
  exist: '#f0a500',
  dwdm: '#7b2d8b',
  rail: '#1596d2',
  p25a: '#1a9e6b',
};
