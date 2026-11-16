import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Maximize, Minus, Plus, RefreshCcw, X } from 'lucide-react';
import { SITES, siteColor, type Site } from './topologyData';

// 原图查看器：平移/缩放 + 全部站点点位叠加层 + 屏幕坐标高亮（十字线+站名）+ 右下角全图缩略导航。
// 点位叠加层（用户修正版的方案）：553 个站点按千分比坐标直接叠在物理原图上、按类型着色，
// 半径按当前缩放反向补偿（屏幕大小恒定），点位可点击选中——站点就在原图里，不用去另一张图找。
export interface FocusReq {
  site: Site;
  mode: 'whole' | 'zoom';
  n: number;
}

interface PhysicalMapProps {
  srcImg: string;
  selected: Site | null;
  showSite: boolean;
  sitesVisible: boolean;
  onSelectSite: (s: Site) => void;
  focus: FocusReq | null;
  sectorLabel: string;
  mmTitle: string;
  zoomInLabel: string;
  zoomOutLabel: string;
  resetLabel: string;
  fullLabel: string;
  onClear: () => void;
}

export default function PhysicalMap({
  srcImg,
  selected,
  showSite,
  sitesVisible,
  onSelectSite,
  focus,
  sectorLabel,
  mmTitle,
  zoomInLabel,
  zoomOutLabel,
  resetLabel,
  fullLabel,
  onClear,
}: PhysicalMapProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [nat, setNat] = useState<{ w: number; h: number } | null>(null);
  const [tf, setTf] = useState({ k: 0.2, x: 0, y: 0 });
  const dragRef = useRef<{ mx: number; my: number; x: number; y: number } | null>(null);

  const fit = useCallback(() => {
    const box = boxRef.current;
    if (!box || !nat) return;
    const k = Math.min(box.clientWidth / nat.w, box.clientHeight / nat.h) * 0.98;
    setTf({ k, x: (box.clientWidth - nat.w * k) / 2, y: (box.clientHeight - nat.h * k) / 2 });
  }, [nat]);

  useEffect(() => {
    setNat(null);
  }, [srcImg]);

  useEffect(() => {
    fit();
  }, [fit]);

  // 定位请求：whole=回到全图看位置；zoom=拉近看周边链路
  useEffect(() => {
    if (!focus || !nat || !boxRef.current) return;
    const box = boxRef.current;
    const s = focus.site;
    if (focus.mode === 'whole') {
      fit();
      return;
    }
    const fk = Math.min(box.clientWidth / nat.w, box.clientHeight / nat.h) * 6;
    setTf({
      k: fk,
      x: box.clientWidth / 2 - (s.x / 1000) * nat.w * fk,
      y: box.clientHeight / 2 - (s.y / 1000) * nat.h * fk,
    });
  }, [focus, nat, fit]);

  const zoomBy = (f: number) => {
    const box = boxRef.current;
    if (!box) return;
    const cx = box.clientWidth / 2;
    const cy = box.clientHeight / 2;
    setTf((p) => ({ k: p.k * f, x: cx - (cx - p.x) * f, y: cy - (cy - p.y) * f }));
  };

  const hl =
    showSite && selected && nat
      ? {
          x: tf.x + (selected.x / 1000) * nat.w * tf.k,
          y: tf.y + (selected.y / 1000) * nat.h * tf.k,
        }
      : null;

  const mmView =
    nat && boxRef.current
      ? {
          left: Math.max(0, -tf.x / tf.k / nat.w) * 100,
          top: Math.max(0, -tf.y / tf.k / nat.h) * 100,
          width: Math.min(100, boxRef.current.clientWidth / tf.k / nat.w) * 100,
          height: Math.min(100, boxRef.current.clientHeight / tf.k / nat.h) * 100,
        }
      : null;

  return (
    <div
      ref={boxRef}
      className="relative h-[62vh] min-h-[420px] overflow-hidden bg-white dark:bg-slate-900 cursor-grab active:cursor-grabbing touch-none select-none"
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
        <img
          src={srcImg}
          alt=""
          draggable={false}
          onLoad={(e) => setNat({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
        />
        {showSite && sitesVisible && nat && (
          <svg
            className="absolute left-0 top-0"
            width={nat.w}
            height={nat.h}
            viewBox={`0 0 ${nat.w} ${nat.h}`}
            style={{ overflow: 'visible', pointerEvents: 'none' }}
          >
            {SITES.map((s) => {
              const isSel = selected === s;
              return (
                <circle
                  key={s.name + s.x + s.y}
                  cx={(s.x / 1000) * nat.w}
                  cy={(s.y / 1000) * nat.h}
                  r={(isSel ? 6.2 : 3.2) / Math.max(tf.k, 0.04)}
                  fill={siteColor(s)}
                  stroke={isSel ? '#ff7900' : '#fff'}
                  strokeWidth={isSel ? 3 : 1.6}
                  vectorEffect="non-scaling-stroke"
                  opacity={isSel ? 1 : 0.9}
                  style={{ pointerEvents: 'all', cursor: 'pointer' }}
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelectSite(s);
                  }}
                >
                  <title>{`${s.name}${s.is5g ? ' · 5G' : ''}${s.isp ? ' · ISP' : ''}`}</title>
                </circle>
              );
            })}
          </svg>
        )}
      </div>

      {hl && selected && (
        <div className="absolute inset-0 pointer-events-none z-[5]">
          <div className="absolute top-0 bottom-0 border-l-2 border-dashed border-[#ff5a00]/75" style={{ left: hl.x }} />
          <div className="absolute left-0 right-0 border-t-2 border-dashed border-[#ff5a00]/75" style={{ top: hl.y }} />
          <div
            className="absolute w-11 h-11 -ml-[22px] -mt-[22px] rounded-full border-[3px] border-[#ff5a00] bg-[#ff7900]/20 animate-pulse"
            style={{ left: hl.x, top: hl.y }}
          />
          <div
            className="absolute w-2.5 h-2.5 -ml-[5px] -mt-[5px] rounded-full bg-[#ff5a00] border-2 border-white"
            style={{ left: hl.x, top: hl.y }}
          />
          <div
            className="absolute pointer-events-auto bg-slate-900 text-white text-[13px] font-semibold px-2.5 py-1.5 rounded-lg shadow-lg whitespace-nowrap"
            style={{
              left: Math.min(Math.max(6, hl.x + 18), Math.max(6, (boxRef.current?.clientWidth || 300) - 170)),
              top: Math.min(Math.max(6, hl.y - 58), Math.max(6, (boxRef.current?.clientHeight || 300) - 60)),
            }}
          >
            {selected.name}
            <button type="button" onClick={onClear} className="ml-2 text-[#ffb37e] font-bold">
              <X className="w-3.5 h-3.5 inline" />
            </button>
            <span className="block font-normal text-[#ffb37e] text-[11px] mt-0.5">{sectorLabel}</span>
          </div>
        </div>
      )}

      <div className="absolute right-2 top-2 flex gap-1.5 z-10">
        <button type="button" title={zoomInLabel} onClick={() => zoomBy(1.25)} className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 shadow-sm">
          <Plus className="w-4 h-4" />
        </button>
        <button type="button" title={zoomOutLabel} onClick={() => zoomBy(0.8)} className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 shadow-sm">
          <Minus className="w-4 h-4" />
        </button>
        <button type="button" title={resetLabel} onClick={fit} className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 shadow-sm">
          <RefreshCcw className="w-4 h-4" />
        </button>
        <button type="button" title={fullLabel} onClick={() => boxRef.current?.requestFullscreen?.()} className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 shadow-sm">
          <Maximize className="w-4 h-4" />
        </button>
      </div>

      {showSite && (
        <div
          className="absolute right-2.5 bottom-2.5 w-[190px] bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg overflow-hidden z-[6] shadow-lg cursor-pointer"
          title={mmTitle}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            if (!nat || !boxRef.current) return;
            const body = e.currentTarget.querySelector('.mm-body');
            if (!body) return;
            const r = body.getBoundingClientRect();
            const px = (e.clientX - r.left) / r.width;
            const py = (e.clientY - r.top) / r.height;
            setTf((p) => ({
              ...p,
              x: boxRef.current!.clientWidth / 2 - px * nat.w * p.k,
              y: boxRef.current!.clientHeight / 2 - py * nat.h * p.k,
            }));
          }}
        >
          <div className="text-[11px] text-slate-500 dark:text-slate-400 px-2 py-1 border-b border-slate-100 dark:border-slate-700">
            {mmTitle}
          </div>
          <div className="mm-body relative leading-none">
            <img src={srcImg} alt="" className="w-full block" draggable={false} />
            {mmView && (
              <div
                className="absolute border-2 border-[#ff5a00] bg-[#ff7900]/15"
                style={{ left: `${mmView.left}%`, top: `${mmView.top}%`, width: `${mmView.width}%`, height: `${mmView.height}%` }}
              />
            )}
            {selected && (
              <div
                className="absolute w-[9px] h-[9px] -ml-[4px] -mt-[4px] rounded-full bg-[#ff5a00] border-2 border-white"
                style={{ left: `${selected.x / 10}%`, top: `${selected.y / 10}%` }}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
