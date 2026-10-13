"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { pct } from "./api";

/* Hand-rolled SVG charts: no chart library, so nothing leaves the page but the snapshot.
   Geometry is computed in pixel space from the element's measured width, so text and
   dots keep their shape at any size. */

type Pt = [number, number];
export type Series = { name: string; points: Pt[]; color: string; width?: number };
export type Range = [number, number] | null;

const fmtDay = (ms: number) => new Date(ms).toUTCString().slice(5, 16);
const fmtTime = (ms: number) => new Date(ms).toUTCString().slice(5, 22) + " UTC";

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(600);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((es) => setW(Math.max(200, es[0].contentRect.width)));
    ro.observe(el);
    setW(Math.max(200, el.getBoundingClientRect().width));
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function clip(points: Pt[], range: Range): Pt[] {
  if (!range) return points;
  const out = points.filter((p) => p[0] >= range[0] && p[0] <= range[1]);
  return out.length >= 2 ? out : points;
}

function scales(series: Series[], w: number, h: number, pad: { l: number; r: number; t: number; b: number }) {
  const all = series.flatMap((s) => s.points);
  const xs = all.map((p) => p[0]), ys = all.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(...ys, 0), y1 = Math.max(...ys, 0);
  const span = y1 - y0 || 1;
  const sx = (x: number) => pad.l + ((x - x0) / (x1 - x0 || 1)) * (w - pad.l - pad.r);
  const sy = (y: number) => pad.t + (1 - (y - y0 - 0) / span) * (h - pad.t - pad.b);
  return { x0, x1, y0: y0, y1: y1, sx, sy };
}

function nearest(points: Pt[], x: number): Pt | null {
  if (!points.length) return null;
  let lo = 0, hi = points.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (points[m][0] < x) lo = m; else hi = m; }
  return Math.abs(points[lo][0] - x) < Math.abs(points[hi][0] - x) ? points[lo] : points[hi];
}

/** Multi-line chart with hover readout, optional markers and drag-to-zoom. */
export function LineChart({
  series, height = 260, markers = [], range, onRange, area, yFmt = (v: number) => pct(v, 1), compact,
}: {
  series: Series[]; height?: number; markers?: { x: number; y: number; color: string; label: string }[];
  range?: Range; onRange?: (r: Range) => void; area?: boolean; yFmt?: (v: number) => string; compact?: boolean;
}) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const pad = compact ? { l: 8, r: 8, t: 8, b: 8 } : { l: 48, r: 12, t: 12, b: 26 };
  const shown = useMemo(() => series.map((s) => ({ ...s, points: clip(s.points, range ?? null) })), [series, range]);
  const [hover, setHover] = useState<number | null>(null);
  const [drag, setDrag] = useState<[number, number] | null>(null);
  const S = useMemo(() => scales(shown, w, height, pad), [shown, w, height, pad.l, pad.r, pad.t, pad.b]);
  if (!series[0]?.points?.length || series[0].points.length < 2) return <p className="muted">Not enough history yet.</p>;
  const xAt = (clientX: number) => {
    const r = ref.current!.getBoundingClientRect();
    const px = Math.min(Math.max(clientX - r.left, pad.l), w - pad.r);
    return S.x0 + ((px - pad.l) / (w - pad.l - pad.r)) * (S.x1 - S.x0);
  };
  const path = (pts: Pt[]) => pts.map((p, i) => `${i ? "L" : "M"}${S.sx(p[0]).toFixed(1)},${S.sy(p[1]).toFixed(1)}`).join(" ");
  const ticks = [S.y0, (S.y0 + S.y1) / 2, S.y1].map((v) => Math.round(v * 10) / 10);
  const xticks = [S.x0, (S.x0 + S.x1) / 2, S.x1];
  const hx = hover == null ? null : hover;
  const readings = hx == null ? [] : shown.map((s) => ({ s, p: nearest(s.points, hx) }));
  return (
    <div
      ref={ref}
      className={`chart${drag ? " dragging" : ""}`}
      style={{ height }}
      onPointerMove={(e) => { const x = xAt(e.clientX); setHover(x); if (drag) setDrag([drag[0], x]); }}
      onPointerLeave={() => { setHover(null); setDrag(null); }}
      onPointerDown={(e) => { if (onRange) { (e.target as Element).closest(".chart")?.setPointerCapture?.(e.pointerId); setDrag([xAt(e.clientX), xAt(e.clientX)]); } }}
      onPointerUp={() => {
        if (drag && onRange) { const [a, b] = [Math.min(...drag), Math.max(...drag)]; if (b - a > (S.x1 - S.x0) * 0.02) onRange([a, b]); }
        setDrag(null);
      }}
      onDoubleClick={() => onRange?.(null)}
    >
      <svg width={w} height={height} aria-label="chart">
        {!compact && ticks.map((v) => (
          <g key={v}>
            <line x1={pad.l} x2={w - pad.r} y1={S.sy(v)} y2={S.sy(v)} stroke="var(--line)" strokeWidth={1} />
            <text x={pad.l - 6} y={S.sy(v) + 4} textAnchor="end" className="tick">{yFmt(v)}</text>
          </g>
        ))}
        {!compact && xticks.map((v, i) => (
          <text key={i} x={S.sx(v)} y={height - 8} textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"} className="tick">{fmtDay(v)}</text>
        ))}
        {S.y0 < 0 && S.y1 > 0 && <line x1={pad.l} x2={w - pad.r} y1={S.sy(0)} y2={S.sy(0)} stroke="var(--muted)" strokeWidth={1} strokeDasharray="3 4" />}
        {shown.map((s) => (
          <g key={s.name}>
            {area && <path d={`${path(s.points)} L${S.sx(s.points[s.points.length - 1][0]).toFixed(1)},${S.sy(0)} L${S.sx(s.points[0][0]).toFixed(1)},${S.sy(0)} Z`} fill={s.color} opacity={0.18} />}
            <path d={path(s.points)} fill="none" stroke={s.color} strokeWidth={s.width ?? 1.6} strokeLinejoin="round" />
          </g>
        ))}
        {markers.filter((m) => !range || (m.x >= range[0] && m.x <= range[1])).map((m, i) => (
          <circle key={i} cx={S.sx(m.x)} cy={S.sy(m.y)} r={4} fill={m.color} stroke="var(--bg)" strokeWidth={1.5}><title>{m.label}</title></circle>
        ))}
        {drag && <rect x={Math.min(S.sx(drag[0]), S.sx(drag[1]))} y={pad.t} width={Math.abs(S.sx(drag[1]) - S.sx(drag[0]))} height={height - pad.t - pad.b} fill="var(--accent)" opacity={0.15} />}
        {hx != null && !drag && <line x1={S.sx(hx)} x2={S.sx(hx)} y1={pad.t} y2={height - pad.b} stroke="var(--muted)" strokeWidth={1} />}
        {readings.map(({ s, p }) => p && <circle key={s.name} cx={S.sx(p[0])} cy={S.sy(p[1])} r={3.5} fill={s.color} />)}
      </svg>
      {hx != null && !drag && readings.length > 0 && readings[0].p && (
        <div className="tip" style={{ left: Math.min(S.sx(hx) + 12, w - 170) }}>
          <div className="muted">{fmtTime(readings[0].p[0])}</div>
          {readings.map(({ s, p }) => p && <div key={s.name}><span className="swatch" style={{ background: s.color }} />{s.name} <b className={p[1] >= 0 ? "up" : "down"}>{yFmt(p[1])}</b></div>)}
        </div>
      )}
    </div>
  );
}
