"""Build the public snapshot, verify it, render the OG image, push both to R2 (M8a).

Runs every ``site.snapshot_interval_minutes`` from the scheduler. Without R2 credentials
it only writes ``site/public/data/`` locally (useful for `next dev`).

    python -m app.site.publish
"""

from __future__ import annotations

import io
import logging
import sys

from app.config import get_secrets
from app.site.snapshot import OUT, Snapshot, build, verify

log = logging.getLogger("publish")


def og_image(snap: Snapshot) -> bytes:
    """1200×630 PNG with the headline numbers; numbers pass the whitelist by construction."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1200, 630), "#0b0d10")
    d = ImageDraw.Draw(img)

    def font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            return ImageFont.load_default(size)

    big = font("DejaVuSans-Bold.ttf", 72)
    mid = font("DejaVuSans.ttf", 40)
    small = font("DejaVuSans.ttf", 28)
    p = snap.performance
    d.text((60, 60), "dorkbot", font=big, fill="#e6e6e6")
    d.text((60, 150), f"{snap.mode} · a bot trading its own bag", font=small, fill="#8a919a")
    col = "#3ddc84" if p.bot_pct >= 0 else "#ff5c5c"
    d.text((60, 250), f"{p.bot_pct:+.2f}%", font=big, fill=col)
    d.text(
        (60, 340),
        f"vs BTC {p.btc_hold_pct:+.2f}%  ·  basket {p.basket_pct:+.2f}%",
        font=mid,
        fill="#e6e6e6",
    )
    d.text(
        (60, 410),
        f"drawdown {p.drawdown_pct:+.2f}%  ·  {snap.stats.trades} trades",
        font=mid,
        fill="#8a919a",
    )
    d.text(
        (60, 540),
        f"@{snap.handle} · {snap.generated_at:%Y-%m-%d %H:%M} UTC",
        font=small,
        fill="#8a919a",
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def push_r2(objects: dict[str, tuple[bytes, str]]) -> bool:
    s = get_secrets()
    if not (s.cloudflare_account_id and s.r2_access_key_id.get_secret_value()):
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
            Bucket=s.r2_bucket,
            Key=key,
            Body=body,
            ContentType=ctype,
            CacheControl="public, max-age=60",
        )
    return True


def publish() -> Snapshot:
    snap = build()
    problems = verify(snap)
    if problems:
        raise RuntimeError("snapshot failed the whitelist: " + "; ".join(problems[:5]))
    data = snap.model_dump_json(indent=1).encode()
    og = og_image(snap)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(data)
    OUT.with_name("og.png").write_bytes(og)
    pushed = push_r2({"snapshot.json": (data, "application/json"), "og.png": (og, "image/png")})
    log.info(
        "snapshot %s (%d bytes)%s",
        snap.generated_at,
        len(data),
        " pushed to R2" if pushed else " written locally",
    )
    return snap


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    publish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
