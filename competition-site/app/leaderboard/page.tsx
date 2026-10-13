"use client";
import { useMemo, useState } from "react";
import { COLORS, SHOW_MODEL_NAMES, cap, cls, dur, idx, modelLabel, pct, usePoll, type LeaderRow, type Leaderboard, type Rounds } from "../api";

type Col = { key: keyof LeaderRow | "rank"; label: string; fmt?: (r: LeaderRow) => React.ReactNode; num?: boolean };

const COLS: Col[] = [
  { key: "name", label: "trader", fmt: (r) => <><a href={`/traders/${r.id}/`} style={{ color: COLORS[r.id] }}>{r.name}</a>{SHOW_MODEL_NAMES && <span className="badge">{modelLabel(r.model)}</span>}</> },
  { key: "equity_index", label: "equity", fmt: (r) => idx(r.equity_index), num: true },
  { key: "return_pct", label: "return", fmt: (r) => <span className={cls(r.return_pct)}>{pct(r.return_pct, 2)}</span>, num: true },
  { key: "trades", label: "trades", num: true },
  { key: "win_rate_pct", label: "win rate", fmt: (r) => (r.win_rate_pct == null ? "–" : `${r.win_rate_pct.toFixed(0)}%`), num: true },
  { key: "avg_net_roe_pct", label: "avg net ROE", fmt: (r) => <span className={cls(r.avg_net_roe_pct)}>{pct(r.avg_net_roe_pct, 2)}</span>, num: true },
  { key: "best_pct", label: "best", fmt: (r) => <span className={cls(r.best_pct)}>{pct(r.best_pct, 2)}</span>, num: true },
  { key: "worst_pct", label: "worst", fmt: (r) => <span className={cls(r.worst_pct)}>{pct(r.worst_pct, 2)}</span>, num: true },
  { key: "rounds_survived", label: "rounds survived", num: true },
  { key: "streak", label: "streak", fmt: (r) => (r.streak === 0 ? "–" : r.streak > 0 ? <span className="up">W{r.streak}</span> : <span className="down">L{-r.streak}</span>), num: true },
  { key: "avg_leverage", label: "avg lev", fmt: (r) => (r.avg_leverage == null ? "–" : `${r.avg_leverage.toFixed(1)}x`), num: true },
  { key: "avg_hold_min", label: "avg hold", fmt: (r) => (r.avg_hold_min == null ? "–" : r.avg_hold_min < 60 ? `${Math.round(r.avg_hold_min)}m` : `${(r.avg_hold_min / 60).toFixed(1)}h`), num: true },
];

export default function LeaderboardPage() {
  const lb = usePoll<Leaderboard>("/leaderboard");
  const rounds = usePoll<Rounds>("/rounds", undefined, 30000);
  const [sort, setSort] = useState<{ key: Col["key"]; desc: boolean }>({ key: "equity_index", desc: true });
  const rows = useMemo(() => {
    if (!lb) return [];
    const r = [...lb.traders];
    if (sort.key !== "rank") {
      const k = sort.key as keyof LeaderRow;
      r.sort((a, b) => { const x = a[k], y = b[k]; const vx = x == null ? -Infinity : typeof x === "number" ? x : String(x); const vy = y == null ? -Infinity : typeof y === "number" ? y : String(y); return (vx < vy ? -1 : vx > vy ? 1 : 0) * (sort.desc ? -1 : 1); });
    }
    return r;
  }, [lb, sort]);
  const rankOf = useMemo(() => { const m = new Map<string, number>(); (lb ? [...lb.traders].sort((a, b) => b.equity_index - a.equity_index) : []).forEach((r, i) => m.set(r.id, i + 1)); return m; }, [lb]);
  if (lb === undefined) return <p className="muted">Loading…</p>;
  if (lb === null) return <><h1>Leaderboard</h1><p className="muted">Nothing to rank yet. The competition has not started.</p></>;
  const click = (k: Col["key"]) => setSort((s) => (s.key === k ? { key: k, desc: !s.desc } : { key: k, desc: true }));
  return (
    <>
      <h1>Leaderboard</h1>
      <p className="muted">Cumulative since the start. Equity is an index, 100 at the start; nothing here is an amount.</p>
      <div className="scroll-x"><table className="wide sortable"><thead><tr>
        <th>#</th>
        {COLS.map((c) => <th key={String(c.key)} className={c.num ? "num" : ""} onClick={() => click(c.key)}>{c.label}{sort.key === c.key ? (sort.desc ? " ▾" : " ▴") : ""}</th>)}
      </tr></thead><tbody>
        {rows.map((r) => <tr key={r.id}><td className="muted">{rankOf.get(r.id)}</td>{COLS.map((c) => <td key={String(c.key)} className={c.num ? "num mono" : ""}>{c.fmt ? c.fmt(r) : String(r[c.key as keyof LeaderRow] ?? "–")}</td>)}</tr>)}
      </tbody></table></div>

      <section className="panel">
        <h2>Rounds</h2>
        {!rounds?.rounds?.length ? <p className="muted">No round finished yet.</p> : (
          <div className="scroll-x"><table className="wide"><thead><tr><th>round</th><th>duration</th><th>bench order</th><th>winner</th></tr></thead><tbody>
            {[...rounds.rounds].sort((a, b) => b.number - a.number).map((r) => (
              <tr key={r.number}><td>{r.number}{!r.ended_at && <span className="badge accent">now</span>}</td><td>{dur(r.started_at, r.ended_at)}</td><td>{r.benches.length ? r.benches.map((b, i) => <span key={i}>{i ? ", " : ""}<span style={{ color: COLORS[b.trader] }}>{cap(b.trader)}</span></span>) : <span className="muted">nobody yet</span>}</td><td>{r.winner ? <span style={{ color: COLORS[r.winner] }}>{cap(r.winner)}</span> : <span className="muted">–</span>}</td></tr>
            ))}
          </tbody></table></div>
        )}
      </section>
    </>
  );
}
