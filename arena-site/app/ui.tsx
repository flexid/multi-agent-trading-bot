"use client";
import { useState } from "react";
import { COLORS, LOSSES_TO_BENCH, ago, cap, cls, idx, modelLabel, num, pct, type Round, type Rulebook, type Trade, type TraderId, type TraderState } from "./api";

export const V = "c3"; // brand asset version for this site

/** "long" in green, "short" in red, everywhere they appear. */
export function Side({ d }: { d: string }) {
  const k = d.toLowerCase();
  return k === "long" || k === "short" ? <span className={k}>{cap(d)}</span> : <>{d}</>;
}

export function Avatar({ id, benched, size = 56 }: { id: TraderId; benched?: boolean; size?: number }) {
  return <img className={`duck${benched ? " benched" : ""}`} src={`/brand/${id}.svg?v=${V}`} alt="" width={size} height={size} />;
}

/** Two loss pips: lit for each loss this round. */
export function Pips({ losses }: { losses: number }) {
  return (
    <span className="pips" title={`${losses} of ${LOSSES_TO_BENCH} losses this round`}>
      {Array.from({ length: LOSSES_TO_BENCH }, (_, i) => <span key={i} className={`pip${i < losses ? " lit" : ""}`} />)}
    </span>
  );
}

export function RulebookView({ rb, open }: { rb: Rulebook; open?: boolean }) {
  return (
    <details className="acc" open={open}>
      <summary><span className="t">rulebook</span><span className="muted small">{rb.leverage}x · TP {rb.tp_net_roe_pct}% · SL {rb.sl_net_roe_pct}% net · conviction ≥ {rb.min_conviction.toFixed(2)}</span></summary>
      <div className="acc-body rb">
        <div><b>Long when</b><p>{rb.entry_long}</p><Conds items={rb.conditions?.long} /></div>
        <div><b>Short when</b><p>{rb.entry_short}</p><Conds items={rb.conditions?.short} /></div>
        <div><b>Stay flat when</b><p>{rb.no_trade}</p><Conds items={rb.conditions?.no_trade} /></div>
        {(rb.early_exit || rb.conditions?.exit?.length || rb.conditions?.max_hold_minutes) ? <div><b>Exit early when</b><p>{rb.early_exit}</p><Conds items={rb.conditions?.exit} />{rb.conditions?.max_hold_minutes ? <p className="muted small">or after {rb.conditions.max_hold_minutes} minutes</p> : null}</div> : null}
        {rb.conditions && <p className="muted small">The lines in code are what trades; the words are for reading.</p>}
      </div>
    </details>
  );
}

/** What Jev saw on the last candle: the three probabilities and the conviction against the bar. */
export function LastLook({ t }: { t: TraderState }) {
  const d = t.last_decision;
  if (!d) return <p className="muted small look">Waiting for the first closed candle.</p>;
  const pct = (x: number) => `${Math.round(x * 100)}%`;
  const bar = d.conviction >= d.min_conviction;
  const coded = !!t.rulebook?.conditions?.long?.length || !!t.rulebook?.conditions?.short?.length;
  return (
    <p className="small look">
      <span className="muted">Last candle, {ago(d.at)} ago:</span>{" "}
      {coded && d.kind === "signal" ? (
        <><span className="long">long rules {pct(d.p_long)} met</span> · <span className="short">short rules {pct(d.p_short)} met</span>{d.acted && <span className="badge neon">entered</span>}{t.judgments_this_round != null && <span className="muted"> · {t.judgments_this_round} checks this round</span>}</>
      ) : d.kind === "signal" ? (
        <><span className="long">long {pct(d.p_long)}</span> · <span className="short">short {pct(d.p_short)}</span> · none {pct(d.p_none)} · conviction <b className={bar ? "up" : ""}>{d.conviction.toFixed(2)}</b> <span className="muted">of {d.min_conviction.toFixed(2)} needed</span>{d.acted && <span className="badge neon">acted</span>}</>
      ) : (
        <>close {pct(d.p_close ?? 0)} · conviction <b>{d.conviction.toFixed(2)}</b> <span className="muted">of {d.min_conviction.toFixed(2)} needed</span>{d.acted && <span className="badge neon">closing</span>}</>
      )}
      {t.judgments_this_round != null && <span className="muted"> · {t.judgments_this_round} looks this round</span>}
    </p>
  );
}

function Conds({ items }: { items?: string[] | null }) {
  if (!items?.length) return null;
  return <ul className="conds">{items.map((c, i) => <li key={i} className="mono small">{c}</li>)}</ul>;
}

export function statusLine(t: TraderState): React.ReactNode {
  if (t.benched) return <span className="muted">Benched · waiting for reset</span>;
  if (!t.position) return <span>Flat</span>;
  const p = t.position;
  return <><Side d={p.side} /> {p.leverage}x · <span className={cls(p.unrealized_roe_pct)}>{pct(p.unrealized_roe_pct, 2)}</span> unrealised</>;
}

