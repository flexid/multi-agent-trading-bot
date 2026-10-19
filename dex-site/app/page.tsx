"use client";
import { ago, cap, cls, num, pct, useDex } from "./api";

function Side({ d }: { d: string }) { const k = d.toLowerCase(); return <span className={k}>{cap(d)}</span>; }

export default function Mirror() {
  const d = useDex();
  if (d === undefined) return <p className="muted">Loading…</p>;
  if (d === null) return <><h1>Mirror <span className="badge">not running</span></h1><p className="muted">The DEX mirror has not published yet.</p></>;
  const label = d.mode === "live" ? "LIVE" : d.mode === "testnet" ? "TESTNET" : "PAPER";
  return (
    <>
      <h1><span className={`badge ${d.mode === "live" ? "neon" : "warn"}`} style={{ fontSize: 14, padding: "4px 12px" }}>{label}</span><span className="badge">{d.venue}</span>{d.mismatch && <span className="badge down">paused: mismatch</span>}</h1>
      <p className="muted">dorkbot&apos;s record, mirrored trade for trade on a decentralised perpetuals exchange. {d.mode === "live" ? "Real funds on a public address." : d.mode === "testnet" ? "Testnet funds: real orders on the test network, no money at stake." : "Paper fills on the venue's prices, no orders placed."} The decisions come from <a href="https://dorkbot.dev/">dorkbot.dev</a>; nothing is decided here. <a href="/how/">How it works</a>.</p>
      <div className="row">
        <div className="tile"><div className="k">equity index</div><div className="v">{d.equity_index.toFixed(2)}</div><div className="muted small">100 at start{d.since ? `, ${new Date(d.since).toUTCString().slice(5, 16)}` : ""}</div></div>
        <div className="tile"><div className="k">return</div><div className={`v ${cls(d.return_pct)}`}>{pct(d.return_pct)}</div></div>
        <div className="tile"><div className="k">trades</div><div className="v">{d.stats.trades}</div><div className="muted small">{d.stats.wins} won</div></div>
        <div className="tile"><div className="k">follows</div><div className="v small">the {d.follows} book</div></div>
      </div>
      {d.address && <p className="small muted">Address {d.address.slice(0, 8)}…{d.address.slice(-6)} · <a href={d.explorer ?? "#"}>explorer</a></p>}

      <section className="panel">
        <h2>Open trades <span className="badge right">{label.toLowerCase()}</span></h2>
        {d.open_trades.length === 0 ? <p className="muted">Nothing open right now.</p> : (
          <div className="scroll-x"><table className="trades"><thead><tr><th>asset</th><th>side</th><th>lev</th><th>entry</th><th>now</th><th>tp</th><th>sl</th><th>price</th><th>margin</th><th>vs main</th><th>since</th></tr></thead><tbody>
            {d.open_trades.map((t, i) => <tr key={i}><td>{t.asset}</td><td><Side d={t.side} /></td><td>{t.leverage}x</td><td className="mono">{num(t.entry)}</td><td className="mono">{num(t.price)}</td><td className="mono">{num(t.tp)}</td><td className="mono">{num(t.sl)}</td><td className={cls(t.unrealized_price_pct)}>{pct(t.unrealized_price_pct)}</td><td className={cls(t.unrealized_margin_pct)}>{pct(t.unrealized_margin_pct)}</td><td className="muted">{t.entry_divergence_pct == null ? "–" : `${pct(t.entry_divergence_pct, 3)} entry`}</td><td>{ago(t.opened_at)}</td></tr>)}
          </tbody></table></div>
        )}
      </section>

      <section className="panel">
        <h2>Closed trades</h2>
        {d.closed_trades.length === 0 ? <p className="muted">No trade closed yet.</p> : (
          <div className="scroll-x"><table className="trades"><thead><tr><th>asset</th><th>side</th><th>lev</th><th>entry</th><th>exit</th><th>net</th><th>main book</th><th>exit</th><th>held</th><th></th></tr></thead><tbody>
            {d.closed_trades.map((t, i) => <tr key={i}><td>{t.asset}</td><td><Side d={t.side} /></td><td>{t.leverage}x</td><td className="mono">{num(t.entry)}</td><td className="mono">{num(t.exit)}</td><td className={cls(t.net_roe_pct)}>{pct(t.net_roe_pct)}</td><td className={cls(t.main_margin_pct)}>{pct(t.main_margin_pct)}</td><td>{(t.exit_reason ?? "").replace("mirror:", "")}</td><td>{t.closed_at ? ago(t.opened_at).replace(/^(.*)$/, (m) => m) : ""}</td><td>{t.tx ? <a href={`${d.explorer ?? ""}`}>tx</a> : <span className="muted small">paper</span>}</td></tr>)}
          </tbody></table></div>
        )}
        <p className="muted small">"net" is this mirror's result on its margin after fees; "main book" is what the same trade made on dorkbot.dev. The difference is slippage, fees and timing.</p>
      </section>
    </>
  );
}
