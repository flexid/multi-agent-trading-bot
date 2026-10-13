"use client";
import { useEffect, useRef, useState } from "react";

/* The Competition API (bot spec §6), read-only. These types are the site's whole contract
   with the bot; the bot publishes them as @dorkbot/competition-types. Percentages and the
   equity index only, never USD. */

import { TRADERS, type TraderId } from "./consts";
export { TRADERS, type TraderId };
export const LOSSES_TO_BENCH = 2;
export const POLL_MS = 10000;
export const SHOW_MODEL_NAMES = true;

export type Rulebook = {
  name: string;
  thesis: string;
  leverage: number;
  tp_net_roe_pct: number;
  sl_net_roe_pct: number;
  min_conviction: number;
  entry_long: string;
  entry_short: string;
  no_trade: string;
  early_exit?: string | null;
};

export type Position = {
  side: "long" | "short";
  entry: number;
  leverage: number;
  tp: number;
  sl: number;
  unrealized_roe_pct: number;
  opened_at: string;
};

export type TraderState = {
  id: TraderId;
  name: string;
  model: string;
  benched: boolean;
  benched_at?: string | null;
  losses: number;
  equity_index: number;
  return_pct: number;
  position: Position | null;
  rulebook: Rulebook | null;
};

export type State = {
  generated_at: string;
  mode?: "live" | "paper";
  started_at: string;
  round: { number: number; started_at: string; active: number };
  traders: TraderState[];
};

export type LeaderRow = {
  id: TraderId;
  name: string;
  model: string;
  equity_index: number;
  return_pct: number;
  trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number | null;
  avg_net_roe_pct: number | null;
  best_pct: number | null;
  worst_pct: number | null;
  rounds_survived: number;
  streak: number;
  avg_leverage: number | null;
  avg_hold_min: number | null;
};

export type Leaderboard = { generated_at: string; traders: LeaderRow[] };

export type Decision = {
  p_long: number;
  p_short: number;
  p_none: number;
  conviction: number;
  min_conviction: number;
};

export type Trade = {
  id: number;
  trader: TraderId;
  round: number;
  side: "long" | "short";
  leverage: number;
  entry: number;
  exit: number | null;
  tp: number;
  sl: number;
  opened_at: string;
  closed_at: string | null;
  net_roe_pct: number | null;
  fees_pct_of_margin: number | null;
  result: "win" | "loss" | null;
  exit_reason: "tp" | "sl" | "earlyExit" | "bench" | null;
  decision: Decision;
};

export type Trades = { trades: Trade[] };

export type Round = {
  number: number;
  started_at: string;
  ended_at: string | null;
  benches: { trader: TraderId; at: string; losses: number }[];
  winner: TraderId | null;
};

export type Rounds = { rounds: Round[] };

/** Optional: every rulebook a trader declared, by round. The site shows only the current one without it. */
export type Rulebooks = { rulebooks: { round: number; rulebook: Rulebook }[] };

export type Equity = {
  rounds: { number: number; started_at: string }[];
  series: Partial<Record<TraderId, [number, number][]>>; // [ms, index]
};

export const API_BASE = process.env.NEXT_PUBLIC_ARENA_API ?? "/samples";
const SAMPLES = API_BASE === "/samples";

/** Sample files stand in for the API in local dev: /samples/state.json etc. */
function url(path: string, query?: Record<string, string | number | undefined>): string {
  if (SAMPLES) return `${API_BASE}/${path.replace(/^\//, "")}.json?t=${Math.floor(Date.now() / 60000)}`;
  const q = Object.entries(query ?? {}).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`);
  return `${API_BASE}${path}${q.length ? `?${q.join("&")}` : ""}`;
}

const cache = new Map<string, { at: number; data: unknown }>();

/** The sample files hold every trader; apply the `trader` query the real API would. */
function narrow(data: unknown, trader: string): unknown {
  const d = data as { trades?: Trade[]; series?: Equity["series"] } & Record<string, unknown>;
  if (Array.isArray(d.trades)) return { ...d, trades: d.trades.filter((x) => x.trader === trader) };
  if (d.series && typeof d.series === "object") return { ...d, series: { [trader]: d.series[trader as TraderId] ?? [] } };
  return data;
}

export async function get<T>(path: string, query?: Record<string, string | number | undefined>, maxAgeMs = 2000): Promise<T | null> {
  const u = url(path, query);
  const hit = cache.get(u);
  if (hit && Date.now() - hit.at < maxAgeMs) return hit.data as T;
  try {
    const r = await fetch(u, { cache: "no-store" });
    if (!r.ok) return null;
    let data = (await r.json()) as T;
    if (SAMPLES && query?.trader) data = narrow(data, String(query.trader)) as T;
    cache.set(u, { at: Date.now(), data });
    return data;
  } catch {
    return null;
  }
}

/** Poll an endpoint every `ms` while the tab is visible. `undefined` = loading, `null` = unreachable. */
export function usePoll<T>(path: string, query?: Record<string, string | number | undefined>, ms = POLL_MS): T | null | undefined {
  const [data, setData] = useState<T | null | undefined>(undefined);
  const key = JSON.stringify([path, query]);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    const pull = () => { if (!document.hidden) get<T>(path, query).then((d) => { if (alive.current) setData((cur) => (d === null && cur !== undefined ? cur : d)); }); };
    pull();
    const t = setInterval(pull, ms);
    const onVis = () => { if (!document.hidden) pull(); };
    document.addEventListener("visibilitychange", onVis);
    return () => { alive.current = false; clearInterval(t); document.removeEventListener("visibilitychange", onVis); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ms]);
  return data;
}

/* formatting, as on dorkbot.dev */
export const pct = (x: number | null | undefined, d = 1) => (x == null ? "–" : `${x > 0 ? "+" : ""}${x.toFixed(d)}%`);
export const cls = (x: number | null | undefined) => (x == null ? "" : x >= 0 ? "up" : "down");
export const num = (x: number | null | undefined, d = 2) => (x == null ? "–" : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
export const idx = (x: number | null | undefined) => (x == null ? "–" : x.toFixed(2));
export const ago = (iso: string, now = Date.now()) => {
  const s = Math.max(0, Math.floor((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h}h ${m % 60}m` : `${Math.floor(h / 24)}d ${h % 24}h`;
};
export const dur = (fromIso: string, toIso?: string | null, now = Date.now()) => {
  const ms = (toIso ? Date.parse(toIso) : now) - Date.parse(fromIso);
  const m = Math.max(0, Math.round(ms / 60000));
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h}h ${m % 60}m` : `${Math.floor(h / 24)}d ${h % 24}h`;
};
export const when = (iso: string) => new Date(iso).toUTCString().slice(5, 22) + " UTC";
export const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

export const COLORS: Record<TraderId, string> = { kwikton: "#4cc9f0", kwekton: "#b388ff", kwakton: "#ffd166" };
export const MODEL_LABEL: Record<string, string> = { astra: "Astra", grok: "Grok", fable: "Fable", claude: "Fable" };
export const modelLabel = (m: string) => MODEL_LABEL[m.toLowerCase()] ?? m;
