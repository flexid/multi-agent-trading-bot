import type { NextConfig } from "next";

// Static export for Cloudflare Pages, like dorkbot.dev. Data comes from the competition
// bot's API (NEXT_PUBLIC_COMPETITION_API); nothing on the server is reachable from here.
const nextConfig: NextConfig = { output: "export", trailingSlash: true, images: { unoptimized: true } };
export default nextConfig;
