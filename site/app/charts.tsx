"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { monthYear, pct, tick, type Snapshot } from "./snapshot";

/* Hand-rolled SVG charts: no chart library, so nothing leaves the page but the snapshot.
   Geometry is computed in pixel space from the element's measured width, so text and
   dots keep their shape at any size. */

type Pt = [number, number];
type Series = { name: string; points: Pt[]; color: string; width?: number };
type Range = [number, number] | null;

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

function drawdown(points: Pt[]): Pt[] {
  let peak = -Infinity;
  return points.map(([x, y]) => { peak = Math.max(peak, y); return [x, Math.min(0, y - peak)] as Pt; });
}

function Bars({ rows, fmt = (v: number) => pct(v, 2) }: { rows: { label: string; value: number; note?: string; cls?: string }[]; fmt?: (v: number) => string }) {
  const max = Math.max(...rows.map((r) => Math.abs(r.value)), 0.01);
  return (
    <div className="bars">
      {rows.map((r) => (
        <div key={r.label} className="bar-row">
          <div className={`bar-label ${r.cls ?? ""}`}>{r.label}</div>
          <div className="bar-track">
            <div className="bar-zero" />
            <div className={`bar-fill ${r.value >= 0 ? "pos" : "neg"}`} style={{ width: `${(Math.abs(r.value) / max) * 50}%`, [r.value >= 0 ? "left" : "right"]: "50%" } as React.CSSProperties} />
          </div>
          <div className={`bar-value ${r.value >= 0 ? "up" : "down"}`}>{fmt(r.value)}</div>
          {r.note && <div className="bar-note muted">{r.note}</div>}
        </div>
      ))}
    </div>
  );
}

