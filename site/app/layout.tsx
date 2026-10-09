import type { Metadata } from "next";
import "./globals.css";
import ShareLink from "./share";

const SNAP = process.env.NEXT_PUBLIC_SNAPSHOT_URL ?? "/data/snapshot.json";
const OG = SNAP.replace(/snapshot\.json$/, "og.png");
const V = "6d8a4f25"; // brand asset version: bumps when assets/ change

export const metadata: Metadata = {
  title: "dorkbot - by @decentradork - nfa",
  description: "A bot trading its own bag on Bybit. Every trade posted to X.",
  icons: { icon: `/brand/favicon-32.png?v=${V}`, apple: `/brand/apple-touch-icon.png?v=${V}` },
  openGraph: { title: "dorkbot - by @decentradork - nfa", description: "A bot trading its own bag.", images: [{ url: OG }] },
  twitter: { card: "summary_large_image", site: "@decentradork", images: [OG] },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <main>
          <header><a href="/"><img className="avatar" src={`/brand/avatar-96.png?v=${V}`} alt="" /></a><nav><a href="/">Overview</a><a href="/agents/">Agents</a><a href="/disclaimer/">Disclaimer</a></nav><a className="handle" href="https://x.com/decentradork">@decentradork</a></header>
          <a href="/" className="wordmark"><img src={`/brand/logo-1200.png?v=${V}`} alt="dorkbot" /></a>
          {children}
          <footer>nfa. just a bot trading its own bag. · <a href="/disclaimer/">terms &amp; disclaimer</a> · <a href="https://x.com/decentradork">@decentradork</a> · <ShareLink /></footer>
        </main>
      </body>
    </html>
  );
}
