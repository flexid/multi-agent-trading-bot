"""M0 connectivity self-test.

    python -m app.selftest                        # read-only checks + paper round-trip
    python -m app.selftest --live --confirm yes   # also one minimal real order + cancel

Without ``--live --confirm yes`` the Bybit client cannot send an order at all.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from app.config import Config, Secrets, get_config, get_secrets
from app.data.polymarket import PolymarketClient
from app.data.x import XClient
from app.execution.bybit_client import BybitAPIError, BybitClient
from app.execution.bybit_models import OrderRequest, Side
from app.execution.paper import paper_fill, round_trip_pnl

MAX_CLOCK_SKEW_MS = 1000
KEY_EXPIRY_WARN_DAYS = 30
LIVE_PRICE_DISCOUNT = Decimal("0.90")  # rest the self-test order 10% below the bid
DEPTH_PCT = Decimal("0.02")
WANTED_KEY_SCOPES = {"Spot"}


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "FAIL"
    SKIP = "skip"


class Check(BaseModel):
    name: str
    status: Status
    detail: str
    data: dict[str, Any] = {}


CheckFn = Callable[[], Awaitable[Check | list[Check]]]


async def _run(name: str, fn: CheckFn) -> list[Check]:
    try:
        result = await fn()
    except Exception as exc:  # a self-test reports every failure instead of stopping
        return [Check(name=name, status=Status.FAIL, detail=f"{type(exc).__name__}: {exc}")]
    return result if isinstance(result, list) else [result]


def _pct(value: Decimal, places: int = 3) -> str:
    return f"{value * 100:.{places}f}%"


# --- Bybit: public ---------------------------------------------------------


async def check_time(client: BybitClient) -> Check:
    skew = await client.sync_time()
    status = Status.OK if abs(skew) <= MAX_CLOCK_SKEW_MS else Status.WARN
    return Check(
        name="bybit.time",
        status=status,
        detail=f"local clock skew {skew:+d} ms (requests are offset-corrected)",
        data={"skew_ms": skew},
    )


async def check_pair(client: BybitClient, asset: str, symbol: str, *, traded: bool) -> Check:
    """Listing, filters, liquidity and margin terms of one pair."""
    name = f"bybit.pair.{symbol}"
    missing = Status.FAIL if traded else Status.SKIP
    inst = await client.instrument(symbol)
    if inst is None:
        return Check(name=name, status=missing, detail="not listed", data={"asset": asset})
    ticker = await client.ticker(symbol)
    bids, asks = (await client.orderbook(symbol)).depth_quote(DEPTH_PCT)
    base = await client.margin_coin(inst.base_coin)
    quote = await client.margin_coin(inst.quote_coin)
    can_long_lev = inst.margin_enabled and bool(quote and quote.borrowable)
    can_short = inst.margin_enabled and bool(base and base.borrowable)
    data = {
        "asset": asset,
        "status": inst.status,
        "margin_trading": inst.margin_trading,
        "tick_size": inst.price_filter.tick_size,
        "qty_step": inst.lot_size_filter.base_precision,
        "min_order_amt": inst.lot_size_filter.min_order_amt,
        "turnover_24h": ticker.turnover24h,
        "spread_bps": round(ticker.spread_bps, 1),
        "depth_bids_2pct": round(bids),
        "depth_asks_2pct": round(asks),
        "leveraged_long": can_long_lev,
        "short": can_short,
        "base_borrow_apr": base.borrow_apr if base else None,
        "quote_borrow_apr": quote.borrow_apr if quote else None,
        "base_collateral_ratio": base.collateral_ratio if base else None,
    }
    detail = (
        f"{inst.status}, margin={inst.margin_trading}, short={'yes' if can_short else 'no'}, "
        f"24h ${ticker.turnover24h:,.0f}, spread {ticker.spread_bps:.1f} bps, "
        f"±2% depth ${bids:,.0f}/${asks:,.0f}"
    )
    if not inst.is_trading:
        status = missing
    elif traded and not inst.margin_enabled:
        status = Status.WARN  # tradable, but 1x long-only
    else:
        status = Status.OK
    return Check(name=name, status=status, detail=detail, data=data)


async def check_pairs(client: BybitClient, cfg: Config) -> list[Check]:
    checks: list[Check] = []
    for asset in cfg.trading.assets:
        pairs = [(cfg.symbol(asset), True)]
        if cfg.exchange.alt_quote:
            pairs.append((cfg.symbol(asset, cfg.exchange.alt_quote), False))
        for symbol, traded in pairs:
            checks += await _run(
                f"bybit.pair.{symbol}", partial(check_pair, client, asset, symbol, traded=traded)
            )
    return checks


# --- Bybit: account --------------------------------------------------------


async def check_key(client: BybitClient) -> Check:
    info = await client.api_key_info()
    days = info.days_to_expiry()
    extra = sorted(set(info.granted) - WANTED_KEY_SCOPES)
    problems: list[str] = []
    if not info.ip_bound:
        problems.append("not IP-bound")
    if days is not None and days < KEY_EXPIRY_WARN_DAYS:
        problems.append(f"expires in {days} d")
    if extra:
        problems.append(f"wider than needed: {', '.join(extra)}")
    if info.read_only:
        problems.append("read-only")
    if "Spot" not in info.granted:
        problems.append("no spot trade permission")
    expiry = f"expires in {days} d" if days is not None else "no expiry"
    detail = f"VIP '{info.vip_level}', {'master' if info.is_master else 'sub'} account, {expiry}"
    if problems:
        detail += "; " + "; ".join(problems)
    return Check(
        name="bybit.api_key",
        status=Status.WARN if problems else Status.OK,
        detail=detail,
        data={
            "vip_level": info.vip_level,
            "is_master": info.is_master,
            "ip_bound": info.ip_bound,
            "scopes": sorted(info.granted),
            "expired_at": info.expired_at,
            "kyc_region": info.kyc_region,
        },
    )


async def check_account(client: BybitClient) -> Check:
    info = await client.account_info()
    margin = await client.spot_margin_state()
    cross = info.margin_mode in {"REGULAR_MARGIN", "PORTFOLIO_MARGIN"}
    ready = cross and margin.enabled
    detail = (
        f"margin mode {info.margin_mode}, spot margin "
        f"{'on' if margin.enabled else 'off'}, leverage setting '{margin.spot_leverage or '-'}'"
    )
    if not ready:
        detail += "; borrowing needs cross margin and spot margin switched on"
    return Check(
        name="bybit.account",
        status=Status.OK if ready else Status.WARN,
        detail=detail,
        data={"margin_mode": info.margin_mode, "spot_margin_enabled": margin.enabled},
    )


async def check_balance(client: BybitClient, cfg: Config) -> Check:
    wallet = await client.wallet_balance()
    quote = wallet.of(cfg.exchange.quote)
    quote_bal = quote.wallet_balance if quote else Decimal(0)
    others = sorted(c.coin for c in wallet.coin if c.coin != cfg.exchange.quote and c.usd_value)
    borrows = [c.coin for c in wallet.coin if c.borrow_amount]
    detail = (
        f"{cfg.exchange.quote} {quote_bal:,.2f}, total equity ${wallet.total_equity or 0:,.2f}, "
        f"{len(others)} other coins held, {len(borrows)} open borrows"
    )
    notes: list[str] = []
    if quote_bal == 0:
        notes.append(f"no {cfg.exchange.quote} to trade with")
    if others:
        notes.append("account holds assets the bot does not manage")
    return Check(
        name="bybit.balance",
        status=Status.WARN if notes else Status.OK,
        detail=detail + ("; " + "; ".join(notes) if notes else ""),
        data={"quote_balance": quote_bal, "other_coins": len(others), "borrows": borrows},
    )


async def check_fees(client: BybitClient, cfg: Config) -> list[Check]:
    checks: list[Check] = []
    quotes = [cfg.exchange.quote] + ([cfg.exchange.alt_quote] if cfg.exchange.alt_quote else [])
    for asset in cfg.trading.assets:
        for quote in quotes:
            symbol = cfg.symbol(asset, quote)

            async def one(symbol: str = symbol) -> Check:
                fee = await client.fee_rate(symbol)
                return Check(
                    name=f"bybit.fee.{symbol}",
                    status=Status.OK,
                    detail=f"maker {_pct(fee.maker_fee_rate)}, taker {_pct(fee.taker_fee_rate)}",
                    data={"maker": fee.maker_fee_rate, "taker": fee.taker_fee_rate},
                )

            checks += await _run(f"bybit.fee.{symbol}", one)
    return checks


async def check_subaccounts(client: BybitClient) -> Check:
    try:
        count = await client.sub_member_count()
    except BybitAPIError as exc:
        if exc.ret_code == 10005:
            return Check(
                name="bybit.subaccounts",
                status=Status.WARN,
                detail="key lacks subaccount permission; cannot verify from the API",
            )
        raise
    return Check(name="bybit.subaccounts", status=Status.OK, detail=f"{count} subaccounts visible")


# --- Paper and live round-trips ---------------------------------------------


async def check_paper(client: BybitClient, cfg: Config) -> Check:
    symbol = cfg.symbol(cfg.trading.assets[0])
    inst = await client.instrument(symbol)
    if inst is None:
        return Check(name="paper.round_trip", status=Status.FAIL, detail=f"{symbol} not listed")
    book = await client.orderbook(symbol, limit=1)
    qty = inst.min_qty_at(book.best_ask.price)
    fee = Decimal("0.001")
    if client.has_credentials:
        # On failure keep the default fee; the key check reports the cause.
        with contextlib.suppress(BybitAPIError):
            fee = (await client.fee_rate(symbol)).taker_fee_rate
    entry = paper_fill(book, Side.BUY, qty, fee)
    exit_ = paper_fill(book, Side.SELL, qty, fee)
    pnl = round_trip_pnl(entry, exit_)
    ok = pnl < 0 and entry.qty == inst.round_qty(entry.qty)
    return Check(
        name="paper.round_trip",
        status=Status.OK if ok else Status.FAIL,
        detail=(
            f"{symbol} buy {qty} @ {entry.price}, sell @ {exit_.price}, "
            f"net {pnl:.6f} {cfg.exchange.quote} (spread + fees)"
        ),
        data={"qty": qty, "pnl": pnl},
    )


async def check_live_order(client: BybitClient, cfg: Config) -> Check:
    """Place one post-only limit buy far below the market, then cancel it."""
    name = "bybit.live_order"
    symbol = cfg.symbol(cfg.trading.assets[0])
    inst = await client.instrument(symbol)
    if inst is None:
        return Check(name=name, status=Status.FAIL, detail=f"{symbol} not listed")
    ticker = await client.ticker(symbol)
    price = inst.round_price(ticker.bid1_price * LIVE_PRICE_DISCOUNT)
    qty = inst.min_qty_at(price)
    notional = price * qty
    quote = (await client.wallet_balance()).of(inst.quote_coin)
    if quote is None or quote.wallet_balance < notional:
        return Check(
            name=name,
            status=Status.FAIL,
            detail=f"need {notional:.2f} {inst.quote_coin} free; nothing was sent",
        )
    link_id = f"selftest-{uuid.uuid4().hex[:16]}"
    request = OrderRequest(
        symbol=symbol, side=Side.BUY, qty=qty, price=price, order_link_id=link_id, post_only=True
    )
    await client.place_order(request)
    seen = False
    try:
        seen = any(o.order_link_id == link_id for o in await client.open_orders(symbol))
    finally:
        await client.cancel_order(symbol, link_id)
    gone = all(o.order_link_id != link_id for o in await client.open_orders(symbol))
    ok = seen and gone
    return Check(
        name=name,
        status=Status.OK if ok else Status.FAIL,
        detail=(
            f"{symbol} buy {qty} @ {price} ({notional:.2f} {inst.quote_coin}) placed, "
            f"{'seen' if seen else 'NOT seen'} open, {'cancelled' if gone else 'STILL OPEN'}"
        ),
        data={"order_link_id": link_id, "price": price, "qty": qty},
    )


# --- Other sources ----------------------------------------------------------


async def check_perp_data() -> Check:
    """Funding and open interest from Bybit global public data (indicator input only)."""
    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": "BTCUSDT"},
        )
    row = response.json()["result"]["list"][0]
    return Check(
        name="bybit_global.perp_data",
        status=Status.OK,
        detail=(
            f"BTCUSDT funding {row['fundingRate']}, OI ${Decimal(row['openInterestValue']):,.0f}"
        ),
    )


async def check_polymarket() -> list[Check]:
    async with PolymarketClient() as pm:
        markets = await pm.top_markets(limit=20)
        usable = [m for m in markets if len(m.clob_token_ids) == len(m.outcomes) >= 2]
        gamma = Check(
            name="polymarket.gamma",
            status=Status.OK if usable else Status.FAIL,
            detail=(
                f"{len(markets)} markets, {len(usable)} with parsed outcomes, prices and token ids"
            ),
        )
        if not usable:
            return [gamma]
        token = usable[0].clob_token_ids[0]

        async def clob() -> Check:
            book = await pm.book(token)
            mid = await pm.midpoint(token)
            history = await pm.price_history(token)
            return Check(
                name="polymarket.clob",
                status=Status.OK if history else Status.WARN,
                detail=(
                    f"market {usable[0].id}: mid {mid}, book {len(book.bids)}/{len(book.asks)} "
                    f"levels, {len(history)} hourly points in 24h"
                ),
            )

        async def data() -> Check:
            count = await pm.recent_trade_count()
            return Check(
                name="polymarket.data_api",
                status=Status.OK if count else Status.WARN,
                detail=f"{count} recent trades returned",
            )

        return [
            gamma,
            *await _run("polymarket.clob", clob),
            *await _run("polymarket.data_api", data),
        ]


async def check_x(secrets: Secrets, cfg: Config) -> list[Check]:
    if not secrets.x_bearer_token.get_secret_value():
        return [Check(name="x.read", status=Status.FAIL, detail="X_BEARER_TOKEN is empty")]
    async with XClient(secrets.x_bearer_token) as x:

        async def user() -> Check:
            found = await x.user_by_username(cfg.posting.handle)
            return Check(
                name="x.user_lookup", status=Status.OK, detail=f"@{found.username} id {found.id}"
            )

        async def search() -> Check:
            cashtag = cfg.posting.cashtags[cfg.trading.assets[0]].lstrip("$")
            posts = await x.search_recent(f"{cashtag} lang:en -is:retweet")
            # Post text is untrusted; report counts and timestamps only.
            newest = max((p.created_at for p in posts if p.created_at), default=None)
            return Check(
                name="x.search_recent",
                status=Status.OK if posts else Status.WARN,
                detail=f"{len(posts)} posts read (billed), newest {newest:%Y-%m-%d %H:%M} UTC"
                if newest
                else f"{len(posts)} posts read",
                data={"posts_read": len(posts)},
            )

        return [*await _run("x.user_lookup", user), *await _run("x.search_recent", search)]


# --- Runner -----------------------------------------------------------------


async def run(cfg: Config, secrets: Secrets, *, live: bool, with_x: bool) -> list[Check]:
    checks: list[Check] = []
    async with BybitClient(
        secrets.bybit_base_url,
        secrets.bybit_api_key,
        secrets.bybit_api_secret,
        recv_window_ms=cfg.exchange.recv_window_ms,
        allow_orders=live,
    ) as bybit:
        checks += await _run("bybit.time", lambda: check_time(bybit))
        checks += await check_pairs(bybit, cfg)
        if bybit.has_credentials:
            checks += await _run("bybit.api_key", lambda: check_key(bybit))
            checks += await _run("bybit.account", lambda: check_account(bybit))
            checks += await _run("bybit.balance", lambda: check_balance(bybit, cfg))
            checks += await check_fees(bybit, cfg)
            checks += await _run("bybit.subaccounts", lambda: check_subaccounts(bybit))
        else:
            checks.append(
                Check(name="bybit.api_key", status=Status.FAIL, detail="BYBIT_API_KEY/SECRET empty")
            )
        checks += await _run("paper.round_trip", lambda: check_paper(bybit, cfg))
        if live:
            checks += await _run("bybit.live_order", lambda: check_live_order(bybit, cfg))
        else:
            checks.append(
                Check(
                    name="bybit.live_order",
                    status=Status.SKIP,
                    detail="run `make selftest-live CONFIRM=yes`",
                )
            )
    checks += await _run("bybit_global.perp_data", check_perp_data)
    checks += await _run("polymarket", check_polymarket)
    if with_x:
        checks += await _run("x.read", lambda: check_x(secrets, cfg))
    else:
        checks.append(Check(name="x.read", status=Status.SKIP, detail="--no-x"))
    return checks


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    return "\n".join(f"{c.status.value:<4}  {c.name:<{width}}  {c.detail}" for c in checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--live", action="store_true", help="place and cancel one real order")
    parser.add_argument("--confirm", default="", help="must be 'yes' together with --live")
    parser.add_argument("--no-x", action="store_true", help="skip the billed X read (~$0.06)")
    parser.add_argument("--json", type=Path, help="also write the results to this file")
    args = parser.parse_args(argv)

    if args.live and args.confirm != "yes":
        print("refusing: --live needs --confirm yes (make selftest-live CONFIRM=yes)")
        return 2

    cfg, secrets = get_config(), get_secrets()
    print(f"self-test {datetime.now(UTC):%Y-%m-%d %H:%M} UTC against {secrets.bybit_base_url}")
    checks = asyncio.run(run(cfg, secrets, live=args.live, with_x=not args.no_x))
    print(render(checks))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps([c.model_dump(mode="json") for c in checks], indent=2) + "\n"
        )
    failed = [c for c in checks if c.status is Status.FAIL]
    print(f"\n{len(checks)} checks, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
