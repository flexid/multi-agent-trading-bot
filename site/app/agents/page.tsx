"use client";
import { useEffect, useState } from "react";
import { cls, loadSnapshot, pct, tick, type Snapshot } from "../snapshot";

const name = (s: Snapshot, id: string) => { const m = (s.crew ?? []).find((c) => c.id === id); return m ? <><b>{m.name}</b> <span className="muted small">{id}</span></> : id; };
// Model rows read "<model> (<seat>/<variant>)". The crew name follows the model, not the
// seat: Dyne is the Claude manager wherever it sits; "alt" means it answered from the other
// seat as the shadow comparison.
const lname = (s: Snapshot, l: { name: string; kind: string }): { label: string; note: string } => {
  if (l.kind === "agent") { const m = (s.crew ?? []).find((c) => c.id === l.name); return { label: m ? m.name : l.name, note: l.name }; }
  const mm = l.name.match(/^(.*?) \((pm_[12])\/(main|alt)\)$/);
  if (!mm) return { label: l.name, note: "" };
  const [, model, , variant] = mm;
  const id = model.startsWith("claude") ? "pm_1" : model.startsWith("gpt") ? "pm_2" : null;
  const m = id ? (s.crew ?? []).find((c) => c.id === id) : null;
  return { label: m ? m.name : model, note: `${model} · ${variant === "main" ? "its own seat" : "shadow, the other seat"}` };
};
const sc = (x: number | null | undefined) => (x == null ? "–" : `${x > 0 ? "+" : ""}${x.toFixed(2)}`);

export default function Agents() {
  const [s, setS] = useState<Snapshot | null | undefined>(undefined);
  useEffect(() => { loadSnapshot().then(setS); }, []);
  if (s === undefined) return <p className="muted">Loading…</p>;
  if (s === null || s.assets.length === 0) return <p className="muted">No cycle data yet.</p>;
  return (
    <>
      <h1>The crew <span className={`badge ${s.mode === "live" ? "neon" : ""}`}>{s.mode}</span></h1>
      <p className="muted"><b>dorkbot</b> trades ten coins on Bybit with its own money, long and short, in two sleeves: the bluechips (BTC, ETH, SOL, XRP, DOGE) carry most of the capital, the alts (PEPE, HBAR, PUMP, SPX, ENA) a smaller share with tighter limits. Every four hours each coin gets a set of readings, scored from −1 (very bearish) to +1 (very bullish): the tape, the charts, the prediction markets, the macro climate and X. The alts get one more from Djaf, who labels news, exchange notices and token unlocks the moment they land.</p>
      <p className="muted crew-intro">Two portfolio managers (PMs), one on Claude and one on GPT, read the same evidence without seeing each other and each propose a trade or nothing. Only when both agree on the direction does a plan go further. A deterministic rulebook then sets the size and can still say no: exposure caps, a daily loss lock, a drawdown pause. One process alone touches the exchange and runs the stops, targets and time limits in code, around the clock, with no model in the loop. Every fill is posted to X. On Monday, Optima checks what each reading costs against what it predicts, and Coach writes the week&apos;s review for the owner. Nobody on the crew changes the bot by itself.</p>
      <div className="crew">
        {(s.crew ?? []).map((m) => <div key={m.id} className="tile"><div className="k">{m.role}</div><div className="v">{m.name}</div><div className="muted small">{m.blurb}</div></div>)}
      </div>
      <h2>Latest readings</h2>
      <p className="muted small">Tap a coin to open or close its readings.</p>
      {s.assets.map((a, i) => (
        <details key={a.asset} className="acc" open={i === 0}>
          <summary><span className="t">{tick(a.cashtag)}</span><span className="muted">{a.consensus}</span><span className="badge">Dyne {a.pm_claude ?? "–"}</span><span className="badge">Dork {a.pm_gpt ?? "–"}</span></summary>
          <div className="acc-body">
          <p className="muted">{a.reason}. Formula {sc(a.formula_score)}, consensus {sc(a.consensus_score)}.
            {a.macro_regime && <> Macro regime {a.macro_regime}, tradfi coupling {a.coupling?.toFixed(2)}; macro parts tradfi {sc(a.macro_tradfi)} / crypto-native {sc(a.macro_native)}.</>}</p>
          <div className="scroll-x"><table className="notes"><thead><tr><th className="name">reading</th><th className="num">score</th><th className="num">confidence</th><th>notes</th></tr></thead><tbody>
            {a.agents.map((g) => <tr key={g.agent}><td className="name">{name(s, g.agent)}{!g.valid && <span className="badge">stale</span>}</td><td className={`num ${cls(g.score)}`}>{sc(g.score)}</td><td className="num">{pct(g.confidence * 100, 0)}</td><td className="note">{g.reasons.join(" · ")}</td></tr>)}
          </tbody></table></div>
          </div>
        </details>
      ))}
      <section className="panel">
      <h2>Leaderboard <span className="muted small">how well each reading predicted the next day</span></h2>
      {s.leaderboard.length === 0 ? <p className="muted">Measured once enough trades have closed.</p> : (
        <div className="scroll-x"><table className="notes"><thead><tr><th className="name">name</th><th className="num">IC 1d</th><th className="num">sample</th><th>notes</th></tr></thead><tbody>
          {s.leaderboard.map((l) => <tr key={l.name}><td className="name"><b>{lname(s, l).label}</b></td><td className={`num ${cls(l.ic_1d)}`}>{sc(l.ic_1d)}</td><td className="num">{l.sample}</td><td className="note">{l.kind === "agent" ? "reading" : "manager"}{lname(s, l).note ? ` · ${lname(s, l).note}` : ""}</td></tr>)}
        </tbody></table></div>
      )}
      </section>
    </>
  );
}
