import type { Metadata } from "next";
import "./globals.css";

const SNAP = process.env.NEXT_PUBLIC_SNAPSHOT_URL ?? "/data/snapshot.json";
const OG = SNAP.replace(/snapshot\.json$/, "og.png");

export const metadata: Metadata = {
  title: "dorkbot",
  description: "A bot trading its own bag on Bybit. Every trade posted to X.",
  icons: { icon: "/brand/favicon-32.png", apple: "/brand/apple-touch-icon.png" },
  openGraph: { title: "dorkbot", description: "A bot trading its own bag.", images: [{ url: OG }] },
  twitter: { card: "summary_large_image", site: "@decentradork", images: [OG] },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <main>
          <header><a href="/"><img className="avatar" src="/brand/avatar-96.png" alt="" /></a><nav><a href="/">Overview</a><a href="/agents/">Agents</a><a href="https://x.com/decentradork">@decentradork</a></nav></header>
          <a href="/" className="wordmark"><img src="/brand/logo-1200.png" alt="dorkbot" /></a>
          {children}
          <footer>nfa. just a bot trading its own bag. · <a href="https://x.com/decentradork">@decentradork</a></footer>
        </main>
      </body>
    </html>
  );
}
