"use client";
import { useEffect, useState } from "react";
import { cls, loadSnapshot, pct, type Snapshot } from "../snapshot";

const sc = (x: number | null | undefined) => (x == null ? "–" : `${x > 0 ? "+" : ""}${x.toFixed(2)}`);

export default function Agents() {
  const [s, setS] = useState<Snapshot | null | undefined>(undefined);
  useEffect(() => { loadSnapshot().then(setS); }, []);
  if (s === undefined) return <p className="muted">Loading…</p>;
  if (s === null || s.assets.length === 0) return <p className="muted">No cycle data yet.</p>;
  return (
    <>
      <img className="logo" src="/brand/logo-600.png" alt="dorkbot" />
      <h1>Agents <span className={`badge ${s.mode === "live" ? "neon" : ""}`}>{s.mode}</span></h1>
      <p className="muted">Five agents score each asset every four hours. Two portfolio managers (Claude and GPT) read the same evidence without seeing each other; a trade needs both. Scores run from −1 to +1.</p>
      {s.assets.map((a) => (
        <section key={a.asset}>
          <h2>{a.cashtag} · {a.consensus} <span className="badge">Claude {a.pm_claude ?? "–"}</span><span className="badge">GPT {a.pm_gpt ?? "–"}</span></h2>
          <p className="muted">{a.reason}. Formula {sc(a.formula_score)}, consensus {sc(a.consensus_score)}.
            {a.macro_regime && <> Macro regime {a.macro_regime}, tradfi coupling {a.coupling?.toFixed(2)}; macro parts tradfi {sc(a.macro_tradfi)} / crypto-native {sc(a.macro_native)}.</>}</p>
          <table><thead><tr><th>agent</th><th>score</th><th>confidence</th><th>notes</th></tr></thead><tbody>
            {a.agents.map((g) => <tr key={g.agent}><td>{g.agent}{!g.valid && <span className="badge">stale</span>}</td><td className={cls(g.score)}>{sc(g.score)}</td><td>{pct(g.confidence * 100, 0)}</td><td className="muted">{g.reasons.join(" · ")}</td></tr>)}
          </tbody></table>
        </section>
      ))}
      <h2>Leaderboard</h2>
      {s.leaderboard.length === 0 ? <p className="muted">Measured once enough trades have closed.</p> : (
        <table><thead><tr><th>name</th><th>kind</th><th>IC 1d</th><th>sample</th></tr></thead><tbody>
          {s.leaderboard.map((l) => <tr key={l.name}><td>{l.name}</td><td>{l.kind}</td><td className={cls(l.ic_1d)}>{sc(l.ic_1d)}</td><td>{l.sample}</td></tr>)}
        </tbody></table>
      )}
    </>
  );
}
