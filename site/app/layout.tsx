import type { Metadata } from "next";
import "./globals.css";

const SNAP = process.env.NEXT_PUBLIC_SNAPSHOT_URL ?? "/data/snapshot.json";
const OG = SNAP.replace(/snapshot\.json$/, "og.png");

export const metadata: Metadata = {
  title: "dorkbot",
  description: "A bot trading its own bag on Bybit. Every trade posted to X.",
  openGraph: { title: "dorkbot", description: "A bot trading its own bag.", images: [{ url: OG }] },
  twitter: { card: "summary_large_image", site: "@decentradork", images: [OG] },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <main>
          <nav><a href="/">Overview</a><a href="/agents/">Agents</a><a href="https://x.com/decentradork">@decentradork</a></nav>
          {children}
          <footer>nfa. just a bot trading its own bag. · <a href="https://x.com/decentradork">@decentradork</a></footer>
        </main>
      </body>
    </html>
  );
}
