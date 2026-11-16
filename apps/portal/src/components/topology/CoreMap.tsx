import React from 'react';
import { CORE_LINK_COLORS, CORE_LINKS, CORE_NODES, SITES, type Site } from './topologyData';

// 骨干示意：按拓扑 PDF 第 2 页逻辑图人工梳理的核心 IPRAN/DWDM 骨干（仅核心层）。
interface CoreMapProps {
  onSelect: (s: Site) => void;
  legend: { exist: string; rail: string; p25a: string; dwdm: string };
  distanceNote: string;
}

export default function CoreMap({ onSelect, legend, distanceNote }: CoreMapProps) {
  const byId = Object.fromEntries(CORE_NODES.map((c) => [c.id, c]));
  return (
    <svg width={1040} height={540} viewBox="0 0 1040 540" className="block bg-white">
      {CORE_LINKS.map(([a, b, t], i) => {
        const A = byId[a];
        const B = byId[b];
        return (
          <line
            key={i}
            x1={A.x}
            y1={A.y}
            x2={B.x}
            y2={B.y}
            stroke={CORE_LINK_COLORS[t]}
            strokeWidth={t === 'exist' ? 5 : 3}
            strokeDasharray={t === 'dwdm' ? '7 4' : undefined}
            opacity={0.85}
          />
        );
      })}
      {CORE_NODES.map((c) => (
        <g
          key={c.id}
          className={c.site ? 'cursor-pointer' : undefined}
          onClick={(e) => {
            if (!c.site) return;
            e.stopPropagation();
            const found = SITES.find((s) => s.name === c.site);
            if (found) onSelect(found);
          }}
        >
          <circle cx={c.x} cy={c.y} r={17} fill="#29a7e1" stroke="#fff" strokeWidth={2} />
          <text x={c.x} y={c.y + 5} textAnchor="middle" fontSize={13} fill="#fff" fontWeight={700}>
            ✕
          </text>
          <text x={c.x} y={c.y + 32} textAnchor="middle" fontSize={12} fill="#111">
            {c.label}
          </text>
        </g>
      ))}
      <g fontSize={12}>
        <rect x={60} y={505} width={16} height={5} fill={CORE_LINK_COLORS.exist} />
        <text x={82} y={512}>{legend.exist}</text>
        <rect x={210} y={505} width={16} height={5} fill={CORE_LINK_COLORS.rail} />
        <text x={232} y={512}>{legend.rail}</text>
        <rect x={390} y={505} width={16} height={5} fill={CORE_LINK_COLORS.p25a} />
        <text x={412} y={512}>{legend.p25a}</text>
        <rect x={570} y={505} width={16} height={5} fill={CORE_LINK_COLORS.dwdm} />
        <text x={592} y={512}>{legend.dwdm}</text>
        <text x={770} y={512} fill="#555">
          {distanceNote}
        </text>
      </g>
    </svg>
  );
}
