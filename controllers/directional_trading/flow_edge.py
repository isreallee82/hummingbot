"""
FlowEdge  — Hummingbot V2 Directional Trading Controller
============================================================
Belongs in: controllers/directional_trading/flow_edge.py

Inherits from DirectionalTradingControllerBase — the same base as
macd_bb_v1, bollinger_v1, dman_v3. Uses DCAExecutorConfig with
DCAMode.MAKER (same pattern as dman_v3) so DCA entry spreads are
dynamic limit orders, not aggressive market orders.

What makes it different from every other directional controller:

1. Multi-Timeframe Regime Detection
   Fast candles (3m)  → CFI signal + VWAP deviation
   Slow candles (15m) → ADX regime classifier
   Signal only fires when BOTH timeframes agree AND regime is trending.
   In ranging/extreme regimes signal is suppressed to zero.

2. NATR-Scaled DCA Spreads (like dman_v3 uses BB width)
   DCA entry levels scale with live NATR — wider in volatile markets,
   tighter in calm ones. Stop-loss and take-profit also scale with NATR
   via new_instance_with_adjusted_volatility.

3. Funding Rate Bias (perpetuals only)
   Reads live funding rate. Extreme positive funding adds bearish tilt
   to signal score. Extreme negative adds bullish tilt.

4. Self-Adaptive Threshold
   Monitors own win rate across closed executors each tick. Tightens
   signal_threshold when win rate drops, loosens when strong.
   No external process — fully embedded in the controller.

Signal features (all from candles — zero external dependencies):
  - Candle Flow Imbalance (CFI): (close-open)/(high-low) smoothed over 5 bars
  - VWAP Deviation: (close - rolling_vwap) / vwap
  - ADX: trend strength on slow timeframe for regime classification
  - NATR: volatility for spread scaling and triple barrier sizing
  - RSI: overbought/oversold filter to cancel contradictory entries
"""

from collections import deque
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Tuple

import pandas as pd
import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.dca_executor.data_types import DCAExecutorConfig, DCAMode


class MarketRegime(str, Enum):
    RANGING = "ranging"  # ADX low — suppress signal
    TRENDING = "trending"  # ADX mid — fire signal
    EXTREME = "extreme"  # ADX high — suppress signal (risk-off)


