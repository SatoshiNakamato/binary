"""Real OlympTrade ticks and DEMO-only binary order gateway.

The upstream client is used for WebSocket transport. Five-minute OHLC candles
are constructed locally from event-1 ticks; no speculative history endpoint is
used. Order submission is hard-coded to the demo group.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

PAIRS = {"EUR/USD": "EURUSD", "GBP/USD": "GBPUSD", "USD/JPY": "USDJPY"}
PAIR_NAMES = {v: k for k, v in PAIRS.items()}
CANDLE_SECONDS = 300
TRADE_SECONDS = 120
MIN_PAYOUT = 0.80
DEFAULT_WS_URI = (
    "wss://ws.olymptrade.com/otp?"
    "cid_ver=1&cid_app=web%40OlympTrade%402025.2.26123%4026123"
    "&cid_device=%40%40desktop&cid_os=windows%4010"
)


def load_client_class():
    path = os.path.abspath(os.path.expanduser(os.environ["OLYMPTRADE_API_PATH"]))
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module("olymptrade_ws.core.client").OlympTradeClient


def load_access_token(token: Optional[str] = None) -> str:
    """Load the raw access_token without imposing a JWT format."""
    value = token if token is not None else os.environ.get("OLYMPTRADE_ACCESS_TOKEN", "")
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if not value:
        raise RuntimeError("OLYMPTRADE_ACCESS_TOKEN is required")
    return value


@dataclass(frozen=True)
class Tick:
    pair: str
    price: float
    timestamp: float


@dataclass(frozen=True)
class Candle:
    pair: str
    timestamp: int
    open: float
    high: float
    low: float
    close: float


class CandleBuilder:
    def __init__(self, seconds=CANDLE_SECONDS):
        self.seconds = seconds
        self.current: Dict[str, Candle] = {}
        self.closed: Dict[str, List[Candle]] = defaultdict(list)

    def push(self, tick: Tick) -> Optional[Candle]:
        bucket = int(tick.timestamp // self.seconds) * self.seconds
        cur = self.current.get(tick.pair)
        if cur is None:
            self.current[tick.pair] = Candle(tick.pair, bucket, tick.price, tick.price, tick.price, tick.price)
            return None
        if bucket == cur.timestamp:
            self.current[tick.pair] = Candle(
                tick.pair, cur.timestamp, cur.open,
                max(cur.high, tick.price), min(cur.low, tick.price), tick.price
            )
            return None
        if bucket < cur.timestamp:
            return None
        self.closed[tick.pair].append(cur)
        self.closed[tick.pair] = self.closed[tick.pair][-100:]
        self.current[tick.pair] = Candle(tick.pair, bucket, tick.price, tick.price, tick.price, tick.price)
        return cur

    def history(self, pair: str, limit=30):
        return list(self.closed.get(pair, []))[-limit:]


class OlympTradeDemoGateway:
    def __init__(self, token=None):
        normalized = load_access_token(token)
        client_class = load_client_class()
        ws_uri = os.environ.get("OLYMPTRADE_WS_URI", DEFAULT_WS_URI).strip()
        if not ws_uri:
            ws_uri = DEFAULT_WS_URI
        self.client = client_class(
            access_token=normalized,
            uri=ws_uri,
            log_raw_messages=False,
        )
        self.candles = CandleBuilder()
        self._candle_callbacks: List[Callable[[Candle], Awaitable[None]]] = []
        self._trade_updates: asyncio.Queue[dict] = asyncio.Queue()
        self._payouts: Dict[str, float] = {}
        self.demo_account_id: Optional[int] = None
        self._started = False

    def on_candle(self, callback):
        self._candle_callbacks.append(callback)

    async def _tick(self, message):
        data = message.get("d", [])
        if not isinstance(data, list):
            return
        for item in data:
            try:
                code = item.get("p")
                price = float(item.get("q"))
                raw_ts = float(item.get("t"))
                ts = raw_ts / 1000.0 if raw_ts > 10_000_000_000 else raw_ts
            except (TypeError, ValueError):
                continue
            pair = PAIR_NAMES.get(str(code))
            if pair is None:
                continue
            closed = self.candles.push(Tick(pair, price, ts))
            if closed is not None:
                for cb in self._candle_callbacks:
                    await cb(closed)

    async def _trade(self, message):
        data = message.get("d", [])
        if not isinstance(data, list):
            return
        for item in data:
            if isinstance(item, dict):
                row = dict(item)
                row["event"] = message.get("e")
                await self._trade_updates.put(row)

    def _payout_update(self, message):
        data = message.get("d", [])
        if not isinstance(data, list):
            return
        for item in data:
            if not isinstance(item, dict):
                continue
            code = item.get("pair") or item.get("p") or item.get("asset")
            raw = item.get("profitability", item.get("profit"))
            try:
                value = float(raw)
                if value > 1:
                    value /= 100
            except (TypeError, ValueError):
                continue
            pair = PAIR_NAMES.get(str(code), str(code))
            if pair in PAIRS:
                self._payouts[pair] = value

    async def start(self):
        if self._started:
            return
        self.client.register_callback(1, self._tick)
        for event in (21, 22, 26):
            self.client.register_callback(event, self._trade)
        self.client.register_callback(183, self._payout_update)
        try:
            await self.client.start()
            await self.client.initialize_session()
        except Exception as exc:
            try:
                await self.client.stop()
            except Exception:
                pass
            message = str(exc)
            if "invalid_token" in message.lower() or "1008" in message:
                raise RuntimeError(
                    "OlympTrade rejected the access token during WebSocket session "
                    "initialization (1008 invalid_token). The client now passes the "
                    "token unchanged except for an optional Bearer prefix or wrapping "
                    "quotes. This response means the server rejected that credential; "
                    "the token itself must be refreshed from the currently logged-in "
                    "OlympTrade WebSocket session."
                ) from exc
            raise

        for _ in range(20):
            data = self.client.current_balance.get("d", []) if isinstance(self.client.current_balance, dict) else []
            for account in data if isinstance(data, list) else []:
                if isinstance(account, dict) and account.get("group") == "demo" and account.get("account_id") is not None:
                    self.demo_account_id = int(account["account_id"])
                    break
            if self.demo_account_id:
                break
            await asyncio.sleep(0.25)
        if not self.demo_account_id:
            await self.client.stop()
            raise RuntimeError("Demo account ID not found")

        for code in PAIRS.values():
            await self.client.market.subscribe_ticks(code)
        await self.refresh_payouts()
        self._started = True

    async def refresh_payouts(self):
        response = await self.client.market.get_profitability(self.demo_account_id)
        data = response.get("d") if isinstance(response, dict) else response
        self._payout_update({"d": data})
        return dict(self._payouts)

    def payout(self, pair):
        return self._payouts.get(pair)

    async def place_demo_order(self, pair, amount, direction):
        if self.demo_account_id is None:
            raise RuntimeError("Demo account is not initialized")
        if pair not in PAIRS:
            raise ValueError("unsupported pair")
        if direction not in ("up", "down"):
            raise ValueError("direction must be up or down")
        if amount <= 0:
            raise ValueError("amount must be positive")
        result = await self.client.trade.place_trade(
            pair=PAIRS[pair],
            amount=amount,
            direction=direction,
            duration=TRADE_SECONDS,
            account_id=self.demo_account_id,
            group="demo",
            category="digital",
        )
        if not result or not result.get("id"):
            raise RuntimeError(f"Demo order not acknowledged: {result!r}")
        return result

    async def next_trade_update(self, timeout=180):
        return await asyncio.wait_for(self._trade_updates.get(), timeout)

    async def stop(self):
        if not self._started:
            return
        for code in PAIRS.values():
            try:
                await self.client.market.unsubscribe_ticks(code)
            except Exception:
                pass
        await self.client.stop()
        self._started = False