export function TraderCard({ t, rank, showModel = true }: { t: TraderState; rank: number; showModel?: boolean }) {
  return (
    <article className={`trader${t.benched ? " is-benched" : ""}`} style={{ borderColor: t.benched ? "var(--line)" : COLORS[t.id] }}>
      {t.benched && <span className="ribbon">Benched</span>}
      <div className="trader-head">
        <Avatar id={t.id} benched={t.benched} />
        <div className="trader-id">
          <div className="trader-name"><a href={`/traders/${t.id}/`} style={{ color: t.benched ? "var(--muted)" : COLORS[t.id] }}>{t.name}</a>{showModel && <span className="badge">{modelLabel(t.model)}</span>}</div>
          <div className="muted small">#{rank} · {statusLine(t)}</div>
        </div>
        <Pips losses={t.losses} />
      </div>
      <div className="row trader-nums">
        <div className="tile"><div className="k">equity index</div><div className="v">{idx(t.equity_index)}</div></div>
        <div className="tile"><div className="k">total return</div><div className={`v ${cls(t.return_pct)}`}>{pct(t.return_pct, 2)}</div></div>
      </div>
      {t.rulebook ? (
        <>
          <div className="strategy"><b>{t.rulebook.name}</b> <span className="muted">· {t.rulebook.leverage}x</span><p>{t.rulebook.thesis}</p></div>
          <LastLook t={t} />
          <RulebookView rb={t.rulebook} />
        </>
      ) : <p className="muted small">No rulebook yet this round.</p>}
    </article>
  );
}

export type FeedEvent = { at: string; kind: "open" | "close" | "bench" | "reset"; trader?: TraderId; text: React.ReactNode; cls?: string };

/** Opens, closes, benches and resets, merged and newest first. */
export function feedEvents(trades: Trade[], rounds: Round[], names: Record<string, string>, limit = 20): FeedEvent[] {
  const out: FeedEvent[] = [];
  const nm = (id: TraderId) => names[id] ?? cap(id);
  for (const tr of trades) {
    out.push({ at: tr.opened_at, kind: "open", trader: tr.trader, text: <>{nm(tr.trader)} opened a <Side d={tr.side} /> at {num(tr.entry)}, {tr.leverage}x</> });
    if (tr.closed_at) out.push({ at: tr.closed_at, kind: "close", trader: tr.trader, cls: cls(tr.net_roe_pct), text: <>{nm(tr.trader)} closed the <Side d={tr.side} /> at {num(tr.exit)} · <b className={cls(tr.net_roe_pct)}>{pct(tr.net_roe_pct, 2)}</b> net · {reasonLabel(tr.exit_reason)}</> });
  }
  for (const r of rounds) {
    for (const b of r.benches) out.push({ at: b.at, kind: "bench", trader: b.trader, text: <>{nm(b.trader)} benched in round {r.number} after {b.losses} losses</> });
    if (r.number > 1) out.push({ at: r.started_at, kind: "reset", text: <>Round {r.number} started · everyone back in</> });
  }
  return out.sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).slice(0, limit);
}

export const reasonLabel = (r: Trade["exit_reason"]) => ({ tp: "take profit", sl: "stop loss", earlyExit: "early exit", bench: "benched" } as Record<string, string>)[r ?? ""] ?? (r ?? "open");

export function Feed({ events }: { events: FeedEvent[] }) {
  if (!events.length) return <p className="muted">Nothing has happened yet.</p>;
  return (
    <ul className="feed">
      {events.map((e, i) => <li key={i}><span className="muted small mono">{ago(e.at)} ago</span><span className="dot" style={{ background: e.trader ? COLORS[e.trader] : "var(--muted)" }} /><span>{e.text}</span></li>)}
    </ul>
  );
}

/** One trade with an expandable details row: the opening decision in full. */
export function TradeRow({ t }: { t: Trade }) {
  const [open, setOpen] = useState(false);
  const d = t.decision;
  return (
    <>
      <tr className="click" onClick={() => setOpen(!open)}>
        <td className="mono small">{t.opened_at.slice(5, 16).replace("T", " ")}</td>
        <td><Side d={t.side} /></td>
        <td>{t.leverage}x</td>
        <td className="mono">{num(t.entry)}</td>
        <td className="mono">{num(t.exit)}</td>
        <td className={cls(t.net_roe_pct)}>{pct(t.net_roe_pct, 2)}</td>
        <td>{reasonLabel(t.exit_reason)}</td>
        <td className="muted small">{open ? "hide" : "details"}</td>
      </tr>
      {open && (
        <tr className="details"><td colSpan={8}>
          <div className="row">
            <div className="tile"><div className="k">take profit · stop</div><div className="v small mono">{num(t.tp)} · {num(t.sl)}</div></div>
            <div className="tile"><div className="k">fees, % of margin</div><div className="v small">{t.fees_pct_of_margin == null ? "–" : `${t.fees_pct_of_margin.toFixed(2)}%`}</div></div>
            <div className="tile"><div className="k">held</div><div className="v small">{t.closed_at ? durText(t.opened_at, t.closed_at) : "open"}</div></div>
            <div className="tile"><div className="k">round</div><div className="v small">{t.round}</div></div>
          </div>
          <p className="small" style={{ margin: "10px 0 0" }}>
            <b>Jev at entry:</b> P(long) {d.p_long.toFixed(2)} · P(short) {d.p_short.toFixed(2)} · P(none) {d.p_none.toFixed(2)} · conviction <b>{d.conviction.toFixed(2)}</b> against a minimum of {d.min_conviction.toFixed(2)}
          </p>
        </td></tr>
      )}
    </>
  );
}

const durText = (a: string, b: string) => {
  const m = Math.max(0, Math.round((Date.parse(b) - Date.parse(a)) / 60000));
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
};
