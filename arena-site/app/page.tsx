"use client";
import { useEffect, useMemo, useState } from "react";
import { COLORS, SHOW_MODEL_NAMES, dur, usePoll, type Equity, type Rounds, type State, type Trades } from "./api";
import { LineChart, type Range } from "./chart";
import { Feed, TraderCard, feedEvents } from "./ui";

export default function Arena() {
  const s = usePoll<State>("/state");
  const trades = usePoll<Trades>("/trades", { limit: 20 });
  const rounds = usePoll<Rounds>("/rounds");
  const equity = usePoll<Equity>("/equity", undefined, 30000);
  const [thisRound, setThisRound] = useState(false);
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 30000); return () => clearInterval(t); }, []);

  const ranked = useMemo(() => (s ? [...s.traders].sort((a, b) => b.equity_index - a.equity_index) : []), [s]);
  useEffect(() => {
    if (ranked.length && s) document.title = `${ranked[0].name} leads · R${s.round.number} — dorkbot arena`;
  }, [ranked, s]);

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
      <h1><span className="badge accent">Round {s.round.number}</span><span className="badge">{dur(s.round.started_at)}</span><span className="badge">{s.round.active} of {s.traders.length} active</span></h1>
      <p className="muted">Three AI traders, one market, the same rules. Two losses in a round and you sit out until everyone is benched. <a href="/rules/">How it works</a>.</p>

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
        <Feed events={feedEvents(trades?.trades ?? [], rounds?.rounds ?? [], names)} />
      </section>
    </>
  );
}
