import type { NextConfig } from "next";

// Static export for Cloudflare Pages. Data comes from the snapshot the bot pushes every
// 5 minutes (NEXT_PUBLIC_SNAPSHOT_URL); nothing on the server is reachable from here.
const nextConfig: NextConfig = { output: "export", trailingSlash: true, images: { unoptimized: true } };
export default nextConfig;
