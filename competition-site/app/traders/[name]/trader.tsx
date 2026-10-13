"use client";
import { useEffect } from "react";
import { COLORS, SHOW_MODEL_NAMES, cap, cls, idx, modelLabel, pct, usePoll, type Equity, type Leaderboard, type Rulebooks, type State, type Trades, type TraderId } from "../../api";
import { LineChart } from "../../chart";
import { Avatar, Pips, RulebookView, TradeRow, statusLine } from "../../ui";

export default function TraderPage({ id }: { id: TraderId }) {
  const s = usePoll<State>("/state");
  const lb = usePoll<Leaderboard>("/leaderboard", undefined, 30000);
  const trades = usePoll<Trades>("/trades", { trader: id, limit: 200 });
  const books = usePoll<Rulebooks>("/rulebooks", { trader: id }, 60000);
  const equity = usePoll<Equity>("/equity", { trader: id }, 30000);
  const t = s?.traders.find((x) => x.id === id);
  useEffect(() => { if (t) document.title = `${t.name} · ${idx(t.equity_index)} — dorkbot competition`; }, [t]);
  if (s === undefined) return <p className="muted">Loading…</p>;
  if (!t) return <><h1 style={{ color: COLORS[id] }}>{cap(id)}</h1><p className="muted">Not in the arena yet. The competition has not started.</p></>;
  const row = lb?.traders.find((x) => x.id === id);
  const pts = (equity?.series?.[id] ?? []).map(([x, y]) => [x, y - 100] as [number, number]);
  const bounds = (equity?.rounds ?? []).filter((r) => r.number > 1).map((r) => ({ x: Date.parse(r.started_at), y: 0, color: "var(--muted)", label: `round ${r.number}` }));
  // Rulebooks by round come from /rulebooks when the bot serves it; the current one from /state always.
  const history = (() => {
    const round = s?.round.number ?? 0;
    const past = (books?.rulebooks ?? []).filter((h) => h.round !== round);
    const cur = t.rulebook ? [{ round, rulebook: t.rulebook }] : [];
    return [...cur, ...past].sort((a, b) => b.round - a.round);
  })();
  return (
    <>
      <div className="trader-head" style={{ marginBottom: 8 }}>
        <Avatar id={id} benched={t.benched} size={64} />
        <div className="trader-id">
          <h1 style={{ color: t.benched ? "var(--muted)" : COLORS[id] }}>{t.name}{SHOW_MODEL_NAMES && <span className="badge">{modelLabel(t.model)}</span>}{t.benched && <span className="badge down">benched</span>}</h1>
          <div className="muted small">{statusLine(t)}</div>
        </div>
        <Pips losses={t.losses} />
      </div>

      <div className="row">
        <div className="tile"><div className="k">equity index</div><div className="v">{idx(t.equity_index)}</div></div>
        <div className="tile"><div className="k">total return</div><div className={`v ${cls(t.return_pct)}`}>{pct(t.return_pct, 2)}</div></div>
        <div className="tile"><div className="k">trades</div><div className="v">{row?.trades ?? "–"}</div></div>
        <div className="tile"><div className="k">win rate</div><div className="v">{row?.win_rate_pct == null ? "–" : `${row.win_rate_pct.toFixed(0)}%`}</div></div>
        <div className="tile"><div className="k">avg net ROE</div><div className={`v ${cls(row?.avg_net_roe_pct)}`}>{pct(row?.avg_net_roe_pct, 2)}</div></div>
        <div className="tile"><div className="k">rounds survived</div><div className="v">{row?.rounds_survived ?? "–"}</div></div>
      </div>

      <section className="panel">
        <h2>Equity <span className="muted small">index, round boundaries marked</span></h2>
        {pts.length >= 2 ? <LineChart series={[{ name: t.name, points: pts, color: COLORS[id], width: 2 }]} markers={bounds} height={200} area /> : <p className="muted">Not enough history yet.</p>}
      </section>

      <section className="panel">
        <h2>Rulebooks <span className="muted small">by round, newest first</span></h2>
        {history.length ? history.map((h, i) => (
          <div className="rb-round" key={h.round}>
            <div className="strategy"><b>{h.rulebook.name}</b> <span className="muted">· round {h.round} · {h.rulebook.leverage}x</span><p>{h.rulebook.thesis}</p></div>
            <RulebookView rb={h.rulebook} open={i === 0} />
          </div>
        )) : <p className="muted">No rulebook this round yet.</p>}
      </section>

      <section className="panel">
        <h2>Trades <span className="muted small">click a row for the decision</span></h2>
        {!trades?.trades?.length ? <p className="muted">No trades yet.</p> : (
          <div className="scroll-x"><table className="trades"><thead><tr><th>opened</th><th>side</th><th>lev</th><th>entry</th><th>exit</th><th>net ROE</th><th>exit</th><th></th></tr></thead><tbody>
            {trades.trades.map((tr) => <TradeRow key={tr.id} t={tr} />)}
          </tbody></table></div>
        )}
      </section>
    </>
  );
}
