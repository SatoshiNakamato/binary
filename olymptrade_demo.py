"""OlympTrade market/trade gateway restricted to the DEMO account.

This module uses the inspected ChipaDevTeam WebSocket client. It builds 5-minute
OHLC candles locally from real tick events instead of relying on the upstream
client's speculative candle-history implementation. Orders are hard-blocked to
OlympTrade's demo group.

Set OLYMPTRADE_API_PATH to the checked-out OlympTradeAPI directory.
Set OLYMPTRADE_ACCESS_TOKEN in the process environment; never commit it.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

PAIRS = {"EUR/USD": "EURUSD", "GBP/USD": "GBPUSD", "USD/JPY": "USDJPY"}
PAIR_NAMES = {v: k for k, v in PAIRS.items()}
CANDLE_SECONDS = 5 * 60
TRADE_SECONDS = 2 * 60
MIN_PAYOUT = 0.80


def load_client_class():
    path = os.environ.get("OLYMPTRADE_API_PATH")
    if not path:
        raise RuntimeError("OLYMPTRADE_API_PATH is required")
    path = os.path.abspath(os.path.expanduser(path))
    if path not in sys.path:
        sys.path.insert(0, path)
    module = importlib.import_module("olymptrade_ws.core.client")
    return module.OlympTradeClient


@dataclass(frozen=True)
class Tick:
    pair: str
    price: float
    timestamp: float


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float

    def as_dict(self) -> dict:
        return {"timestamp": self.timestamp, "open": self.open, "high": self.high, "low": self.low, "close": self.close}


class CandleBuilder:
    def __init__(self, seconds: int = CANDLE_SECONDS):
        self.seconds = seconds
        self._current: Dict[str, Candle] = {}
        self._closed: Dict[str, List[Candle]] = defaultdict(list)

    def push(self, tick: Tick) -> Optional[Candle]:
        bucket = int(tick.timestamp // self.seconds) * self.seconds
        cur = self._current.get(tick.pair)
        if cur is None:
            self._current[tick.pair] = Candle(bucket, tick.price, tick.price, tick.price, tick.price)
            return None
        if bucket == cur.timestamp:
            self._current[tick.pair] = Candle(cur.timestamp, cur.open, max(cur.high, tick.price), min(cur.low, tick.price), tick.price)
            return None
        if bucket < cur.timestamp:
            return None
        self._closed[tick.pair].append(cur)
        self._closed[tick.pair] = self._closed[tick.pair][-100:]
        self._current[tick.pair] = Candle(bucket, tick.price, tick.price, tick.price, tick.price)
        return cur

    def history(self, pair: str, limit: int = 30) -> List[Candle]:
        return list(self._closed.get(pair, []))[-limit:]


class OlympTradeDemoGateway:
    """Real WebSocket market data + DEMO-only order gateway."""

    def __init__(self, token: Optional[str] = None):
        token = token or os.environ.get("OLYMPTRADE_ACCESS_TOKEN")
        if not token:
            raise RuntimeError("OLYMPTRADE_ACCESS_TOKEN is required")
        self._token = token
        Client = load_client_class()
        self.client = Client(access_token=token, log_raw_messages=False)
        self.candles = CandleBuilder()
        self._tick_callbacks: List[Callable[[Tick], Awaitable[None]]] = []
        self._candle_callbacks: List[Callable[[Candle], Awaitable[None]]] = []
        self._trade_updates: asyncio.Queue[dict] = asyncio.Queue()
        self._started = False
        self.demo_account_id: Optional[int] = None
        self._payouts: Dict[str, float] = {}

    async def _on_tick(self, message: dict):
        data = message.get("d", [])
        if not isinstance(data, list):
            return
        for item in data:
            pair_code = item.get("p")
            price = item.get("q")
            ts = item.get("t")
            if pair_code is None or price is None or ts is None:
                continue
            try:
                tick = Tick(PAIR_NAMES.get(str(pair_code), str(pair_code)), float(price), float(ts) / (1000 if float(ts) > 10_000_000_000 else 1))
            except (TypeError, ValueError):
                continue
            if tick.pair not in PAIRS:
                continue
            for callback in self._tick_callbacks:
                await callback(tick)
            closed = self.candles.push(tick)
            if closed is not None:
                for callback in self._candle_callbacks:
                    await callback(closed)

    async def _on_trade_update(self, message: dict):
        event = message.get("e")
        for item in message.get("d", []) if isinstance(message.get("d"), list) else []:
            payload = dict(item)
            payload["event"] = event
            await self._trade_updates.put(payload)

    async def _on_payout_update(self, message: dict):
        data = message.get("d")
        if isinstance(data, list):
            for item in data:
                pair = item.get("pair") or item.get("p") or item.get("asset")
                payout = item.get("profitability")
                if payout is None:
                    payout = item.get("profit")
                if pair is not None and payout is not None:
                    try:
                        value = float(payout)
                        if value > 1:
                            value /= 100.0
                        self._payouts[PAIR_NAMES.get(str(pair), str(pair))] = value
                    except (TypeError, ValueError):
                        pass

    async def start(self):
        if self._started:
            return
        # Register before start so no early events are lost.
        self.client.register_callback(1, self._on_tick)
        self.client.register_callback(21, self._on_trade_update)
        self.client.register_callback(22, self._on_trade_update)
        self.client.register_callback(26, self._on_trade_update)
        self.client.register_callback(183, self._on_payout_update)
        await self.client.start()
        await self.client.initialize_session()
        self.demo_account_id = await self._find_demo_account()
        if not self.demo_account_id:
            raise RuntimeError("OlympTrade demo account was not returned by the session")
        for pair in PAIRS.values():
            await self.client.market.subscribe_ticks(pair)
        self._started = True

    async def _find_demo_account(self) -> Optional[int]:
        balance = self.client.current_balance
        data = balance.get("d", []) if isinstance(balance, dict) else []
        if isinstance(data, list):
            for account in data:
                if account.get("group") == "demo" and account.get("account_id") is not None:
                    return int(account["account_id"])
        for _ in range(20):
            await asyncio.sleep(0.25)
            balance = self.client.current_balance
            data = balance.get("d", []) if isinstance(balance, dict) else []
            if isinstance(data, list):
                for account in data:
                    if account.get("group") == "demo" and account.get("account_id") is not None:
                        return int(account["account_id"])
        return None

    def payout(self, pair: str) -> Optional[float]:
        return self._payouts.get(pair)

    async def place_demo_order(self, pair: str, amount: float, direction: str, duration: int = TRADE_SECONDS) -> dict:
        # Hard safety boundary: this gateway can never submit to the real account.
        if self.demo_account_id is None:
            raise RuntimeError("Demo account is not initialized")
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")
        if pair not in PAIRS:
            raise ValueError(f"unsupported pair: {pair}")
        if amount <= 0:
            raise ValueError("amount must be positive")
        result = await self.client.trade.place_order(
            pair=PAIRS[pair], amount=amount, direction=direction, duration=duration,
            account_id=self.demo_account_id, group="demo", category="digital"
        )
        if not result or not result.get("id"):
            raise RuntimeError(f"OlympTrade demo order was not acknowledged: {result!r}")
        return result

    async def next_trade_update(self, timeout: float = 180.0) -> dict:
        return await asyncio.wait_for(self._trade_updates.get(), timeout=timeout)

    async def stop(self):
        if not self._started:
            return
        for pair in PAIRS.values():
            try:
                await self.client.market.unsubscribe_ticks(pair)
            except Exception:
                pass
        await self.client.stop()
        self._started = False
