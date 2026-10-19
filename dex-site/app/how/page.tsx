export default function How() {
  return (
    <div className="rules">
      <h1>How it works</h1>
      <p>dorkbot decides on <a href="https://dorkbot.dev/">dorkbot.dev</a>: fifteen agents, three portfolio managers, a risk engine in code. This site shows the same record mirrored on a decentralised perpetuals exchange, Hyperliquid.</p>
      <h2>The mirror</h2>
      <ul>
        <li><b>It follows, it does not decide.</b> When the main book opens a trade, the mirror opens the same side at the same leverage, sized by the same share of its own equity. When the main book closes, the mirror closes, with the same reason.</li>
        <li><b>The exchange holds the stop and the take.</b> They sit at the main book&apos;s distances from the mirror&apos;s own fill, as trigger orders on the venue. If the mirror ever loses contact, the exchange still closes the trade.</li>
        <li><b>Every pass it checks itself.</b> Positions on the venue against its ledger; on a difference it pauses new entries and says so on this page.</li>
        <li><b>Divergence is shown, not hidden.</b> Each trade carries its entry difference from the main book and its result next to the main book&apos;s.</li>
      </ul>
      <h2>The stages</h2>
      <ol>
        <li><b>Paper:</b> simulated fills on the venue&apos;s real prices, no orders placed.</li>
        <li><b>Testnet:</b> real orders on the test network with test funds. The label at the top says so.</li>
        <li><b>Live:</b> a public address with real funds, every trade on-chain and linked from here.</li>
      </ol>
      <p className="muted">Later, if the record warrants it, a vault: deposit USDC, dorkbot trades it, withdraw when you like, the protocol holds the funds and the fee. Nothing here is advice.</p>
    </div>
  );
}
