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


class RiskParameters(BybitModel):
    price_limit_ratio_x: Decimal = Decimal("0.01")  # orders must sit within ±X of the reference
    price_limit_ratio_y: Decimal = Decimal("0.02")


class Instrument(BybitModel):
    symbol: str
    base_coin: str
    quote_coin: str
    status: str
    margin_trading: str
    lot_size_filter: LotSizeFilter
    price_filter: PriceFilter
    risk_parameters: RiskParameters = RiskParameters()

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

    def depth_truncated(self, pct: Decimal = Decimal("0.02")) -> bool:
        """True when the snapshot ends inside the ±pct band, so the depth is a lower bound."""
        if not self.bids or not self.asks:
            return True
        lo, hi = self.mid * (1 - pct), self.mid * (1 + pct)
        return self.bids[-1].price > lo or self.asks[-1].price < hi


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
        """None when the key never expires (Bybit reports epoch 0 for IP-bound keys)."""
        if self.expired_at is None or self.expired_at.year < 2000:
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


class TimeInForce(StrEnum):
    GTC = "GTC"
    POST_ONLY = "PostOnly"  # entries only: never crosses the spread
    IOC = "IOC"  # exits: take what is there up to the limit, cancel the rest


class OrderRequest(BaseModel):
    """A spot order. Category is fixed: the bot never trades derivatives.

    Three shapes are used: a post-only limit (entries), an IOC limit with a price cap
    (exits) and a conditional stop-market (``trigger_price`` set: the exchange-side
    backup stop).
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    side: Side
    qty: Decimal
    price: Decimal | None = None  # None only for the stop-market
    order_link_id: str = Field(min_length=1, max_length=36, pattern=r"^[A-Za-z0-9_-]+$")
    post_only: bool = False
    time_in_force: TimeInForce | None = None  # None: PostOnly when post_only, else GTC
    trigger_price: Decimal | None = None
    is_leverage: bool = False

    @property
    def tif(self) -> TimeInForce:
        if self.time_in_force is not None:
            return self.time_in_force
        return TimeInForce.POST_ONLY if self.post_only else TimeInForce.GTC

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "category": "spot",
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": format(self.qty, "f"),
            "orderLinkId": self.order_link_id,
            "isLeverage": 1 if self.is_leverage else 0,
        }
        if self.trigger_price is not None:
            # Conditional order: assets are not reserved until the trigger fires, so the
            # bot's own exit can still use them. Market buys are sized in the base coin.
            body |= {
                "orderType": "Market",
                "orderFilter": "StopOrder",
                "triggerPrice": format(self.trigger_price, "f"),
                "marketUnit": "baseCoin",
            }
            return body
        if self.price is None:
            raise ValueError("a limit order needs a price")
        body |= {
            "orderType": "Limit",
            "price": format(self.price, "f"),
            "timeInForce": self.tif.value,
        }
        return body


FINAL_ORDER_STATES = frozenset(
    {"Filled", "Cancelled", "PartiallyFilledCanceled", "Rejected", "Deactivated"}
)


class OrderAck(BybitModel):
    order_id: str
    order_link_id: str


class Order(BybitModel):
    order_id: str
    order_link_id: str
    symbol: str
    side: Side
    price: OptDecimal = None  # blank on market orders
    qty: Decimal
    order_status: str
    cum_exec_qty: Decimal = Decimal(0)
    avg_price: OptDecimal = None
    cum_exec_fee: OptDecimal = None
    trigger_price: OptDecimal = None

    @property
    def is_final(self) -> bool:
        """No further fills can arrive on this order."""
        return self.order_status in FINAL_ORDER_STATES


class Announcement(BaseModel):
    """One row of ``/v5/announcements/index`` (public). Title and description are untrusted
    text: for classification and the admin only, never for the PMs."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    title: str
    description: str = ""
    url: str = ""
    type: dict[str, Any] = Field(default_factory=dict)  # {"key": "new_crypto", "title": ...}
    tags: list[str] = Field(default_factory=list)
    date_ms: int = Field(alias="dateTimestamp")
    start_ms: int | None = Field(default=None, alias="startDateTimestamp")

    @property
    def kind(self) -> str:
        return str(self.type.get("key") or "")

    @property
    def published_at(self) -> datetime:
        return datetime.fromtimestamp(self.date_ms / 1000, tz=UTC)
