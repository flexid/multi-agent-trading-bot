"use client";
import { useEffect, useState } from "react";
import { cls, loadSnapshot, num, pct, type Snapshot } from "./snapshot";

function Curve({ points }: { points: number[][] }) {
  if (points.length < 2) return <p className="muted">Not enough history for a curve yet.</p>;
  const xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys, 0), y1 = Math.max(...ys, 0);
  const sx = (x: number) => ((x - x0) / (x1 - x0 || 1)) * 100, sy = (y: number) => 100 - ((y - y0) / (y1 - y0 || 1)) * 100;
  const d = points.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(2)},${sy(p[1]).toFixed(2)}`).join(" ");
  return (
    <svg className="curve" viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="equity curve">
      <line x1="0" x2="100" y1={sy(0)} y2={sy(0)} stroke="var(--line)" strokeWidth="0.5" />
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/** "long" in green, "short" in red, everywhere they appear. */
function Side({ d }: { d: string }) {
  const k = d.toLowerCase();
  return k === "long" || k === "short" ? <span className={k}>{d}</span> : <>{d}</>;
}

export default function Page() {
  const [s, setS] = useState<Snapshot | null | undefined>(undefined);
  useEffect(() => { loadSnapshot().then(setS); const t = setInterval(() => loadSnapshot().then(setS), 300000); return () => clearInterval(t); }, []);
  if (s === undefined) return <p className="muted">Loading…</p>;
  if (s === null) return <p className="muted">No data right now. The bot pushes a snapshot every five minutes; this one hasn&apos;t arrived.</p>;
  const p = s.performance, st = s.stats;
  return (
    <>
      <h1><span className={`badge ${s.mode === "live" ? "neon" : ""}`}>{s.mode}</span>{!s.heartbeat_ok && <span className="badge">stale</span>}</h1>
      <p className="muted">A bot trading its own bag on Bybit, five assets, <Side d="long" /> and <Side d="short" />. Every trade goes to <a href={`https://x.com/${s.handle}`}>@{s.handle}</a> after it fills. Paper trades are labelled paper. Updated {new Date(s.generated_at).toUTCString().slice(5, 22)} UTC.</p>
      <h2>Performance since {p.since.slice(0, 10)}</h2>
      <div className="row">
        <div className="tile"><div className="k">dorkbot</div><div className={`v ${cls(p.bot_pct)}`}>{pct(p.bot_pct, 2)}</div></div>
        <div className="tile"><div className="k">BTC buy &amp; hold</div><div className={`v ${cls(p.btc_hold_pct)}`}>{pct(p.btc_hold_pct, 2)}</div></div>
        <div className="tile"><div className="k">equal-weight basket</div><div className={`v ${cls(p.basket_pct)}`}>{pct(p.basket_pct, 2)}</div></div>
        <div className="tile"><div className="k">max drawdown</div><div className="v down">{pct(p.drawdown_pct, 2)}</div></div>
      </div>
      <Curve points={p.equity_curve} />
      <h2>Stats</h2>
      <div className="row">
        <div className="tile"><div className="k">trades</div><div className="v">{st.trades}</div></div>
        <div className="tile"><div className="k">win rate</div><div className={`v ${st.win_rate_pct == null ? "" : st.win_rate_pct >= 50 ? "up" : "down"}`}>{st.win_rate_pct == null ? "–" : `${st.win_rate_pct}%`}</div></div>
        <div className="tile"><div className="k">profit factor</div><div className={`v ${st.profit_factor == null ? "" : st.profit_factor >= 1 ? "up" : "down"}`}>{st.profit_factor ?? "–"}</div></div>
        <div className="tile"><div className="k">avg holding</div><div className="v">{st.avg_holding ?? "–"}</div></div>
      </div>
      <h2>Open trades</h2>
      {s.open_trades.length === 0 ? <p className="muted">Nothing open right now.</p> : (
        <table><thead><tr><th>asset</th><th>side</th><th>entry</th><th>lev</th><th>stop</th><th>target</th><th>in trade</th><th>price</th><th>margin</th></tr></thead><tbody>
          {s.open_trades.map((t, i) => <tr key={i}><td>{t.cashtag}{t.paper && <span className="badge">paper</span>}</td><td><Side d={t.direction} /></td><td className="mono">{num(t.entry)}</td><td>{t.leverage}x</td><td className="mono">{num(t.stop)}</td><td className="mono">{num(t.target)}</td><td>{t.time_in_trade}</td><td className={cls(t.unrealized_price_pct)}>{pct(t.unrealized_price_pct)}</td><td className={cls(t.unrealized_margin_pct)}>{pct(t.unrealized_margin_pct)}</td></tr>)}
        </tbody></table>
      )}
      <h2>Closed trades</h2>
      {s.closed_trades.length === 0 ? <p className="muted">No closed trades yet.</p> : (
        <table><thead><tr><th>asset</th><th>side</th><th>entry</th><th>exit</th><th>lev</th><th>price</th><th>margin</th><th>held</th><th></th></tr></thead><tbody>
          {s.closed_trades.map((t, i) => <tr key={i}><td>{t.cashtag}{t.paper && <span className="badge">paper</span>}</td><td><Side d={t.direction} /></td><td className="mono">{num(t.entry)}</td><td className="mono">{num(t.exit)}</td><td>{t.leverage}x</td><td className={cls(t.price_pct)}>{pct(t.price_pct)}</td><td className={cls(t.margin_pct)}>{pct(t.margin_pct)}</td><td>{t.holding}</td><td>{t.x_url && <a href={t.x_url}>thread</a>}</td></tr>)}
        </tbody></table>
      )}
    </>
  );
}