function Heatmap({ history, assets }: { history: Snapshot["history"]; assets: string[] }) {
  // One day at a time by default (a week of cycles does not fit the panel); "all" scrolls.
  const days = useMemo(() => Array.from(new Set(history.map((h) => h.ts.slice(0, 10)))), [history]);
  const [day, setDay] = useState<string>("latest");
  if (!history.length) return <p className="muted">No cycles in the last week.</p>;
  const picked = day === "latest" ? days[days.length - 1] : day;
  const rows = picked === "all" ? history : history.filter((h) => h.ts.slice(0, 10) === picked);
  const all = picked === "all";
  const color = (v: number | null) => {
    if (v == null) return "transparent";
    const a = Math.min(1, Math.abs(v)) * 0.85 + 0.1;
    return v >= 0 ? `rgba(182,255,0,${a})` : `rgba(255,92,92,${a})`;
  };
  const label = (d: string) => new Date(`${d}T00:00:00Z`).toUTCString().slice(5, 11);
  const stamp = (ts: string) => (all ? new Date(ts).toUTCString().slice(5, 11) : new Date(ts).toUTCString().slice(17, 22));
  return (
    <>
      <div className="heat-days">
        <span className="toggle">
          {days.map((d) => <button key={d} className={picked === d ? "on" : ""} onClick={() => setDay(d)}>{label(d)}</button>)}
          <button className={all ? "on" : ""} onClick={() => setDay("all")}>all</button>
        </span>
        <span className="muted small">{rows.length} cycle{rows.length === 1 ? "" : "s"}{all ? " · scroll sideways" : " · times in UTC"}</span>
      </div>
      <div className="heat-scroll">
        <div className={`heat ${all ? "wide" : ""}`} style={{ gridTemplateColumns: all ? `56px repeat(${rows.length}, 28px)` : `56px repeat(${rows.length}, minmax(28px, 1fr))` }}>
          <div />
          {rows.map((h, i) => <div key={i} className="heat-x muted">{all ? (i === 0 || h.ts.slice(0, 10) !== rows[i - 1].ts.slice(0, 10) ? stamp(h.ts) : "") : stamp(h.ts)}</div>)}
          {assets.map((a) => (
            <div key={a} style={{ display: "contents" }}>
              <div className="heat-y muted">{a === "SPX6900" ? "SPX" : a}</div>
              {rows.map((h, i) => {
                const v = h.scores[a] ?? null, d = h.directions[a] ?? "flat";
                return <div key={i} className={`heat-cell ${d}`} style={{ background: color(v) }} title={`${new Date(h.ts).toUTCString().slice(5, 22)} UTC · ${a} · ${d}${v == null ? "" : ` · ${v > 0 ? "+" : ""}${v.toFixed(2)}`}`}>{d === "long" ? "▲" : d === "short" ? "▼" : ""}</div>;
              })}
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

const RANGES: { label: string; ms: number | null }[] = [
  { label: "24h", ms: 86400e3 }, { label: "7d", ms: 7 * 86400e3 }, { label: "30d", ms: 30 * 86400e3 },
  { label: "90d", ms: 90 * 86400e3 }, { label: "all", ms: null },
];

/** 24h / 7d / 30d / 90d / all. Ranges longer than the record are hidden. */
function RangePicker({ span, value, onChange }: { span: number; value: number | null; onChange: (ms: number | null) => void }) {
  const shown = RANGES.filter((r) => r.ms === null || r.ms < span * 1.1 || r.ms === 86400e3);
  if (shown.length <= 2) return null;
  return (
    <span className="toggle">
      {shown.map((r) => <button key={r.label} className={value === r.ms ? "on" : ""} onClick={() => onChange(r.ms)}>{r.label}</button>)}
    </span>
  );
}

/** The equity series to draw for a window: the dense last-7-days series when it fits. */
function pickEquity(p: Snapshot["performance"], range: Range): Pt[] {
  const full = p.equity_curve as Pt[];
  const recent = (p.equity_recent ?? []) as Pt[];
  return range && recent.length > 1 && range[0] >= recent[0][0] ? recent : full;
}

function windowOf(points: Pt[], ms: number | null): Range {
  if (!ms || points.length < 2) return null;
  const end = points[points.length - 1][0];
  return [Math.max(points[0][0], end - ms), end];
}

/** One dot per closing moment. Trades that closed together (a sleeve stop, a kill switch)
    share one dot, green or red by their combined result, so the colour no longer depends
    on which of them happens to be drawn on top. */
/** Dots for closed trades. Trades that would land on the same pixel are one dot coloured
    by their combined result (2026-10-18: a losing SPX dot hid under a winning group twelve
    minutes away on a week-wide chart), so the bucket follows the visible span. */
function tradeMarkers(trades: Snapshot["closed_trades"], equity: Pt[], spanMs = 0) {
  const bucket = Math.max(60000, spanMs / 220);
  const groups = new Map<number, Snapshot["closed_trades"]>();
  for (const t of [...trades].sort((a, b) => Date.parse(a.closed_at) - Date.parse(b.closed_at))) {
    const ms = Date.parse(t.closed_at);
    const key = Math.round(ms / bucket) * bucket;
    groups.set(key, [...(groups.get(key) ?? []), t]);
  }
  return Array.from(groups.entries()).map(([key, ts]) => {
    const x = ts.reduce((a, t) => a + Date.parse(t.closed_at), 0) / ts.length;
    void key;
    const sum = ts.reduce((a, t) => a + t.margin_pct, 0);
    const lines = ts.map((t) => `${tick(t.cashtag)} ${t.direction} ${pct(t.margin_pct)}`);
    return {
      x,
      y: nearest(equity, x)?.[1] ?? 0,
      color: sum >= 0 ? "var(--up)" : "var(--down)",
      label: ts.length === 1 ? lines[0] : `${ts.length} trades closed together (${pct(sum)} combined): ${lines.join(" · ")}`,
    };
  });
}

/** The orange curve on the front page: hover readout, trade dots, range picker, tap or button to expand. */
export function EquityChart({ s, onExpand }: { s: Snapshot; onExpand: () => void }) {
  const p = s.performance;
  const [ms, setMs] = useState<number | null>(null);
  const full = p.equity_curve as Pt[];
  const range = windowOf(full, ms);
  const equity = pickEquity(p, range);
  const span = full.length > 1 ? full[full.length - 1][0] - full[0][0] : 0;
  const shownSpan = range ? range[1] - range[0] : span;
  const markers = tradeMarkers(s.closed_trades, equity, shownSpan);
  const visible = markers.filter((m) => !range || (m.x >= range[0] && m.x <= range[1]));
  return (
    <div className="chart-wrap">
      <div className="chart-tools"><RangePicker span={span} value={ms} onChange={setMs} /></div>
      <LineChart series={[{ name: "dorkbot", points: equity, color: "var(--accent)", width: 1.8 }]} markers={visible.length <= 40 ? markers : []} range={range} height={220} />
      <button className="expand" onClick={onExpand} aria-label="open the detailed charts">details ⤢</button>
    </div>
  );
}

export function DetailOverlay({ s, onClose }: { s: Snapshot; onClose: () => void }) {
  const [range, setRange] = useState<Range>(null);
  const p = s.performance;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = prev; };
  }, [onClose]);
  const full = p.equity_curve as Pt[];
  const equity = pickEquity(p, range);  // a zoom inside the last week uses the dense series
  const span = full.length > 1 ? full[full.length - 1][0] - full[0][0] : 0;
  const picked = range && range[1] === full[full.length - 1]?.[0] ? range[1] - range[0] : null;
  const series: Series[] = [
    { name: "dorkbot", points: equity, color: "var(--accent)", width: 2 },
    ...(p.btc_curve.length > 1 ? [{ name: "BTC hold", points: p.btc_curve as Pt[], color: "#8f958f" }] : []),
    ...(p.basket_curve.length > 1 ? [{ name: "basket", points: p.basket_curve as Pt[], color: "#4f8fe6" }] : []),
  ];
  const markers = tradeMarkers(s.closed_trades, equity, equity.length > 1 ? equity[equity.length - 1][0] - equity[0][0] : 0);
  const dd = drawdown(full);
  const inRange = (ms: string) => !range || (Date.parse(ms) >= range[0] && Date.parse(ms) <= range[1]);
  const trades = s.closed_trades.filter((t) => inRange(t.closed_at)).slice().reverse();
  const setR = useCallback((r: Range) => setRange(r), []);
  return (
    <div className="overlay" role="dialog" aria-modal="true" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="overlay-panel">
        <div className="overlay-head">
          <h2>Details since {monthYear(p.since)}</h2>
          <RangePicker span={span} value={picked && RANGES.some((r) => r.ms === picked) ? picked : range ? -1 : null} onChange={(ms) => setRange(windowOf(full, ms))} />
          <div className="muted small">or drag to zoom · double-click resets{range && <> · <button className="link" onClick={() => setRange(null)}>reset</button></>}</div>
          <button className="close" onClick={onClose} aria-label="close">✕</button>
        </div>
        <h3>Equity vs BTC buy &amp; hold vs equal-weight basket</h3>
        <LineChart series={series} markers={markers} range={range} onRange={setR} height={300} />
        <div className="legend">
          <span><span className="swatch" style={{ background: "var(--accent)" }} />dorkbot {pct(p.bot_pct, 2)}</span>
          <span><span className="swatch" style={{ background: "#8f958f" }} />BTC hold {pct(p.btc_hold_pct, 2)}</span>
          <span><span className="swatch" style={{ background: "#4f8fe6" }} />basket {pct(p.basket_pct, 2)}</span>
          <span><span className="swatch" style={{ background: "var(--up)" }} />win · <span className="swatch" style={{ background: "var(--down)" }} />loss</span>
        </div>
        <h3>Drawdown from peak <span className="muted small">max {pct(p.drawdown_pct, 2)}</span></h3>
        <LineChart series={[{ name: "drawdown", points: dd, color: "var(--down)" }]} range={range} onRange={setR} height={160} area />
        <div className="two-col">
          <div>
            <h3>By asset <span className="muted small">realized, % of starting capital</span></h3>
            <Bars rows={s.by_asset.map((a) => ({ label: tick(a.cashtag), value: a.pct, note: a.trades ? `${a.wins}/${a.trades} won` : "no trades yet" }))} />
          </div>
          <div>
            <h3>Trades <span className="muted small">margin %, newest last{range ? ", in range" : ""}</span></h3>
            {trades.length ? <Bars rows={trades.map((t, i) => ({ label: `${tick(t.cashtag)} ${t.direction === "long" ? "▲" : "▼"}`, value: t.margin_pct, note: `${t.holding}${t.paper ? " · paper" : ""}`, cls: t.direction }))} /> : <p className="muted">No closed trades here.</p>}
          </div>
        </div>
        <h3>Consensus per cycle, last 7 days <span className="muted small">green = bullish, red = bearish, arrow = direction taken</span></h3>
        <Heatmap history={s.history} assets={s.assets.map((a) => a.asset)} />
      </div>
    </div>
  );
}
