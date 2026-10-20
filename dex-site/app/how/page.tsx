export default function How() {
  return (
    <div className="rules">
      <h1>How it works</h1>
      <p>dorkbot decides on <a href="https://dorkbot.dev/">dorkbot.dev</a>: fifteen agents read the market, three portfolio managers argue, and a risk engine in code has the last word. This site shows the same record mirrored, trade for trade, on Hyperliquid, a decentralised perpetuals exchange, with real funds on a public address.</p>

      <h2>The mirror</h2>
      <ul>
        <li><b>It follows, it does not decide.</b> When the main book opens a trade, the mirror opens the same side at the same leverage, sized by the same share of its own equity, capped per trade. When the main book closes, the mirror closes, with the same reason.</li>
        <li><b>The exchange holds the stop and the take.</b> They sit at the main book&apos;s distances from the mirror&apos;s own fill, as trigger orders on the venue. If the mirror ever loses contact, the exchange still closes the trade.</li>
        <li><b>Every few seconds it checks itself.</b> Positions on the venue against its own ledger; on a difference it pauses new entries and says so at the top of the page.</li>
        <li><b>Divergence is shown, not hidden.</b> Each trade carries its entry difference from the main book and its result next to the main book&apos;s. The gap is slippage, fees and timing.</li>
        <li><b>Nothing here is custody.</b> The bot trades through an API wallet that can place and cancel orders and nothing else. Funds move only by the account&apos;s own wallet.</li>
      </ul>

      <h2>The ambition</h2>
      <p>Once the mirror has a record worth following, the plan is to open a Hyperliquid vault: anyone deposits USDC into it with their own wallet, dorkbot trades the pool, results are shared pro rata, withdrawals stay in the depositor&apos;s hands, and the protocol holds the funds and the fee. No token, no contract of ours, no custody. Until then this page is one account, dorkbot&apos;s own, trading in the open.</p>

      <p className="muted">The label at the top says what the figures are: LIVE for real funds on the address shown, PAPER for simulated fills on the venue&apos;s prices. Percentages and an equity index only, never amounts. Nothing here is advice.</p>
    </div>
  );
}
