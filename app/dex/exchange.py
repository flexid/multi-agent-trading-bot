"""The Hyperliquid adapter and the interface the mirror trades through."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

log = logging.getLogger("dex.exchange")

# dorkbot asset -> Hyperliquid coin. kPEPE is PEPE in thousands (prices and sizes × 1000).
COINS = {"PEPE": "kPEPE", "SPX6900": "SPX"}
UNIT = {"PEPE": Decimal(1000)}  # dorkbot units per Hyperliquid unit


def coin_of(asset: str) -> str:
    return COINS.get(asset, asset)


@dataclass(frozen=True)
class Instrument:
    coin: str
    sz_decimals: int
    max_leverage: int


@dataclass(frozen=True)
class Position:
    asset: str
    side: str  # long | short
    qty: Decimal  # in Hyperliquid units
    entry: Decimal
    leverage: int
    unrealized_pnl: Decimal
    margin: Decimal
    liquidation: Decimal | None


@dataclass(frozen=True)
class Fill:
    """A closing fill as the exchange reports it: pnl net of the fee."""

    asset: str
    side: str  # the position's side
    qty: Decimal
    price: Decimal
    fee: Decimal
    pnl: Decimal
    at: datetime
    tx: str | None


class DexExchange(Protocol):
    async def instruments(self) -> dict[str, Instrument]: ...
    async def mids(self) -> dict[str, Decimal]: ...
    async def equity(self) -> tuple[Decimal, Decimal]: ...
    async def positions(self) -> dict[str, Position]: ...
    async def set_leverage(self, asset: str, leverage: int) -> None: ...
    async def open_market(
        self, asset: str, side: str, qty: Decimal, tp: Decimal, sl: Decimal, cloid: str
    ) -> None: ...
    async def close_market(self, asset: str, cloid: str) -> None: ...
    async def closing_fills(self, since: datetime) -> list[Fill]: ...


def round_size(qty: Decimal, sz_decimals: int) -> Decimal:
    return qty.quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_DOWN)


def round_price(px: Decimal) -> float:
    """Hyperliquid prices: at most 5 significant figures and 6 decimals."""
    px = px.quantize(Decimal("0.000001"))
    s = f"{px:f}".rstrip("0").rstrip(".")
    digits = len(s.replace(".", "").lstrip("0"))
    if digits > 5:
        px = px.quantize(Decimal(1).scaleb(px.adjusted() - 4))
    return float(px)


class HyperliquidExchange:
    """Signed trading through the official SDK with an API (agent) key; public reads
    without one. ``testnet`` picks the network."""

    def __init__(self, *, address: str, private_key: str | None, testnet: bool) -> None:
        from hyperliquid.info import Info
        from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

        self.url = TESTNET_API_URL if testnet else MAINNET_API_URL
        self.address = address
        self.info = Info(base_url=self.url, skip_ws=True)
        self._ex: Any = None
        if private_key:
            from eth_account import Account
            from hyperliquid.exchange import Exchange

            wallet = Account.from_key(private_key)
            self._ex = Exchange(wallet, base_url=self.url, account_address=address)
        self._instruments: dict[str, Instrument] | None = None

    async def instruments(self) -> dict[str, Instrument]:
        if self._instruments is None:
            meta = self.info.meta()
            by_coin = {u["name"]: u for u in meta["universe"]}
            out = {}
            for asset in (
                "BTC",
                "ETH",
                "SOL",
                "XRP",
                "DOGE",
                "PEPE",
                "HBAR",
                "PUMP",
                "SPX6900",
                "ENA",
            ):
                u = by_coin.get(coin_of(asset))
                if u is not None and not u.get("isDelisted"):
                    out[asset] = Instrument(
                        coin_of(asset), int(u["szDecimals"]), int(u["maxLeverage"])
                    )
            self._instruments = out
        return self._instruments

    async def mids(self) -> dict[str, Decimal]:
        mids = self.info.all_mids()
        out = {}
        for asset, inst in (await self.instruments()).items():
            if inst.coin in mids:
                out[asset] = Decimal(str(mids[inst.coin])) / UNIT.get(asset, Decimal(1))
        return out

    async def equity(self) -> tuple[Decimal, Decimal]:
        st = self.info.user_state(self.address)
        ms = st.get("marginSummary", {})
        return Decimal(str(ms.get("accountValue", "0"))), Decimal(str(st.get("withdrawable", "0")))

    async def positions(self) -> dict[str, Position]:
        st = self.info.user_state(self.address)
        by_coin = {v: k for k, v in ((a, coin_of(a)) for a in (await self.instruments()))}
        out = {}
        for row in st.get("assetPositions", []):
            p = row.get("position", {})
            szi = Decimal(str(p.get("szi", "0")))
            if szi == 0 or p.get("coin") not in by_coin:
                continue
            asset = by_coin[p["coin"]]
            lev = p.get("leverage", {})
            out[asset] = Position(
                asset=asset,
                side="long" if szi > 0 else "short",
                qty=abs(szi),
                entry=Decimal(str(p.get("entryPx", "0"))) / UNIT.get(asset, Decimal(1)),
                leverage=int(lev.get("value", 1)),
                unrealized_pnl=Decimal(str(p.get("unrealizedPnl", "0"))),
                margin=Decimal(str(p.get("marginUsed", "0"))),
                liquidation=Decimal(str(p["liquidationPx"])) if p.get("liquidationPx") else None,
            )
        return out

    def _need_signer(self) -> Any:
        if self._ex is None:
            raise RuntimeError("no DEX key: set DEX_PRIVATE_KEY for testnet or live trading")
        return self._ex

    async def set_leverage(self, asset: str, leverage: int) -> None:
        self._need_signer().update_leverage(leverage, coin_of(asset), is_cross=False)

    async def open_market(
        self, asset: str, side: str, qty: Decimal, tp: Decimal, sl: Decimal, cloid: str
    ) -> None:
        """A market entry, then the take and the stop as reduce-only trigger orders that
        the exchange holds (the mirror's safety net)."""
        from hyperliquid.utils.types import Cloid

        ex = self._need_signer()
        coin = coin_of(asset)
        unit = UNIT.get(asset, Decimal(1))
        is_buy = side == "long"
        r = ex.market_open(coin, is_buy, float(qty), cloid=Cloid.from_str(cloid))
        _check(r, "open")
        for kind, px in (("tp", tp * unit), ("sl", sl * unit)):
            r = ex.order(
                coin,
                not is_buy,
                float(qty),
                round_price(px),
                {"trigger": {"triggerPx": round_price(px), "isMarket": True, "tpsl": kind}},
                reduce_only=True,
            )
            _check(r, kind)

    async def close_market(self, asset: str, cloid: str) -> None:
        from hyperliquid.utils.types import Cloid

        ex = self._need_signer()
        r = ex.market_close(coin_of(asset), cloid=Cloid.from_str(cloid))
        _check(r, "close")
        # the resting take and stop of that position go too
        for o in self.info.open_orders(self.address):
            if o.get("coin") == coin_of(asset) and o.get("reduceOnly", True):
                ex.cancel(coin_of(asset), int(o["oid"]))

    async def closing_fills(self, since: datetime) -> list[Fill]:
        rows = self.info.user_fills_by_time(self.address, int(since.timestamp() * 1000))
        by_coin = {coin_of(a): a for a in (await self.instruments())}
        out = []
        for f in rows:
            if (
                f.get("coin") not in by_coin
                or Decimal(str(f.get("closedPnl", "0"))) == 0
                and "Close" not in str(f.get("dir", ""))
            ):
                continue
            asset = by_coin[f["coin"]]
            unit = UNIT.get(asset, Decimal(1))
            out.append(
                Fill(
                    asset=asset,
                    side="long" if "Long" in str(f.get("dir", "")) else "short",
                    qty=Decimal(str(f["sz"])),
                    price=Decimal(str(f["px"])) / unit,
                    fee=Decimal(str(f.get("fee", "0"))),
                    pnl=Decimal(str(f.get("closedPnl", "0"))) - Decimal(str(f.get("fee", "0"))),
                    at=datetime.fromtimestamp(int(f["time"]) / 1000, tz=UTC),
                    tx=f.get("hash"),
                )
            )
        return sorted(out, key=lambda x: x.at)


def _check(response: Any, what: str) -> None:
    if not isinstance(response, dict) or response.get("status") != "ok":
        raise RuntimeError(f"hyperliquid {what}: {response}")
    statuses = response.get("response", {}).get("data", {}).get("statuses", [])
    for s in statuses:
        if isinstance(s, dict) and "error" in s:
            raise RuntimeError(f"hyperliquid {what}: {s['error']}")
