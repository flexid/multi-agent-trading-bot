export type Snapshot = {
  generated_at: string; mode: "shadow" | "live"; handle: string; heartbeat_ok: boolean;
  performance: { since: string; bot_pct: number; btc_hold_pct: number; basket_pct: number; drawdown_pct: number; equity_curve: number[][]; btc_curve: number[][]; basket_curve: number[][] };
  stats: { trades: number; win_rate_pct: number | null; profit_factor: number | null; avg_holding: string | null };
  open_trades: { asset: string; cashtag: string; direction: string; entry: number; leverage: number; stop: number; target: number; time_in_trade: string; unrealized_price_pct: number; unrealized_margin_pct: number; paper: boolean }[];
  closed_trades: { asset: string; cashtag: string; direction: string; entry: number; exit: number; leverage: number; price_pct: number; margin_pct: number; holding: string; opened_at: string; closed_at: string; x_url: string | null; paper: boolean }[];
  assets: { asset: string; cashtag: string; agents: { agent: string; score: number; confidence: number; valid: boolean; reasons: string[] }[]; pm_claude: string | null; pm_gpt: string | null; consensus: string; consensus_score: number | null; formula_score: number | null; reason: string; macro_regime: string | null; coupling: number | null; macro_tradfi: number | null; macro_native: number | null }[];
  leaderboard: { name: string; kind: string; ic_1d: number | null; sample: number }[];
  by_asset: { asset: string; cashtag: string; trades: number; wins: number; pct: number }[];
  history: { ts: string; scores: Record<string, number | null>; directions: Record<string, string> }[];
  crew: { id: string; name: string; role: string; blurb: string }[];
};

export const SNAPSHOT_URL = process.env.NEXT_PUBLIC_SNAPSHOT_URL ?? "/data/snapshot.json";

export async function loadSnapshot(): Promise<Snapshot | null> {
  try {
    const r = await fetch(`${SNAPSHOT_URL}?t=${Math.floor(Date.now() / 60000)}`, { cache: "no-store" });
    return r.ok ? ((await r.json()) as Snapshot) : null;
  } catch {
    return null;
  }
}

export const pct = (x: number | null | undefined, d = 1) => (x == null ? "–" : `${x > 0 ? "+" : ""}${x.toFixed(d)}%`);
export const cls = (x: number | null | undefined) => (x == null ? "" : x >= 0 ? "up" : "down");
export const num = (x: number) => x.toLocaleString("en-US", { maximumFractionDigits: 4 });

export async function shareUrl(): Promise<string> {
  // WhatsApp and X cache link previews per URL, so the share link carries the current
  // content hash of the preview image; a new hash means a fresh preview.
  try {
    const r = await fetch(`${SNAPSHOT_URL.replace(/snapshot\.json$/, "og-latest.json")}?t=${Math.floor(Date.now() / 60000)}`, { cache: "no-store" });
    const j = r.ok ? ((await r.json()) as { stamp?: string }) : {};
    return `https://dorkbot.dev/?s=${j.stamp ?? Date.now().toString(36)}`;
  } catch {
    return `https://dorkbot.dev/?s=${Date.now().toString(36)}`;
  }
}

export const tick = (cashtag: string) => cashtag.replace(/^\$/, "");  // tickers without the dollar sign on the site (owner, 2026-10-09)
