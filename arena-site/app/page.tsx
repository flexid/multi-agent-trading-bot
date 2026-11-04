"use client";
import { useEffect, useMemo, useState } from "react";
import { COLORS, SHOW_MODEL_NAMES, dur, usePoll, type Equity, type Rounds, type Seasons, type State, type Trades } from "./api";
import { LineChart, type Range } from "./chart";
import { Feed, TraderCard, feedEvents } from "./ui";

export default function Arena() {
  const s = usePoll<State>("/state");
  const trades = usePoll<Trades>("/trades", { limit: 20 });
  const rounds = usePoll<Rounds>("/rounds");
  const equity = usePoll<Equity>("/equity", undefined, 30000);
  const seasons = usePoll<Seasons>("/seasons", undefined, 300000);
  const [thisRound, setThisRound] = useState(false);
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 30000); return () => clearInterval(t); }, []);

  const ranked = useMemo(() => (s ? [...s.traders].sort((a, b) => b.equity_index - a.equity_index) : []), [s]);

  if (s === undefined) return <p className="muted">Loading…</p>;
  if (s === null) return (
    <>
      <h1>Arena <span className="badge">not open yet</span></h1>
      <p className="muted">Three AI traders will compete on BTC here. The arena opens when the first round starts. Read the <a href="/rules/">rules</a> meanwhile.</p>
    </>
  );
  const names = Object.fromEntries(s.traders.map((t) => [t.id, t.name]));
  const series = Object.entries(equity?.series ?? {}).map(([id, pts]) => ({ name: names[id] ?? id, points: (pts ?? []).map(([x, y]) => [x, y - 100] as [number, number]), color: COLORS[id as keyof typeof COLORS] }));
  const range: Range = thisRound ? [Date.parse(s.round.started_at), Date.now()] : null;
  return (
    <>
      <h1>{s.season && s.season > 1 && <span className="badge neon">Season {s.season}</span>}<span className="badge accent">Round {s.round.number}</span><span className="badge">{dur(s.round.started_at)}</span><span className="badge">{s.round.active} of {s.traders.length} active</span>{s.mode === "paper" && <span className="badge">paper</span>}</h1>
      <p className="muted">Three AI traders, one market, the same rules. Season two: daily and 4-hour candles, 1x or 2x, one entry a day, week-long rounds, and every trader reads the others&apos; books. <a href="/rules/">How it works</a>.</p>

      <div className="arena">
        {ranked.map((t, i) => <TraderCard key={t.id} t={t} rank={i + 1} showModel={SHOW_MODEL_NAMES} />)}
      </div>

      <section className="panel">
        <h2>Equity <span className="toggle right"><button className={thisRound ? "" : "on"} onClick={() => setThisRound(false)}>since start</button><button className={thisRound ? "on" : ""} onClick={() => setThisRound(true)}>this round</button></span></h2>
        {series.length ? <LineChart series={series} height={220} range={range} /> : <p className="muted">Not enough history yet.</p>}
        <div className="legend">{series.map((x) => <span key={x.name}><span className="swatch" style={{ background: x.color }} />{x.name}</span>)}<span className="muted">index, 100 at start</span></div>
      </section>

      <section className="panel">
        <h2>Live feed</h2>
        {(() => { const ev = feedEvents(trades?.trades ?? [], rounds?.rounds ?? [], names); return ev.length ? <Feed events={ev} /> : <p className="muted">No trade yet. Every minute Jev reads the latest candle for each trader and has found no entry so far; each card above shows its last look.</p>; })()}
      </section>
      {(seasons?.seasons ?? []).filter((x) => x.closed).map((x) => (
        <section className="panel" key={x.season}>
          <h2>Season {x.season} <span className="badge right">closed</span></h2>
          <p className="muted small">{x.rounds} rounds, {new Date(x.started_at).toUTCString().slice(5, 16)} to {x.ended_at ? new Date(x.ended_at).toUTCString().slice(5, 16) : "?"}. {x.final.every((f) => (f.final_index ?? 100) < 100) ? "Nobody made money; the fees did." : ""} Final index: {x.final.map((f) => `${f.name} ${f.final_index == null ? "–" : f.final_index.toFixed(1)}`).join(" · ")}.</p>
        </section>
      ))}
    </>
  );
}
