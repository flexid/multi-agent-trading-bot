"use client";
import { useEffect, useState } from "react";

/* dex.json, pushed by the DEX mirror every 15 seconds. Percentages and the index only. */
export type OpenTrade = { asset: string; side: string; leverage: number; entry: number; price: number | null; tp: number; sl: number; opened_at: string; unrealized_price_pct: number; unrealized_margin_pct: number; main_entry: number; entry_divergence_pct: number | null };
export type ClosedTrade = { asset: string; side: string; leverage: number; entry: number; exit: number | null; opened_at: string; closed_at: string | null; net_roe_pct: number | null; exit_reason: string | null; main_margin_pct: number | null; tx: string | null };
export type Dex = {
  generated_at: string; mode: "paper" | "testnet" | "live"; venue: string; address: string | null; explorer: string | null; follows: string; since: string | null;
  equity_index: number; return_pct: number; mismatch: string | null; equity_curve: [number, number][]; open_trades: OpenTrade[]; closed_trades: ClosedTrade[]; stats: { trades: number; wins: number };
};

export const DATA_URL = process.env.NEXT_PUBLIC_DEX_DATA || "/samples/dex.json";

export function useDex(ms = 15000): Dex | null | undefined {
  const [d, setD] = useState<Dex | null | undefined>(undefined);
  useEffect(() => {
    const pull = async () => {
      if (document.hidden) return;
      try {
        const r = await fetch(`${DATA_URL}?t=${Math.floor(Date.now() / 5000)}`, { cache: "no-store" });
        setD(r.ok ? ((await r.json()) as Dex) : (cur) => cur ?? null);
      } catch { setD((cur) => cur ?? null); }
    };
    pull();
    const t = setInterval(pull, ms);
    return () => clearInterval(t);
  }, [ms]);
  return d;
}

export const pct = (x: number | null | undefined, d = 2) => (x == null ? "–" : `${x > 0 ? "+" : ""}${x.toFixed(d)}%`);
export const cls = (x: number | null | undefined) => (x == null ? "" : x >= 0 ? "up" : "down");
export const num = (x: number | null | undefined) => (x == null ? "–" : Math.abs(x) >= 1 ? x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 8 }));
export const ago = (iso: string) => { const m = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000)); return m < 60 ? `${m}m` : m < 2880 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${Math.floor(m / 1440)}d`; };
export const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
