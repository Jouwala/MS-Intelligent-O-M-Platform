import React from 'react';
import { SECTOR_COLORS, SITES, type Site } from './topologyData';

// 站点分布：每个点 = 一个提取到的站点，位置与原图版面一致（坐标为原图千分比），按区域着色。
interface ScatterMapProps {
  selected: Site | null;
  onSelect: (s: Site) => void;
}

export default function ScatterMap({ selected, onSelect }: ScatterMapProps) {
  return (
    <svg width={1000} height={863} viewBox="0 0 1000 863" className="block bg-white">
      <rect width={1000} height={863} fill="#fff" />
      {SITES.map((s) => (
        <circle
          key={s.name + s.x + s.y}
          cx={s.sx}
          cy={s.sy}
          r={s.core ? 5 : 3}
          fill={s.core ? '#111' : SECTOR_COLORS[s.sector] || '#888'}
          opacity={0.85}
          className="cursor-pointer"
          onClick={(e) => {
            e.stopPropagation();
            onSelect(s);
          }}
        >
          <title>{s.name}</title>
        </circle>
      ))}
      {selected && (
        <g pointerEvents="none">
          <circle cx={selected.sx} cy={selected.sy} r={11} fill="none" stroke="#ff5a00" strokeWidth={3} />
          <circle cx={selected.sx} cy={selected.sy} r={4} fill="#ff5a00" stroke="#fff" strokeWidth={2} />
        </g>
      )}
    </svg>
  );
}
