"""Build the public snapshot, verify it, render the OG image, push both to R2 (M8a).

Runs every ``site.snapshot_interval_minutes`` from the scheduler. Without R2 credentials
it only writes ``site/public/data/`` locally (useful for `next dev`).

    python -m app.site.publish
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import sys
from pathlib import Path

from app.config import get_secrets
from app.site.snapshot import OUT, Snapshot, build, verify

log = logging.getLogger("publish")


def og_image(snap: Snapshot) -> bytes:
    """1200×630 PNG: wordmark, avatar, headline numbers in the brand palette."""
    from PIL import Image, ImageDraw, ImageFont

    brand = Path(__file__).resolve().parents[2] / "site" / "public" / "brand"
    img = Image.new("RGB", (1200, 630), "#0a0a0a")
    d = ImageDraw.Draw(img)

    def font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            return ImageFont.load_default(size)

    big, mid, small = (
        font("DejaVuSans-Bold.ttf", 80),
        font("DejaVuSans.ttf", 36),
        font("DejaVuSans.ttf", 26),
    )
    try:
        logo = Image.open(brand / "logo-600.png").convert("RGBA")
        logo = logo.resize((560, round(logo.height * 560 / logo.width)), Image.Resampling.LANCZOS)
        img.paste(logo, (50, 40), logo)
        avatar = (
            Image.open(brand / "avatar-512.png")
            .convert("RGBA")
            .resize((300, 300), Image.Resampling.LANCZOS)
        )
        img.paste(avatar, (850, 165), avatar)
    except OSError:
        d.text((60, 60), "dorkbot", font=big, fill="#fe7e1c")
    p = snap.performance
    col = "#b6ff00" if p.bot_pct >= 0 else "#ff5c5c"
    d.text((60, 270), f"{p.bot_pct:+.2f}%", font=big, fill=col)
    d.text(
        (60, 370),
        f"vs BTC {p.btc_hold_pct:+.2f}%  ·  basket {p.basket_pct:+.2f}%",
        font=mid,
        fill="#ececec",
    )
    d.text(
        (60, 425),
        f"drawdown {p.drawdown_pct:+.2f}%  ·  {snap.stats.trades} trades",
        font=mid,
        fill="#8f958f",
    )
    d.text(
        (60, 540),
        f"{snap.mode} · @{snap.handle} · {snap.generated_at:%Y-%m-%d %H:%M} UTC",
        font=small,
        fill="#fe7e1c",
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def push_r2(objects: dict[str, tuple[bytes, str]], bucket: str | None = None) -> bool:
    s = get_secrets()
    bucket = bucket or s.r2_bucket
    if not (s.cloudflare_account_id and s.r2_access_key_id.get_secret_value() and bucket):
        return False
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{s.cloudflare_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=s.r2_access_key_id.get_secret_value(),
        aws_secret_access_key=s.r2_secret_access_key.get_secret_value(),
        region_name="auto",
    )
    for key, (body, ctype) in objects.items():
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType=ctype,
            CacheControl="public, max-age=60",
        )
    return True


def publish(push: bool = True) -> Snapshot:
    snap = build()
    problems = verify(snap)
    if problems:
        raise RuntimeError("snapshot failed the whitelist: " + "; ".join(problems[:5]))
    data = snap.model_dump_json(indent=1).encode()
    og = og_image(snap)
    # A content-hashed copy: link previews (WhatsApp, X) cache by URL, so the URL must
    # change whenever the numbers on the image change. The site's edge function points
    # og:image at the current one via og-latest.json.
    p = snap.performance
    stamp = hashlib.md5(
        f"{p.bot_pct:.2f}|{p.btc_hold_pct:.2f}|{p.basket_pct:.2f}|{p.drawdown_pct:.2f}|"
        f"{snap.stats.trades}|{snap.mode}".encode()
    ).hexdigest()[:10]
    base = get_secrets().snapshot_public_url.rsplit("/", 1)[0]
    latest = json.dumps({"url": f"{base}/og-{stamp}.png", "stamp": stamp}).encode()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(data)
    OUT.with_name("og.png").write_bytes(og)
    OUT.with_name("og-latest.json").write_bytes(latest)
    pushed = push and push_r2(
        {
            "snapshot.json": (data, "application/json"),
            "og.png": (og, "image/png"),
            f"og-{stamp}.png": (og, "image/png"),
            "og-latest.json": (latest, "application/json"),
        }
    )
    log.info(
        "snapshot %s (%d bytes)%s",
        snap.generated_at,
        len(data),
        " pushed to R2" if pushed else " written locally",
    )
    return snap


def main() -> int:
    """Writes locally; pushes to R2 only with --push (the server's scheduler always pushes)."""
    logging.basicConfig(level=logging.INFO)
    publish(push="--push" in sys.argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
