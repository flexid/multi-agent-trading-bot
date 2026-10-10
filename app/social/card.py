"""Trade card for an exceptional close (owner, 2026-10-09): a 1200×675 PNG with the
wordmark, the cashtag and side, the result and the holding time, attached to the X post.
Only prices, leverage, percentages and durations appear on it, like in the text."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from app.social.templates import TradeFacts, fmt, pct

BRAND = Path(__file__).resolve().parents[2] / "site" / "public" / "brand"
ORANGE, GREEN, RED, GREY, FG, BG = "#fe7e1c", "#b6ff00", "#ff5c5c", "#8f958f", "#ececec", "#0a0a0a"


def render(f: TradeFacts) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    assert f.exit is not None and f.pnl_price_pct is not None and f.pnl_margin_pct is not None
    img = Image.new("RGB", (1200, 675), BG)
    d = ImageDraw.Draw(img)

    def font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            return ImageFont.load_default(size)

    huge, big, mid, small = (
        font("DejaVuSans-Bold.ttf", 120),
        font("DejaVuSans-Bold.ttf", 56),
        font("DejaVuSans.ttf", 34),
        font("DejaVuSans.ttf", 26),
    )
    try:
        logo = Image.open(BRAND / "logo-600.png").convert("RGBA")
        logo = logo.resize((420, round(logo.height * 420 / logo.width)), Image.Resampling.LANCZOS)
        img.paste(logo, (60, 48), logo)
    except OSError:
        d.text((60, 48), "dorkbot", font=big, fill=ORANGE)
    side_col = GREEN if f.direction == "long" else RED
    d.text((60, 190), f.cashtag, font=big, fill=FG)
    w = d.textlength(f.cashtag, font=big)
    d.text((60 + w + 24, 190), f.direction, font=big, fill=side_col)
    d.text(
        (60 + w + 24 + d.textlength(f.direction, font=big) + 24, 204),
        f"{float(f.leverage):g}x",
        font=mid,
        fill=GREY,
    )
    res_col = GREEN if f.pnl_margin_pct >= 0 else RED
    d.text((60, 280), pct(f.pnl_margin_pct), font=huge, fill=res_col)
    d.text((60, 420), "on margin", font=mid, fill=GREY)
    line = f"{fmt(f.entry)} → {fmt(f.exit)}   ·   {pct(f.pnl_price_pct)} on price"
    d.text((60, 490), f"{line}   ·   {f.holding or 'a while'}", font=mid, fill=FG)
    foot = "paper trade  ·  dorkbot.dev  ·  nfa" if f.paper else "dorkbot.dev  ·  nfa"
    d.text((60, 600), foot, font=small, fill=GREY)
    try:
        avatar = (
            Image.open(BRAND / "avatar-512.png")
            .convert("RGBA")
            .resize((220, 220), Image.Resampling.LANCZOS)
        )
        img.paste(avatar, (920, 400), avatar)
    except OSError:
        pass
    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
