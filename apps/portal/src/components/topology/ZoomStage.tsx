import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Maximize, Minus, Plus, RefreshCcw } from 'lucide-react';

// 通用平移/缩放舞台：子内容按 baseW x baseH 的固有尺寸渲染，舞台负责 fit、滚轮缩放、拖动平移。
// 拓扑的定位图/骨干示意（SVG）与原图（img）都走这一层，交互保持一致。
interface ZoomStageProps {
  baseW: number;
  baseH: number;
  heightClass?: string;
  zoomInLabel: string;
  zoomOutLabel: string;
  resetLabel: string;
  fullLabel: string;
  children: React.ReactNode;
}

export default function ZoomStage({
  baseW,
  baseH,
  heightClass = 'h-[62vh] min-h-[420px]',
  zoomInLabel,
  zoomOutLabel,
  resetLabel,
  fullLabel,
  children,
}: ZoomStageProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [tf, setTf] = useState({ k: 0.2, x: 0, y: 0 });
  const dragRef = useRef<{ mx: number; my: number; x: number; y: number } | null>(null);

  const fit = useCallback(() => {
    const box = boxRef.current;
    if (!box || !baseW || !baseH) return;
    const k = Math.min(box.clientWidth / baseW, box.clientHeight / baseH) * 0.98;
    setTf({ k, x: (box.clientWidth - baseW * k) / 2, y: (box.clientHeight - baseH * k) / 2 });
  }, [baseW, baseH]);

  useEffect(() => {
    fit();
  }, [fit]);

  const zoomBy = (f: number) => {
    const box = boxRef.current;
    if (!box) return;
    const cx = box.clientWidth / 2;
    const cy = box.clientHeight / 2;
    setTf((p) => ({ k: p.k * f, x: cx - (cx - p.x) * f, y: cy - (cy - p.y) * f }));
  };

  return (
    <div
      ref={boxRef}
      className={`relative overflow-hidden bg-white dark:bg-slate-900 cursor-grab active:cursor-grabbing touch-none select-none ${heightClass}`}
      onWheel={(e) => {
        e.preventDefault();
        const box = boxRef.current;
        if (!box) return;
        const r = box.getBoundingClientRect();
        const mx = e.clientX - r.left;
        const my = e.clientY - r.top;
        const f = e.deltaY < 0 ? 1.12 : 0.89;
        setTf((p) => ({ k: p.k * f, x: mx - (mx - p.x) * f, y: my - (my - p.y) * f }));
      }}
      onPointerDown={(e) => {
        dragRef.current = { mx: e.clientX, my: e.clientY, x: tf.x, y: tf.y };
        (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
      }}
      onPointerMove={(e) => {
        const d = dragRef.current;
        if (!d) return;
        setTf((p) => ({ ...p, x: d.x + (e.clientX - d.mx), y: d.y + (e.clientY - d.my) }));
      }}
      onPointerUp={() => {
        dragRef.current = null;
      }}
    >
      <div
        className="absolute left-0 top-0"
        style={{ transform: `translate(${tf.x}px, ${tf.y}px) scale(${tf.k})`, transformOrigin: '0 0' }}
      >
        {children}
      </div>
      <div className="absolute right-2 top-2 flex gap-1.5 z-10">
        <button
          type="button"
          title={zoomInLabel}
          onClick={() => zoomBy(1.25)}
          className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 shadow-sm"
        >
          <Plus className="w-4 h-4" />
        </button>
        <button
          type="button"
          title={zoomOutLabel}
          onClick={() => zoomBy(0.8)}
          className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 shadow-sm"
        >
          <Minus className="w-4 h-4" />
        </button>
        <button
          type="button"
          title={resetLabel}
          onClick={fit}
          className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 shadow-sm"
        >
          <RefreshCcw className="w-4 h-4" />
        </button>
        <button
          type="button"
          title={fullLabel}
          onClick={() => boxRef.current?.requestFullscreen?.()}
          className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 shadow-sm"
        >
          <Maximize className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}
