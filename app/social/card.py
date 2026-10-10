"""Trade card for an exceptional close (owner, 2026-10-09): a 1200×675 PNG with the
wordmark, the cashtag and side, the result and the holding time, attached to the X post.
Only prices, leverage, percentages and durations appear on it, like in the text."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from app.social.templates import TradeFacts, fmt, pct

BRAND = Path(__file__).resolve().parents[2] / "site" / "public" / "brand"
ORANGE, GREEN, RED, GREY, FG, BG = "#fe7e1c", "#b6ff00", "#ff5c5c", "#8f958f", "#ececec", "#0a0a0a"
RIGHT_EDGE = 1140  # the avatar's right edge; the top-right line aligns to it


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

    huge, big, mid, small, lev_font = (
        font("DejaVuSans-Bold.ttf", 120),
        font("DejaVuSans-Bold.ttf", 56),
        font("DejaVuSans.ttf", 34),
        font("DejaVuSans.ttf", 26),
        font("DejaVuSans-Bold.ttf", 68),
    )
    logo_mid_y = 48 + 60  # fallback centre line when the wordmark cannot be loaded
    try:
        logo = Image.open(BRAND / "logo-600.png").convert("RGBA")
        logo = logo.resize((420, round(logo.height * 420 / logo.width)), Image.Resampling.LANCZOS)
        img.paste(logo, (60, 48), logo)
        logo_mid_y = 48 + logo.height // 2
    except OSError:
        d.text((60, 48), "dorkbot", font=big, fill=ORANGE)
    # "$SOL long 2x" top right: right edge on the avatar's, centred on the wordmark
    side_col = GREEN if f.direction == "long" else RED
    lev = f"{float(f.leverage):g}x"
    w_tag, w_side, w_lev = (
        d.textlength(f.cashtag, font=big),
        d.textlength(f.direction, font=big),
        d.textlength(lev, font=lev_font),
    )
    x = RIGHT_EDGE - (w_tag + 24 + w_side)
    d.text((x, logo_mid_y - 34), f.cashtag, font=big, fill=FG, anchor="lm")
    d.text((x + w_tag + 24, logo_mid_y - 34), f.direction, font=big, fill=side_col, anchor="lm")
    d.text((RIGHT_EDGE - w_lev, logo_mid_y + 38), lev, font=lev_font, fill=GREY, anchor="lm")
    res_col = GREEN if f.pnl_margin_pct >= 0 else RED
    d.text((60, 250), pct(f.pnl_margin_pct), font=huge, fill=res_col)
    d.text((60, 390), "on margin", font=mid, fill=GREY)
    line = f"{fmt(f.entry)} to {fmt(f.exit)}   ·   {pct(f.pnl_price_pct)} on price"
    d.text((60, 470), f"{line}   ·   {f.holding or 'a while'}", font=mid, fill=FG)
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