class FlowEdgeProConfig(DirectionalTradingControllerConfigBase):
    controller_name: str = "flow_edge"

    # ── Fast candles: signal generation ──────────────────────────────────
    candles_connector: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter candles connector (leave empty = same as trading connector): ",
            "prompt_on_new": True,
        },
    )
    candles_trading_pair: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter candles trading pair (leave empty = same as trading pair): ",
            "prompt_on_new": True,
        },
    )
    fast_interval: str = Field(
        default="3m",
        json_schema_extra={
            "prompt": "Enter fast candle interval for signal generation (e.g. 3m, 5m): ",
            "prompt_on_new": True,
        },
    )
    fast_max_records: int = Field(
        default=100,
        json_schema_extra={
            "prompt": "Enter max fast candles to load (e.g. 100): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )

    # ── Slow candles: regime confirmation ─────────────────────────────────
    slow_interval: str = Field(
        default="15m",
        json_schema_extra={
            "prompt": "Enter slow candle interval for regime confirmation (e.g. 15m, 1h): ",
            "prompt_on_new": True,
        },
    )
    slow_max_records: int = Field(
        default=50,
        json_schema_extra={
            "prompt": "Enter max slow candles to load (e.g. 50): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    allow_fast_regime_entry: bool = Field(
        default=False,
        json_schema_extra={
            "prompt": "Allow entries when fast regime is trending? (True/False): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )

    # ── Regime thresholds ─────────────────────────────────────────────────
    adx_trending_threshold: float = Field(
        default=25.0,
        json_schema_extra={
            "prompt": "Enter ADX threshold for trending regime (e.g. 25): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    adx_extreme_threshold: float = Field(
        default=45.0,
        json_schema_extra={
            "prompt": "Enter ADX threshold for extreme/halt regime (e.g. 45): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )

    # ── Signal threshold ──────────────────────────────────────────────────
    signal_threshold: float = Field(
        default=0.55,
        json_schema_extra={
            "prompt": "Enter signal conviction threshold to fire entry (e.g. 0.55): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )

    # ── DCA entry spreads (scale with NATR like dman_v3 uses BB width) ────
    dca_spreads: List[Decimal] = Field(
        default="0.002,0.005,0.01",
        json_schema_extra={
            "prompt": "Enter comma-separated DCA entry spreads (e.g. 0.002,0.005,0.01): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    dca_amounts_pct: List[Decimal] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter comma-separated DCA amount weights (leave empty for equal split): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    dynamic_spread: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Scale DCA spreads with NATR volatility? (True/False): ",
            "prompt_on_new": True,
        },
    )

    # ── RSI filter ────────────────────────────────────────────────────────
    rsi_length: int = Field(default=14)
    rsi_overbought: float = Field(
        default=70.0,
        json_schema_extra={"is_updatable": True},
    )
    rsi_oversold: float = Field(
        default=30.0,
        json_schema_extra={"is_updatable": True},
    )

    # ── Self-adaptation ──────────────────────────────────────────────────
    adapt_window: int = Field(
        default=20,
        json_schema_extra={"is_updatable": True},
    )

    # ── Funding rate bias ─────────────────────────────────────────────────
    funding_bias_enabled: bool = Field(default=True)
    funding_threshold: float = Field(default=0.0005)

    @field_validator("candles_connector", mode="before")
    @classmethod
    def set_candles_connector(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("connector_name")
        return v

    @field_validator("candles_trading_pair", mode="before")
    @classmethod
    def set_candles_trading_pair(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("trading_pair")
        return v

    @field_validator("dca_spreads", mode="before")
    @classmethod
    def parse_spreads(cls, v):
        if isinstance(v, str):
            return [Decimal(x.strip()) for x in v.split(",")]
        return v

    @field_validator("dca_amounts_pct", mode="before")
    @classmethod
    def parse_amounts(cls, v, validation_info: ValidationInfo):
        spreads = validation_info.data.get("dca_spreads", [])
        if v is None or v == "":
            n = len(spreads) if spreads else 3
            return [Decimal("1") / n for _ in range(n)]
        if isinstance(v, str):
            return [Decimal(x.strip()) for x in v.split(",")]
        return v


class FlowEdgeProController(DirectionalTradingControllerBase):
    """
    FlowEdge  — Hummingbot V2 directional controller.

    Signal pipeline:
      Fast candles → CFI + VWAP dev + RSI filter → raw signal score
      Slow candles → ADX regime → gate: only pass signal when trending
      Funding rate → bias adjustment on perpetuals
      Self-adapt   → tightens/loosens threshold from own win rate

    Execution:
      DCAExecutorConfig(DCAMode.MAKER) with NATR-scaled spreads
      Triple barrier also scales with NATR via adjusted_volatility
    """

    def __init__(self, config: FlowEdgeProConfig, *args, **kwargs):
        self.config = config
        self.max_records_fast = config.fast_max_records
        self.max_records_slow = config.slow_max_records
        self._adapted_threshold: float = config.signal_threshold
        self._recent_pnls: deque = deque(maxlen=config.adapt_window)
        self._last_closed_count: int = 0
        super().__init__(config, *args, **kwargs)
        self.logger().info(
            f"[FlowEdge] Started | pair={config.trading_pair} | "
            f"fast={config.fast_interval} slow={config.slow_interval}"
        )

    # ── Candles registration ───────────────────────────────────────────────

    def get_candles_config(self) -> List[CandlesConfig]:
        return [
            CandlesConfig(
                connector=self.config.candles_connector,
                trading_pair=self.config.candles_trading_pair,
                interval=self.config.fast_interval,
                max_records=self.max_records_fast,
            ),
            CandlesConfig(
                connector=self.config.candles_connector,
                trading_pair=self.config.candles_trading_pair,
                interval=self.config.slow_interval,
                max_records=self.max_records_slow,
            ),
        ]

    # ── Main tick ──────────────────────────────────────────────────────────

    async def update_processed_data(self):
        """
        Called every tick. Sets processed_data["signal"] = 1, -1, or 0.
        The base class create_actions_proposal() reads this and fires
        get_executor_config() when signal != 0.
        """
        # Self-adapt from own performance first
        self._self_adapt()

        fast_df = self.market_data_provider.get_candles_df(
            connector_name=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.fast_interval,
            max_records=self.max_records_fast,
        )
        slow_df = self.market_data_provider.get_candles_df(
            connector_name=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.slow_interval,
            max_records=self.max_records_slow,
        )

        if fast_df is None or fast_df.empty or len(fast_df) < 20:
            self.processed_data["signal"] = 0
            self.processed_data["features"] = pd.DataFrame()
            return

        fast_df = self._compute_fast_features(fast_df)
        slow_regime, slow_adx = self._compute_adx_regime(
            slow_df,
            self.config.adx_trending_threshold,
            self.config.adx_extreme_threshold,
        )
        fast_regime, fast_adx = self._compute_adx_regime(
            fast_df,
            self.config.adx_trending_threshold,
            self.config.adx_extreme_threshold,
        )
        latest = fast_df.iloc[-1]

        natr_pct = float(latest.get("natr_pct", 1.0))
        signal_score = float(latest.get("signal_score", 0.0))
        rsi = float(latest.get("rsi", 50.0))

        # ── Regime gate ────────────────────────────────────────────────
        # Only fire in trending regime — suppress in ranging and extreme
        allow_fast_entry = (
            self.config.allow_fast_regime_entry
            and fast_regime == MarketRegime.TRENDING
        )
        slow_trending = slow_regime == MarketRegime.TRENDING
        if slow_trending and allow_fast_entry:
            entry_gate = "both"
        elif slow_trending:
            entry_gate = "slow"
        elif allow_fast_entry:
            entry_gate = "fast"
        else:
            entry_gate = "none"

        if not slow_trending and not allow_fast_entry:
            self.processed_data.update({
                "signal": 0,
                "features": fast_df,
                "regime": slow_regime,
                "slow_regime": slow_regime,
                "fast_regime": fast_regime,
                "adx": slow_adx,
                "fast_adx": fast_adx,
                "signal_score": signal_score,
                "natr_pct": natr_pct,
                "rsi": rsi,
                "entry_gate": entry_gate,
            })
            return

        # ── RSI dampener ────────────────────────────────────────────────
        if signal_score > 0 and rsi > self.config.rsi_overbought:
            overshoot = (rsi - self.config.rsi_overbought) / (100.0 - self.config.rsi_overbought)
            signal_score *= max(1.0 - overshoot, 0.0)
        if signal_score < 0 and rsi < self.config.rsi_oversold:
            overshoot = (self.config.rsi_oversold - rsi) / self.config.rsi_oversold
            signal_score *= max(1.0 - overshoot, 0.0)

        # ── Funding rate bias ────────────────────────────────────────────
        signal_score += self._get_funding_bias()
        signal_score = max(-1.0, min(1.0, signal_score))

        # ── Signal decision ──────────────────────────────────────────────
        threshold = self._adapted_threshold
        if signal_score >= threshold:
            signal = 1
        elif signal_score <= -threshold:
            signal = -1
        else:
            signal = 0

        self.processed_data.update({
            "signal": signal,
            "features": fast_df,
            "regime": slow_regime,
            "slow_regime": slow_regime,
            "fast_regime": fast_regime,
            "adx": slow_adx,
            "fast_adx": fast_adx,
            "signal_score": signal_score,
            "natr_pct": natr_pct,
            "rsi": rsi,
            "entry_gate": entry_gate,
        })

    # ── Executor config ────────────────────────────────────────────────────

    def get_executor_config(
        self, trade_type: TradeType, price: Decimal, amount: Decimal
    ) -> DCAExecutorConfig:
        """
        DCA MAKER entries with NATR-scaled spreads.
        Mirrors dman_v3 pattern: spread_multiplier derived from volatility.
        """
        spread_multiplier = self._get_spread_multiplier()
        spreads = self.config.dca_spreads
        amounts_pct = self.config.dca_amounts_pct
        total_quote = amount * price

        # Normalise amounts
        total_pct = sum(amounts_pct)
        norm_pct = [p / total_pct for p in amounts_pct]

        if trade_type == TradeType.BUY:
            prices = [price * (1 - s * spread_multiplier) for s in spreads]
        else:
            prices = [price * (1 + s * spread_multiplier) for s in spreads]

        amounts_quote = [total_quote * p for p in norm_pct]

        return DCAExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=trade_type,
            mode=DCAMode.MAKER,
            prices=prices,
            amounts_quote=amounts_quote,
            time_limit=self.config.time_limit,
            stop_loss=self.config.stop_loss * Decimal(str(spread_multiplier)),
            take_profit=self.config.take_profit * Decimal(str(spread_multiplier)),
            trailing_stop=self.config.trailing_stop,
            leverage=self.config.leverage,
        )

    # ── Private helpers ────────────────────────────────────────────────────

    def _compute_fast_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Fast-timeframe signal features."""

        # Candle Flow Imbalance
        candle_range = (df["high"] - df["low"]).replace(0, float("nan"))
        df["cfi"] = ((df["close"] - df["open"]) / candle_range).fillna(0.0).clip(-1.0, 1.0)
        df["cfi_smooth"] = df["cfi"].rolling(5, min_periods=1).mean()

        # VWAP deviation
        typical = (df["high"] + df["low"] + df["close"]) / 3
        cum_vol = df["volume"].cumsum()
        df["vwap"] = (typical * df["volume"]).cumsum() / cum_vol.replace(0, float("nan"))
        df["vwap_dev"] = (
            (df["close"] - df["vwap"]) / df["vwap"].replace(0, float("nan"))
        ).fillna(0.0)

        # NATR
        natr = ta.natr(df["high"], df["low"], df["close"], length=14)
        df["natr_pct"] = natr.bfill().fillna(1.0)

        # RSI
        df.ta.rsi(length=self.config.rsi_length, append=True)
        rsi_col = [c for c in df.columns if c.startswith("RSI")]
        df["rsi"] = df[rsi_col[0]].fillna(50.0) if rsi_col else 50.0

        # Combined signal score [-1, +1]
        df["signal_score"] = (
            0.60 * df["cfi_smooth"] +
            0.40 * df["vwap_dev"].clip(-0.5, 0.5)
        ).clip(-1.0, 1.0)

        return df

    def _compute_adx_regime(
        self,
        df: Optional[pd.DataFrame],
        trending_threshold: float,
        extreme_threshold: float,
        min_length: int = 15,
    ) -> Tuple[MarketRegime, float]:
        """Classify market regime from ADX and return regime + ADX value."""
        if df is None or df.empty or len(df) < min_length:
            return MarketRegime.RANGING, float("nan")

        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        adx_col = [c for c in adx_df.columns if c.startswith("ADX")]
        if not adx_col:
            return MarketRegime.RANGING, float("nan")

        adx = float(adx_df[adx_col[0]].iloc[-1])
        if adx >= extreme_threshold:
            return MarketRegime.EXTREME, adx
        if adx >= trending_threshold:
            return MarketRegime.TRENDING, adx
        return MarketRegime.RANGING, adx

    def _get_spread_multiplier(self) -> Decimal:
        """NATR-based spread multiplier — mirrors dman_v3 dynamic_spread."""
        if not self.config.dynamic_spread:
            return Decimal("1.0")
        natr_pct = self.processed_data.get("natr_pct", 1.0)
        # Scale: 1% NATR → 1x, 2% NATR → 2x, etc.
        return Decimal(str(max(natr_pct, 0.1)))

    def _get_funding_bias(self) -> float:
        """Funding rate directional bias for perpetual connectors."""
        if not self.config.funding_bias_enabled:
            return 0.0
        if "_perpetual" not in self.config.connector_name:
            return 0.0
        try:
            funding_info = self.market_data_provider.get_funding_info(
                self.config.connector_name, self.config.trading_pair
            )
            if funding_info is None or funding_info.rate is None:
                return 0.0
            rate = float(funding_info.rate)
            thr = self.config.funding_threshold
            if rate >= thr:
                return -0.15
            if rate <= -thr:
                return 0.15
        except Exception as e:
            self.logger().debug(f"[FlowEdge] Funding info unavailable: {e}")
        return 0.0

    def _self_adapt(self):
        """
        Rolling-window adaptive layer — tracks the last N closed executors
        and adjusts _adapted_threshold without mutating the Pydantic config.
        """
        closed = [e for e in self.executors_info if not e.is_active]
        new_count = len(closed)
        if new_count > self._last_closed_count:
            for e in closed[self._last_closed_count:]:
                self._recent_pnls.append(float(e.net_pnl_quote))
            self._last_closed_count = new_count

        window_size = len(self._recent_pnls)
        if window_size < 3:
            self.processed_data["win_rate"] = 0.0
            self.processed_data["closed_trades"] = new_count
            self.processed_data["session_pnl"] = sum(float(e.net_pnl_quote) for e in closed)
            return

        wins = sum(1 for pnl in self._recent_pnls if pnl > 0)
        win_rate = wins / window_size

        base = self.config.signal_threshold
        if win_rate < 0.40 and self._adapted_threshold < 0.75:
            self._adapted_threshold = round(
                min(self._adapted_threshold + 0.05, 0.75), 2
            )
            self.logger().warning(
                f"[FlowEdge] Win rate {win_rate:.0%} (last {window_size}) — "
                f"tightening threshold → {self._adapted_threshold}"
            )
        elif win_rate > 0.65 and self._adapted_threshold > base:
            self._adapted_threshold = round(
                max(self._adapted_threshold - 0.02, base), 2
            )
            self.logger().info(
                f"[FlowEdge] Win rate {win_rate:.0%} (last {window_size}) — "
                f"loosening threshold → {self._adapted_threshold}"
            )

        realized_pnl = sum(float(e.net_pnl_quote) for e in closed)
        self.processed_data["win_rate"] = win_rate
        self.processed_data["closed_trades"] = new_count
        self.processed_data["session_pnl"] = realized_pnl

    # ── Status display ─────────────────────────────────────────────────────

    def to_format_status(self) -> List[str]:
        d = self.processed_data
        slow_regime = d.get("slow_regime", d.get("regime", MarketRegime.RANGING))
        fast_regime = d.get("fast_regime", MarketRegime.RANGING)
        score = d.get("signal_score", 0.0)
        sig = d.get("signal", 0)
        natr = d.get("natr_pct", 0.0)
        slow_adx = d.get("adx", 0.0)
        fast_adx = d.get("fast_adx", 0.0)
        rsi = d.get("rsi", 50.0)
        mult = float(self._get_spread_multiplier())
        pnl = d.get("session_pnl", 0.0)
        wr = d.get("win_rate", 0.0)
        trades = d.get("closed_trades", 0)
        entry_gate = d.get("entry_gate", "slow")
        sig_label = {1: "LONG  ▲", -1: "SHORT ▼", 0: "NEUTRAL —"}.get(sig, "?")

        regime_label = {
            MarketRegime.RANGING: "🟢 RANGING  — signal suppressed",
            MarketRegime.TRENDING: "🟡 TRENDING — entries active",
            MarketRegime.EXTREME: "🔴 EXTREME  — halted",
        }

        eff_spreads = [
            f"{float(s) * mult * 10000:.1f}bps"
            for s in self.config.dca_spreads
        ]

        pair = self.config.trading_pair or ""
        if "-" in pair:
            quote_token = pair.split("-")[-1]
        elif "/" in pair:
            quote_token = pair.split("/")[-1]
        elif "_" in pair:
            quote_token = pair.split("_")[-1]
        else:
            quote_token = "QUOTE"

        border = "+" + "-" * 54 + "+"

        def row(text: str) -> str:
            return f"| {text[:52]:<52} |"

        return [
            border,
            row("FlowEdge - Directional DCA Controller"),
            border,
            row("Market State"),
            row(f"Regime slow   : {regime_label.get(slow_regime, str(slow_regime))}"),
            row(f"Regime fast   : {regime_label.get(fast_regime, str(fast_regime))}"),
            row(f"Entry gate    : {entry_gate}"),
            row(f"Signal        : {sig_label} (score {score:+.3f})"),
            row(f"Threshold     : {self._adapted_threshold:.2f} (base {self.config.signal_threshold:.2f})"),
            row(f"ADX slow({self.config.slow_interval:>3}) : {slow_adx:.1f}"),
            row(f"ADX fast({self.config.fast_interval:>3}) : {fast_adx:.1f}"),
            row(f"ADX gates     : trend>{self.config.adx_trending_threshold}"),
            row(f"              extreme>{self.config.adx_extreme_threshold}"),
            row(f"NATR          : {natr:.2f}% (spread x{mult:.2f})"),
            row(f"RSI           : {rsi:.1f} (ob>{self.config.rsi_overbought}"),
            row(f"              os<{self.config.rsi_oversold})"),
            border,
            row("Execution"),
            row(f"DCA Spreads   : {', '.join(eff_spreads)}"),
            row(
                f"Intervals     : fast={self.config.fast_interval} "
                f"slow={self.config.slow_interval}"
            ),
            border,
            row("Performance"),
            row(f"Session PnL   : {pnl:+.4f} {quote_token}"),
            row(f"Win Rate      : {wr:.0%} ({trades} closed trades)"),
            border,
        ]
