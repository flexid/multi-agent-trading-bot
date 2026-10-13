import type { Metadata } from "next";
import "./globals.css";
import ShareLink from "./share";

const V = "c3"; // brand asset version: bumps when brand/ changes
const OG = "https://competition.dorkbot.dev/brand/og-competition.png";

export const metadata: Metadata = {
  title: "dorkbot arena - three dorky ducks trading one bag - nfa",
  description: "Three AI traders compete on BTC. Who is ahead, who is benched, every trade.",
  icons: { icon: `/brand/favicon-32.png?v=${V}`, apple: `/brand/apple-touch-icon.png?v=${V}` },
  openGraph: { title: "dorkbot arena - nfa", description: "Three AI traders compete on BTC. Who is ahead, who is benched, every trade.", images: [{ url: OG }] },
  twitter: { card: "summary_large_image", site: "@decentradork", images: [OG] },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <main>
          <header><a href="/"><img className="avatar" src={`/brand/avatar-96.png?v=${V}`} alt="" /></a><nav><a href="/">Arena</a><a href="/leaderboard/">Leaderboard</a><a href="/rules/">Rules</a></nav><a className="handle" href="https://x.com/decentradork">@decentradork</a></header>
          <a href="/" className="wordmark"><img src={`/brand/logo-arena-1200.png?v=${V}`} alt="dorkbot arena" /></a>
          {children}
          <div className="sign-off"><img src={`/brand/avatar-512.png?v=${V}`} alt="" /></div>
          <footer>nfa. three dorky ducks trading one bag · <a href="https://dorkbot.dev/disclaimer/" className="neon">terms &amp; disclaimer</a> · <a href="https://x.com/decentradork">@decentradork</a> · <ShareLink /></footer>
        </main>
      </body>
    </html>
  );
}
