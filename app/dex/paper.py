"""The paper twin of the DEX adapter: real mid prices from the venue, simulated fills, the
take and the stop checked against every price pass. Fees as on the venue. Used in paper
mode and by the tests, which never sign anything."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from app.dex.exchange import Fill, Instrument, Position


class PaperDex:
    def __init__(self, *, equity: Decimal, taker_fee_pct: Decimal, prices: PriceSource) -> None:
        self.cash = equity
        self.fee = taker_fee_pct / 100
        self._prices = prices
        self._lev: dict[str, int] = {}
        self._pos: dict[str, Position] = {}
        self._tpsl: dict[str, tuple[Decimal, Decimal]] = {}
        self._open_fee: dict[str, Decimal] = {}
        self._fills: list[Fill] = []
        self.now: datetime | None = None

    def _now(self) -> datetime:
        return self.now or datetime.now(UTC)

    async def instruments(self) -> dict[str, Instrument]:
        return await self._prices.instruments()

    async def mids(self) -> dict[str, Decimal]:
        return await self._prices.mids()

    async def equity(self) -> tuple[Decimal, Decimal]:
        mids = await self.mids()
        eq = self.cash
        for a, p in self._pos.items():
            px = mids.get(a, p.entry)
            eq += p.margin + (px - p.entry) * p.qty * (1 if p.side == "long" else -1)
        return eq, self.cash

    async def positions(self) -> dict[str, Position]:
        mids = await self.mids()
        out = {}
        for a, p in self._pos.items():
            px = mids.get(a, p.entry)
            unreal = (px - p.entry) * p.qty * (1 if p.side == "long" else -1)
            out[a] = Position(**{**p.__dict__, "unrealized_pnl": unreal})
        return out

    async def set_leverage(self, asset: str, leverage: int) -> None:
        self._lev[asset] = leverage

    async def open_market(
        self, asset: str, side: str, qty: Decimal, tp: Decimal, sl: Decimal, cloid: str
    ) -> None:
        if asset in self._pos:
            raise RuntimeError(f"paper: {asset} already open")
        px = (await self.mids())[asset]
        lev = self._lev.get(asset, 1)
        notional = px * qty
        margin = notional / lev
        fee = notional * self.fee
        self.cash -= margin + fee
        self._open_fee[asset] = fee
        self._pos[asset] = Position(asset, side, qty, px, lev, Decimal(0), margin, None)
        self._tpsl[asset] = (tp, sl)

    async def close_market(self, asset: str, cloid: str) -> None:
        if asset in self._pos:
            await self._settle(asset, (await self.mids())[asset])

    async def closing_fills(self, since: datetime) -> list[Fill]:
        return [f for f in self._fills if f.at >= since]

    async def on_prices(self) -> None:
        """The exchange-side take and stop: fire when the mid crosses them."""
        mids = await self.mids()
        for asset in list(self._pos):
            p, (tp, sl) = self._pos[asset], self._tpsl[asset]
            px = mids.get(asset)
            if px is None:
                continue
            if p.side == "long":
                if px <= sl:
                    await self._settle(asset, sl)
                elif px >= tp:
                    await self._settle(asset, tp)
            elif px >= sl:
                await self._settle(asset, sl)
            elif px <= tp:
                await self._settle(asset, tp)

    async def _settle(self, asset: str, price: Decimal) -> None:
        p = self._pos.pop(asset)
        self._tpsl.pop(asset, None)
        gross = (price - p.entry) * p.qty * (1 if p.side == "long" else -1)
        fee = price * p.qty * self.fee
        self.cash += p.margin + gross - fee
        self._fills.append(
            Fill(
                asset,
                p.side,
                p.qty,
                price,
                fee + self._open_fee.pop(asset, Decimal(0)),
                gross - fee - self._open_fee.get(asset, Decimal(0)),
                self._now(),
                None,
            )
        )

    def restore(
        self, cash: Decimal, positions: dict[str, tuple[Position, Decimal, Decimal, Decimal]]
    ) -> None:
        """After a restart: cash and each open position with (tp, sl, entry fee)."""
        self.cash = cash
        for asset, (p, tp, sl, fee) in positions.items():
            self._pos[asset], self._tpsl[asset], self._open_fee[asset], self._lev[asset] = (
                p,
                (tp, sl),
                fee,
                p.leverage,
            )


class PriceSource(Protocol):
    """Public reads of the venue (instruments and mids): a HyperliquidExchange serves both,
    tests script them."""

    async def instruments(self) -> dict[str, Instrument]: ...
    async def mids(self) -> dict[str, Decimal]: ...
