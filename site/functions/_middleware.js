// Cloudflare Pages edge function: point og:image / twitter:image at the current
// content-hashed preview image, so WhatsApp and X never show a stale cached preview.
const SNAPSHOT_BASE = "https://data.dorkbot.dev";

export async function onRequest(context) {
  const response = await context.next();
  const type = response.headers.get("content-type") || "";
  if (!type.includes("text/html")) return response;
  let url = null;
  try {
    const r = await fetch(`${SNAPSHOT_BASE}/og-latest.json`, { cf: { cacheTtl: 60, cacheEverything: true } });
    if (r.ok) url = (await r.json()).url;
  } catch {}
  if (!url) return response;
  return new HTMLRewriter()
    .on('meta[property="og:image"]', { element(e) { e.setAttribute("content", url); } })
    .on('meta[name="twitter:image"]', { element(e) { e.setAttribute("content", url); } })
    .transform(response);
}
