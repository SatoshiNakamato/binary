"""Trend-gated binary-options decision engine.

This module produces UP/DOWN/WAIT decisions from completed OHLC candles.
It deliberately has no broker, credential, order, or money-transfer interface.

Strategy configuration:
    candle timeframe: 5 minutes
    decision horizon: 2 minutes
    assets: EUR/USD, GBP/USD, USD/JPY
    mode: trend-following only
    minimum payout: 80 percent

The 2-minute horizon is measured forward from the decision timestamp. A
completed 5-minute candle is used for pattern/trend features; callers must
also record the price at decision time and at decision+120 seconds when
building outcome history.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from math import isfinite
from typing import Iterable, Literal


Side = Literal["UP", "DOWN", "WAIT"]

CANDLE_MINUTES = 5
TRADE_MINUTES = 2
MIN_PAYOUT = 0.80
ASSETS = ("EUR/USD", "GBP/USD", "USD/JPY")
MIN_CANDLES = 30


@dataclass(frozen=True)
class Candle:
    timestamp: float
    open: float
    high: float
    low: float
    close: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open


@dataclass(frozen=True)
class Decision:
    asset: str
    side: Side
    confidence_score: float
    trend: str
    patterns: tuple[str, ...]
    payout: float
    reason: str
    candle_minutes: int = CANDLE_MINUTES
    trade_minutes: int = TRADE_MINUTES

    def to_dict(self) -> dict:
        return asdict(self)


def _mean(values: Iterable[float]) -> float:
    xs = list(values)
    return sum(xs) / len(xs) if xs else 0.0


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    value = values[0]
    for x in values[1:]:
        value = alpha * x + (1.0 - alpha) * value
    return value


def _trend(candles: list[Candle]) -> str:
    closes = [c.close for c in candles]
    if len(closes) < 20:
        return "UNKNOWN"
    fast = _ema(closes[-20:], 8)
    slow = _ema(closes[-20:], 20)
    recent = closes[-5:]
    rising = recent[-1] > recent[0] and all(recent[i] >= recent[i - 1] for i in range(2, len(recent)))
    falling = recent[-1] < recent[0] and all(recent[i] <= recent[i - 1] for i in range(2, len(recent)))
    if fast > slow and rising:
        return "UPTREND"
    if fast < slow and falling:
        return "DOWNTREND"
    return "RANGE"


def _patterns(c: Candle, prev: Candle | None) -> list[str]:
    if c.range <= 0:
        return []
    body = c.body
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    out: list[str] = []

    if body / c.range >= 0.80:
        out.append("BULLISH_MARUBOZU" if c.bullish else "BEARISH_MARUBOZU")
    if lower >= max(body * 2.0, c.range * 0.45) and upper <= c.range * 0.20:
        out.append("HAMMER" if c.bullish else "BEARISH_HAMMER")
    if upper >= max(body * 2.0, c.range * 0.45) and lower <= c.range * 0.20:
        out.append("SHOOTING_STAR" if c.bearish else "INVERTED_HAMMER")

    if prev is not None:
        if c.bullish and prev.bearish and c.open <= prev.close and c.close >= prev.open:
            out.append("BULLISH_ENGULFING")
        if c.bearish and prev.bullish and c.open >= prev.close and c.close <= prev.open:
            out.append("BEARISH_ENGULFING")
        if c.high < prev.high and c.low > prev.low:
            out.append("INSIDE_BAR")

    return out


def decide(asset: str, candles: list[Candle], payout: float) -> Decision:
    """Return a trend-only UP/DOWN/WAIT decision.

    Confidence is an evidence score, not a probability of winning.
    """
    asset = asset.upper().strip()
    if asset not in ASSETS:
        return Decision(asset, "WAIT", 0.0, "UNKNOWN", (), payout, "asset_not_allowed")
    if len(candles) < MIN_CANDLES:
        return Decision(asset, "WAIT", 0.0, "UNKNOWN", (), payout, "insufficient_candles")
    if not isfinite(payout) or payout < MIN_PAYOUT:
        return Decision(asset, "WAIT", 0.0, "UNKNOWN", (), payout, "payout_below_80_percent")
    if any(c.high < max(c.open, c.close) or c.low > min(c.open, c.close) or c.range <= 0 for c in candles[-MIN_CANDLES:]):
        return Decision(asset, "WAIT", 0.0, "UNKNOWN", (), payout, "invalid_candle_data")

    trend = _trend(candles)
    current = candles[-1]
    prev = candles[-2]
    patterns = _patterns(current, prev)

    if trend == "UPTREND":
        aligned = {"BULLISH_MARUBOZU", "HAMMER", "BULLISH_ENGULFING"}
        confirming = [p for p in patterns if p in aligned]
        if not confirming:
            return Decision(asset, "WAIT", 0.0, trend, tuple(patterns), payout, "uptrend_without_bullish_confirmation")
        score = min(1.0, 0.50 + 0.15 * len(confirming))
        return Decision(asset, "UP", score, trend, tuple(patterns), payout, "trend_and_bullish_confirmation")

    if trend == "DOWNTREND":
        aligned = {"BEARISH_MARUBOZU", "SHOOTING_STAR", "BEARISH_ENGULFING"}
        confirming = [p for p in patterns if p in aligned]
        if not confirming:
            return Decision(asset, "WAIT", 0.0, trend, tuple(patterns), payout, "downtrend_without_bearish_confirmation")
        score = min(1.0, 0.50 + 0.15 * len(confirming))
        return Decision(asset, "DOWN", score, trend, tuple(patterns), payout, "trend_and_bearish_confirmation")

    return Decision(asset, "WAIT", 0.0, trend, tuple(patterns), payout, "trend_not_directional")


if __name__ == "__main__":
    print({
        "candle_minutes": CANDLE_MINUTES,
        "trade_minutes": TRADE_MINUTES,
        "minimum_payout": MIN_PAYOUT,
        "assets": ASSETS,
        "mode": "trend_following_only",
    })
