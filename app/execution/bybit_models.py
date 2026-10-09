"""Pydantic models for the Bybit V5 payloads the bot uses."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from pydantic.alias_generators import to_camel

HOURS_PER_YEAR = Decimal(24 * 365)


def _blank_to_none(value: Any) -> Any:
    return None if value == "" else value


# Bybit sends "" for numbers that do not apply to the account mode.
OptDecimal = Annotated[Decimal | None, BeforeValidator(_blank_to_none)]


class BybitModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True
    )


class Side(StrEnum):
    BUY = "Buy"
    SELL = "Sell"


class LotSizeFilter(BybitModel):
    base_precision: Decimal
    quote_precision: Decimal
    min_order_qty: Decimal
    max_order_qty: Decimal
    min_order_amt: Decimal
    max_order_amt: Decimal


class PriceFilter(BybitModel):
    tick_size: Decimal


def _quantize(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


class Instrument(BybitModel):
    symbol: str
    base_coin: str
    quote_coin: str
    status: str
    margin_trading: str
    lot_size_filter: LotSizeFilter
    price_filter: PriceFilter

    @property
    def is_trading(self) -> bool:
        return self.status == "Trading"

    @property
    def margin_enabled(self) -> bool:
        """Whether the pair can be traded with borrowed funds (leverage and shorts)."""
        return self.margin_trading != "none"

    def round_price(self, price: Decimal, *, up: bool = False) -> Decimal:
        return _quantize(price, self.price_filter.tick_size, ROUND_CEILING if up else ROUND_FLOOR)

    def round_qty(self, qty: Decimal, *, up: bool = False) -> Decimal:
        return _quantize(
            qty, self.lot_size_filter.base_precision, ROUND_CEILING if up else ROUND_FLOOR
        )

    def min_qty_at(self, price: Decimal, *, headroom: Decimal = Decimal("1.1")) -> Decimal:
        """Smallest order quantity the exchange accepts at ``price``, with some headroom."""
        by_amount = self.round_qty(self.lot_size_filter.min_order_amt * headroom / price, up=True)
        return max(by_amount, self.lot_size_filter.min_order_qty)


class Kline(BaseModel):
    model_config = ConfigDict(frozen=True)

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal

    @classmethod
    def from_row(cls, row: list[str]) -> Kline:
        """Bybit sends [startTime(ms), open, high, low, close, volume, turnover]."""
        return cls(
            open_time=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
            open=Decimal(row[1]),
            high=Decimal(row[2]),
            low=Decimal(row[3]),
            close=Decimal(row[4]),
            volume=Decimal(row[5]),
            turnover=Decimal(row[6]),
        )


class Ticker(BybitModel):
    symbol: str
    bid1_price: Decimal
    bid1_size: Decimal
    ask1_price: Decimal
    ask1_size: Decimal
    last_price: Decimal
    turnover24h: Decimal
    volume24h: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid1_price + self.ask1_price) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask1_price - self.bid1_price) / self.mid * Decimal(10_000)


class BookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal
    qty: Decimal


def _levels(value: Any) -> Any:
    if isinstance(value, list):
        return [
            {"price": lv[0], "qty": lv[1]} if isinstance(lv, list | tuple) else lv for lv in value
        ]
    return value


class OrderBook(BybitModel):
    symbol: str = Field(alias="s")
    bids: Annotated[list[BookLevel], BeforeValidator(_levels)] = Field(alias="b")
    asks: Annotated[list[BookLevel], BeforeValidator(_levels)] = Field(alias="a")
    ts: int

    @property
    def best_bid(self) -> BookLevel:
        return self.bids[0]

    @property
    def best_ask(self) -> BookLevel:
        return self.asks[0]

    @property
    def mid(self) -> Decimal:
        return (self.best_bid.price + self.best_ask.price) / 2

    def depth_quote(self, pct: Decimal = Decimal("0.02")) -> tuple[Decimal, Decimal]:
        """Quote-coin value resting within ±pct of mid, as (bids, asks)."""
        lo, hi = self.mid * (1 - pct), self.mid * (1 + pct)
        bids = sum((lv.price * lv.qty for lv in self.bids if lv.price >= lo), Decimal(0))
        asks = sum((lv.price * lv.qty for lv in self.asks if lv.price <= hi), Decimal(0))
        return bids, asks


class FeeRate(BybitModel):
    symbol: str
    taker_fee_rate: Decimal
    maker_fee_rate: Decimal


class ApiKeyInfo(BybitModel):
    """Key metadata. The key and secret fields of the payload are deliberately not modeled."""

    read_only: int
    permissions: dict[str, list[str]]
    ips: list[str]
    expired_at: datetime | None = None
    vip_level: str = ""
    is_master: bool = True
    uta: int = 0
    kyc_region: str = ""

    @property
    def ip_bound(self) -> bool:
        return bool(self.ips) and "*" not in self.ips

    @property
    def granted(self) -> dict[str, list[str]]:
        return {scope: perms for scope, perms in self.permissions.items() if perms}

    def days_to_expiry(self, now: datetime | None = None) -> int | None:
        if self.expired_at is None:
            return None
        return (self.expired_at - (now or datetime.now(UTC))).days


class AccountInfo(BybitModel):
    margin_mode: str
    unified_margin_status: int
    spot_hedging_status: str = ""


class SpotMarginState(BybitModel):
    spot_leverage: str = ""
    spot_margin_mode: str = "0"

    @property
    def enabled(self) -> bool:
        return self.spot_margin_mode == "1"


class CoinBalance(BybitModel):
    coin: str
    wallet_balance: Decimal
    equity: OptDecimal = None
    usd_value: OptDecimal = None
    locked: OptDecimal = None
    borrow_amount: OptDecimal = None
    spot_borrow: OptDecimal = None


class WalletBalance(BybitModel):
    account_type: str
    total_equity: OptDecimal = None
    total_wallet_balance: OptDecimal = None
    total_available_balance: OptDecimal = None
    coin: list[CoinBalance] = Field(default_factory=list)

    def of(self, coin: str) -> CoinBalance | None:
        return next((c for c in self.coin if c.coin == coin), None)


class MarginCoin(BybitModel):
    """Public spot-margin data for one coin at one VIP level."""

    currency: str
    borrowable: bool
    collateral_ratio: Decimal
    hourly_borrow_rate: Decimal
    margin_collateral: bool
    max_borrowing_amount: Decimal

    @property
    def borrow_apr(self) -> Decimal:
        return self.hourly_borrow_rate * HOURS_PER_YEAR


class OrderRequest(BaseModel):
    """A spot limit order. Category is fixed: the bot never trades derivatives."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    order_link_id: str = Field(min_length=1, max_length=36, pattern=r"^[A-Za-z0-9_-]+$")
    post_only: bool = False
    is_leverage: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "category": "spot",
            "symbol": self.symbol,
            "side": self.side.value,
            "orderType": "Limit",
            "qty": format(self.qty, "f"),
            "price": format(self.price, "f"),
            "timeInForce": "PostOnly" if self.post_only else "GTC",
            "orderLinkId": self.order_link_id,
            "isLeverage": 1 if self.is_leverage else 0,
        }


class OrderAck(BybitModel):
    order_id: str
    order_link_id: str


class Order(BybitModel):
    order_id: str
    order_link_id: str
    symbol: str
    side: Side
    price: Decimal
    qty: Decimal
    order_status: str
    cum_exec_qty: Decimal = Decimal(0)
