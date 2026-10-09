"""SPEC §10 live self-test at minimal size on the real gateway: order, margin borrow,
close, kill switch. Records ``risk_state.live_selftest_at`` on success.

    python -m app.execution.live_selftest --confirm yes      # on the server (IP-bound key)

Sequence on BTCUSDT: (1) long: post-only buy of the minimum amount at the bid, wait,
sell it back; (2) short: sell the minimum amount with auto-borrow (isLeverage=1), buy it
back so the borrow repays; (3) kill switch: queue a kill request and confirm the executor
would act (control request applied flag). Every step is logged; any failure stops it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.config import get_config, get_secrets
from app.db.models import ControlRequest, RiskState
from app.db.session import new_session
from app.execution.bybit_client import BybitClient
from app.execution.bybit_gateway import BybitGateway
from app.execution.gateway import Outcome
from app.execution.simulator import Quote, Side


async def run() -> int:
    cfg, secrets = get_config(), get_secrets()
    symbol = cfg.symbol("BTC")
    async with BybitClient(
        secrets.bybit_base_url, secrets.bybit_api_key, secrets.bybit_api_secret, allow_orders=True
    ) as client:
        gw = BybitGateway(client)
        inst = await client.instrument(symbol)
        assert inst is not None
        t = await client.ticker(symbol)
        quote = Quote(t.bid1_price, t.ask1_price, datetime.now(UTC))
        qty = inst.min_qty_at(quote.ask, headroom=Decimal("1.2"))
        print(f"[1/3] long {qty} {symbol} at ~{quote.bid}")
        r = await gw.open(symbol, Side.LONG, qty, False, f"st-{uuid.uuid4().hex[:12]}", quote)
        print(f"      entry: {r.outcome.value} filled {r.filled_qty} avg {r.avg_price} fee {r.fee}")
        if r.outcome not in (Outcome.FILLED, Outcome.PARTIAL) or r.filled_qty <= 0:
            return 1
        t = await client.ticker(symbol)
        quote = Quote(t.bid1_price, t.ask1_price, datetime.now(UTC))
        r = await gw.close(symbol, Side.LONG, r.filled_qty, f"st-{uuid.uuid4().hex[:12]}", quote)
        print(f"      exit: {r.outcome.value} filled {r.filled_qty} avg {r.avg_price}")
        if r.outcome not in (Outcome.FILLED, Outcome.PARTIAL):
            return 1
        t = await client.ticker(symbol)
        quote = Quote(t.bid1_price, t.ask1_price, datetime.now(UTC))
        print(f"[2/3] short {qty} {symbol} with margin borrow at ~{quote.ask}")
        r = await gw.open(symbol, Side.SHORT, qty, True, f"st-{uuid.uuid4().hex[:12]}", quote)
        print(f"      entry: {r.outcome.value} filled {r.filled_qty} avg {r.avg_price} {r.detail}")
        if r.outcome not in (Outcome.FILLED, Outcome.PARTIAL) or r.filled_qty <= 0:
            return 1
        t = await client.ticker(symbol)
        quote = Quote(t.bid1_price, t.ask1_price, datetime.now(UTC))
        r = await gw.close(symbol, Side.SHORT, r.filled_qty, f"st-{uuid.uuid4().hex[:12]}", quote)
        print(f"      cover: {r.outcome.value} filled {r.filled_qty} avg {r.avg_price}")
        if r.outcome not in (Outcome.FILLED, Outcome.PARTIAL):
            return 1
        wallet = await client.wallet_balance()
        borrows = [c.coin for c in wallet.coin if c.borrow_amount and c.borrow_amount > 0]
        print(f"      open borrows after cover: {borrows or 'none'}")
        if borrows:
            return 1
    print("[3/3] kill switch: queueing a kill request for the executor")
    with new_session() as s:
        req = ControlRequest(
            ts=datetime.now(UTC), kind="kill", source="live_selftest", reason="self-test"
        )
        s.add(req)
        s.commit()
        rid = req.id
    for _ in range(12):
        await asyncio.sleep(5)
        with new_session() as s:
            row = s.get(ControlRequest, rid)
            if row and row.applied_at:
                print(f"      executor applied the kill: {row.result}")
                break
    else:
        print("      executor did not apply the kill within 60 s")
        return 1
    with new_session() as s:
        s.add(
            ControlRequest(
                ts=datetime.now(UTC), kind="resume", source="live_selftest", reason="self-test done"
            )
        )
        state = s.get(RiskState, 1)
        if state is not None:
            state.live_selftest_at = datetime.now(UTC)
        s.commit()
    print("live self-test passed; risk_state.live_selftest_at set")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    if args.confirm != "yes":
        print("refusing: needs --confirm yes (real orders at minimal size)")
        return 2
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
