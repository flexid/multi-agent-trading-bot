import type { Metadata } from "next";
import "./globals.css";
import ShareLink from "./share";

const V = "d3";

export const metadata: Metadata = {
  title: "dorkbot DEX - the same bot, on-chain - nfa",
  description: "dorkbot's record mirrored on Hyperliquid with real funds on a public address. The ambition: a vault anyone can deposit into.",
  icons: { icon: `/brand/favicon-32.png?v=${V}`, apple: `/brand/apple-touch-icon.png?v=${V}` },
  openGraph: { title: "dorkbot dex - nfa", description: "dorkbot's record mirrored on-chain. The ambition: a vault anyone can deposit into." },
  twitter: { card: "summary", site: "@decentradork" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <main>
          <header><a href="/"><img className="avatar" src={`/brand/avatar-96.png?v=${V}`} alt="" /></a><nav><a href="/">Mirror</a><a href="/how/">How it works</a><a href="https://dorkbot.dev/">dorkbot.dev</a></nav><a className="handle" href="https://x.com/decentradork">@decentradork</a></header>
          <a href="/" className="wordmark"><img src={`/brand/logo-dex-1200.png?v=${V}`} alt="dorkbot dex" /></a>
          {children}
          <div className="sign-off"><img src={`/brand/avatar-512.png?v=${V}`} alt="" /></div>
          <footer>nfa. the same dorky bot, on-chain · <a href="https://dorkbot.dev/disclaimer/" className="neon">terms &amp; disclaimer</a> · <a href="https://x.com/decentradork">@decentradork</a> · <ShareLink /></footer>
        </main>
      </body>
    </html>
  );
}
