"""Autonomous OlympTrade DEMO runtime for the FLY binary strategy.

The runtime consumes real OlympTrade ticks, closes its own 5-minute candles,
waits for a complete 30-candle context, applies the repository strategy, and
places only DEMO orders when the strategy returns UP/DOWN and payout is >= 80%.
No real-account order path exists in this module.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from binary_strategy import Candle as StrategyCandle, decide
from olymptrade_demo import MIN_PAYOUT, PAIRS, TRADE_SECONDS, OlympTradeDemoGateway

STAKE = float(os.environ.get("FLY_BINARY_DEMO_STAKE", "10"))
STATE_DIR = Path(os.environ.get("FLY_STATE_DIR", "build"))
LEDGER = STATE_DIR / "binary_demo_ledger.jsonl"


def record(event: str, **fields) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "event": event, **fields}
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")


def convert(candles):
    return [StrategyCandle(c.timestamp, c.open, c.high, c.low, c.close) for c in candles]


async def main() -> None:
    gateway = OlympTradeDemoGateway()
    ready = {pair: asyncio.Event() for pair in PAIRS}

    async def on_candle(candle):
        pair = candle.pair
        history = gateway.candles.history(pair, 30)
        if len(history) < 30:
            record("warmup", pair=pair, candles=len(history))
            return
        payout = gateway.payout(pair)
        if payout is None or payout < MIN_PAYOUT:
            record("wait", pair=pair, reason="payout_below_threshold", payout=payout)
            return
        decision = decide(pair, convert(history), payout)
        record("decision", pair=pair, side=decision.side, trend=decision.trend,
               confidence=decision.confidence_score, patterns=decision.patterns,
               payout=payout, reason=decision.reason)
        if decision.side not in {"UP", "DOWN"}:
            return
        result = await gateway.place_demo_order(
            pair=pair, amount=STAKE, direction=decision.side.lower(), duration=TRADE_SECONDS
        )
        record("order_accepted", pair=pair, side=decision.side, stake=STAKE,
               duration=TRADE_SECONDS, trade_id=result.get("id"), account="demo")

    for pair in PAIRS:
        # The gateway emits candles through its public callback list.
        gateway.on_candle(on_candle)

    await gateway.start()
    record("started", account="demo", pairs=list(PAIRS), candle_minutes=5,
           trade_minutes=2, min_payout=MIN_PAYOUT, stake=STAKE)
    try:
        while True:
            await asyncio.sleep(60)
            await gateway.refresh_payouts()
    finally:
        await gateway.stop()
        record("stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
