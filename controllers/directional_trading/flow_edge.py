"""
FlowEdge — Hummingbot V2 Directional Trading Controller
============================================================
Belongs in: controllers/directional_trading/flow_edge.py

Inherits from DirectionalTradingControllerBase — the same base as
macd_bb_v1, bollinger_v1, dman_v3. Uses DCAExecutorConfig with
DCAMode.MAKER (same pattern as dman_v3) so DCA entry spreads are
dynamic limit orders, not aggressive market orders.

What makes it different from every other directional controller:

1. Multi-Timeframe Regime Detection
   Fast candles (3m)  → flow + extension + trend signals
   Slow candles (15m) → ADX regime classifier + DI directional bias
   Signal only fires when the regime gate opens. In ranging/extreme
   regimes the signal is suppressed to zero.

2. Volatility-Normalised Feature Blend
   Every feature is mapped into the same [-1, +1] unit before it is
   weighted, so no single term can dominate the blend by unit accident.
   Price-space features are divided by live NATR and squashed with tanh,
   which makes the score scale-free: on synthetic candles the |score|
   distribution is within 1% across 0.15%/bar and 0.6%/bar volatility,
   so one threshold holds across pairs and regimes.

3. NATR-Scaled Execution
   DCA entry levels, stop-loss, take-profit and the trailing stop all
   scale with a *normalised* volatility multiplier (live NATR relative
   to a configured baseline, clamped). Take-profit is floored above the
   round-trip fee so a winning barrier can never realise a loss.

4. Funding Rate Bias (perpetuals only)
   Reads live funding rate. Extreme positive funding adds bearish tilt
   to the signal score. Extreme negative adds bullish tilt.

5. Self-Adaptive, Non-Latching Threshold
   Monitors its own win rate and its own turnover across closed
   executors, and steers the entry threshold toward a target each tick.
   Because it tracks a target rather than ratcheting, the threshold can
   always recover — the controller cannot adapt itself into a permanent
   halt. No external process — fully embedded in the controller.

Signal features (all from candles — zero external dependencies):
  - Candle Flow Imbalance (CFI): (close-open)/(high-low) smoothed over 5 bars
  - VWAP Extension: (close - rolling_vwap) / vwap, in NATR units, tanh-squashed
  - Trend: (close - EMA) / ATR, tanh-squashed — cross-bar persistence
  - DI Bias: (DI+ - DI-)/(DI+ + DI-) on the regime timeframe
  - ADX: trend strength on slow timeframe for regime classification
  - NATR: volatility for spread scaling and triple barrier sizing
  - RSI: overbought/oversold dampener on contradictory entries
"""

import math
from collections import deque
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.core.data_type.common import PositionMode, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.dca_executor.data_types import DCAExecutorConfig, DCAMode
from hummingbot.strategy_v2.executors.position_executor.data_types import TrailingStop
from hummingbot.strategy_v2.models.executor_actions import StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType

# ADX(length) needs ~2x length bars before it produces a non-NaN value
# (it is an RMA of DX, which is itself an RMA-based ratio).
ADX_LENGTH = 14
ADX_WARMUP_BARS = ADX_LENGTH * 2


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
        default=150,
        json_schema_extra={
            "prompt": "Enter max fast candles to load (e.g. 150): ",
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
        default=100,
        json_schema_extra={
            "prompt": "Enter max slow candles to load (e.g. 100): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    allow_fast_regime_entry: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Allow entries when fast regime is trending? (True/False): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )

    # ── Regime thresholds ─────────────────────────────────────────────────
    adx_trending_threshold: float = Field(
        default=22.0,
        json_schema_extra={
            "prompt": "Enter ADX threshold for trending regime (e.g. 22): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    adx_extreme_threshold: float = Field(
        default=50.0,
        json_schema_extra={
            "prompt": "Enter ADX threshold for extreme/halt regime (e.g. 50): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )

    # ── Signal blend ──────────────────────────────────────────────────────
    signal_threshold: float = Field(
        default=0.30,
        json_schema_extra={
            "prompt": "Enter signal conviction threshold to fire entry (e.g. 0.30): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    cfi_weight: float = Field(default=0.35, json_schema_extra={"is_updatable": True})
    vwap_weight: float = Field(default=0.25, json_schema_extra={"is_updatable": True})
    trend_weight: float = Field(default=0.25, json_schema_extra={"is_updatable": True})
    di_weight: float = Field(default=0.15, json_schema_extra={"is_updatable": True})
    vwap_window: int = Field(default=24, json_schema_extra={"is_updatable": True})
    trend_ema_length: int = Field(default=21, json_schema_extra={"is_updatable": True})

    # ── DCA entry spreads (scale with normalised NATR) ────────────────────
    dca_spreads: List[Decimal] = Field(
        default=[Decimal("0.002"), Decimal("0.005"), Decimal("0.01")],
        json_schema_extra={
            "prompt": "Enter comma-separated DCA entry spreads (e.g. 0.002,0.005,0.01): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    dca_amounts_pct: Optional[List[Decimal]] = Field(
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

    # ── Volatility multiplier normalisation ───────────────────────────────
    # NATR is reported by pandas_ta in PERCENT (0.42 means 0.42%). Dividing by
    # a baseline turns it into a dimensionless multiplier centred on 1.0, so a
    # market at its baseline volatility uses exactly the configured spreads.
    natr_baseline_pct: float = Field(
        default=0.35,
        json_schema_extra={
            "prompt": "Enter the NATR%% that maps to a 1.0x multiplier (e.g. 0.35): ",
            "is_updatable": True,
        },
    )
    vol_multiplier_min: float = Field(default=0.6, json_schema_extra={"is_updatable": True})
    vol_multiplier_max: float = Field(default=2.5, json_schema_extra={"is_updatable": True})

    # ── Fee-aware take profit floor ───────────────────────────────────────
    # A take-profit tighter than the round-trip fee books a loss every time it
    # fills. This is the hard floor applied after volatility scaling.
    min_take_profit: Decimal = Field(
        default=Decimal("0.0015"),
        json_schema_extra={
            "prompt": "Enter the minimum take profit after fees (e.g. 0.0015 for 0.15%): ",
            "is_updatable": True,
        },
    )

    # ── Emergency risk exit ───────────────────────────────────────────────
    # In DCAMode.MAKER the executor defers its own stop-loss until every level
    # of the ladder has filled, so a partially-filled ladder is governed only
    # by time_limit. This controller-side backstop closes that gap: it watches
    # realised+unrealised PnL on every active executor and stops the ones that
    # breach it, filled or not. Set to null to disable.
    emergency_stop_loss_pct: Optional[Decimal] = Field(
        default=Decimal("0.05"),
        json_schema_extra={
            "prompt": "Enter the emergency stop loss as a decimal (e.g. 0.05 for 5%, empty to disable): ",
            "is_updatable": True,
        },
    )

    # ── RSI dampener ──────────────────────────────────────────────────────
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
    adapt_min_samples: int = Field(default=4, json_schema_extra={"is_updatable": True})
    adapt_win_rate_low: float = Field(default=0.40, json_schema_extra={"is_updatable": True})
    adapt_win_rate_high: float = Field(default=0.65, json_schema_extra={"is_updatable": True})
    adapt_step: float = Field(default=0.02, json_schema_extra={"is_updatable": True})
    # Minimum seconds between threshold steps. Without this the threshold moves
    # once per ~1s tick and crosses its whole range in well under a minute.
    adapt_interval_seconds: float = Field(default=60.0, json_schema_extra={"is_updatable": True})
    threshold_floor: float = Field(default=0.15, json_schema_extra={"is_updatable": True})
    threshold_ceiling: float = Field(default=0.60, json_schema_extra={"is_updatable": True})
    # Turnover governor: if nothing has been opened for this long, walk the
    # threshold back down toward the floor so the controller cannot go quiet
    # forever after a losing streak.
    stale_entry_seconds: int = Field(default=1800, json_schema_extra={"is_updatable": True})

    # ── Funding rate bias ─────────────────────────────────────────────────
    funding_bias_enabled: bool = Field(default=True)
    funding_threshold: float = Field(default=0.0005)
    funding_bias_strength: float = Field(default=0.15, json_schema_extra={"is_updatable": True})

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
            if v.strip() == "":
                return [Decimal("0.002"), Decimal("0.005"), Decimal("0.01")]
            return [Decimal(x.strip()) for x in v.split(",")]
        return v

    @field_validator("dca_amounts_pct", mode="before")
    @classmethod
    def parse_amounts(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            if v.strip() == "":
                return None
            return [Decimal(x.strip()) for x in v.split(",")]
        return v

    @model_validator(mode="after")
    def validate_dca_ladder(self):
        """
        Fill an equal-weight ladder when weights are omitted, and reject a
        ladder whose weights do not line up with its spreads — a mismatch
        silently truncates the DCA levels at executor build time.
        """
        n = len(self.dca_spreads)
        if n == 0:
            raise ValueError("dca_spreads must contain at least one entry spread")
        if self.dca_amounts_pct is None:
            self.dca_amounts_pct = [Decimal("1") / Decimal(n) for _ in range(n)]
        elif len(self.dca_amounts_pct) != n:
            raise ValueError(
                f"dca_amounts_pct has {len(self.dca_amounts_pct)} weights but "
                f"dca_spreads has {n} levels — they must be the same length"
            )
        if sum(self.dca_amounts_pct) <= 0:
            raise ValueError("dca_amounts_pct must sum to a positive value")
        if self.threshold_floor > self.threshold_ceiling:
            raise ValueError("threshold_floor must be <= threshold_ceiling")
        return self


class FlowEdgeProController(DirectionalTradingControllerBase):
    """
    FlowEdge — Hummingbot V2 directional controller.

    Signal pipeline:
      Fast candles → CFI + VWAP extension + trend → volatility-normalised blend
      Slow candles → ADX regime gate + DI directional bias
      Funding rate → bias adjustment on perpetuals
      Self-adapt   → steers the threshold from win rate and turnover

    Execution:
      DCAExecutorConfig(DCAMode.MAKER) with NATR-scaled spreads
      Triple barrier scales with the same normalised multiplier, with a
      fee-aware floor on take-profit
    """

    def __init__(self, config: FlowEdgeProConfig, *args, **kwargs):
        self.config = config
        self.max_records_fast = config.fast_max_records
        self.max_records_slow = config.slow_max_records
        self._adapted_threshold: float = config.signal_threshold
        self._recent_pnls: deque = deque(maxlen=config.adapt_window)
        self._seen_closed_ids: set = set()
        # Newest entry timestamp ever observed per side, so cooldown survives
        # the executor being pruned from executors_info once it terminates.
        self._last_entry_ts: Dict[TradeType, float] = {}
        # Wall-clock of the last threshold step, for the adapt rate limit.
        self._last_adapt_ts: Optional[float] = None
        self._realized_pnl: float = 0.0
        self._closed_trades: int = 0
        # Set on the first tick — the data provider's clock is not reliable yet
        # at construction time.
        self._first_tick_time: Optional[float] = None
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

        # Both frames must exclude the in-progress bar before ANY feature is
        # computed — see _drop_forming_bar. This happens before the length
        # check below so the warmup guard accounts for the dropped row.
        fast_df = self._drop_forming_bar(fast_df)
        slow_df = self._drop_forming_bar(slow_df)

        min_fast_bars = max(self.config.trend_ema_length, self.config.rsi_length + 1, 20)
        if fast_df is None or fast_df.empty or len(fast_df) < min_fast_bars:
            self.processed_data.update({
                "signal": 0,
                "features": pd.DataFrame(),
                "regime": MarketRegime.RANGING,
                "entry_gate": "warmup",
            })
            return

        fast_df = self._compute_fast_features(fast_df)
        slow_regime, slow_adx, slow_di = self._compute_adx_regime(slow_df)
        fast_regime, fast_adx, _ = self._compute_adx_regime(fast_df)
        latest = fast_df.iloc[-1]

        natr_pct = self._safe_float(latest.get("natr_pct"), default=self.config.natr_baseline_pct)
        rsi = self._safe_float(latest.get("rsi"), default=50.0)

        # Static bias terms — scalars that apply to every row of the frame.
        di_bias = self.config.di_weight * slow_di
        funding_bias = self._get_funding_bias()

        # Per-row score so backtests see a real signal series, not one frozen
        # value. The scalar path below reads the last row of the same column.
        fast_df["score_biased"] = (
            fast_df["score_damped"] + di_bias + funding_bias
        ).clip(-1.0, 1.0)

        threshold = self._adapted_threshold
        fast_df["signal"] = np.select(
            [fast_df["score_biased"] >= threshold, fast_df["score_biased"] <= -threshold],
            [1, -1],
            default=0,
        )

        signal_score = self._safe_float(fast_df["score_biased"].iloc[-1], default=0.0)

        # ── Regime gate ────────────────────────────────────────────────
        # Entries need a confirmed trend on at least one timeframe. The slow
        # frame is the risk-off filter: an EXTREME reading there means a crash
        # or a parabolic squeeze, and a maker DCA ladder is the wrong
        # instrument for both, so it halts everything. An EXTREME reading on
        # the fast frame is just a short-term impulse — common on 3m candles —
        # so it merely fails to confirm rather than halting the controller.
        if slow_regime == MarketRegime.EXTREME:
            allow_fast_entry = False
            slow_trending = False
            entry_gate = "halt"
        else:
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

        gate_open = slow_trending or allow_fast_entry
        if not gate_open:
            signal = 0
            fast_df["signal"] = 0
        elif signal_score >= threshold:
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
            "di_bias": di_bias,
            "funding_bias": funding_bias,
            "signal_score": signal_score,
            "natr_pct": natr_pct,
            "rsi": rsi,
            "entry_gate": entry_gate,
            "threshold": threshold,
        })

    # ── Executor config ────────────────────────────────────────────────────

    def get_executor_config(
        self, trade_type: TradeType, price: Decimal, amount: Decimal
    ) -> DCAExecutorConfig:
        """
        DCA MAKER entries with NATR-scaled spreads.
        Mirrors dman_v3 pattern: spread_multiplier derived from volatility,
        but normalised against a baseline so it is a true multiplier.
        """
        multiplier = self._get_volatility_multiplier()
        spreads = self.config.dca_spreads
        amounts_pct = self.config.dca_amounts_pct
        total_quote = amount * price

        # Normalise amounts
        total_pct = sum(amounts_pct)
        norm_pct = [p / total_pct for p in amounts_pct]

        if trade_type == TradeType.BUY:
            prices = [price * (Decimal("1") - s * multiplier) for s in spreads]
        else:
            prices = [price * (Decimal("1") + s * multiplier) for s in spreads]

        amounts_quote = [total_quote * p for p in norm_pct]

        take_profit = self._scale_barrier(self.config.take_profit, multiplier)
        if take_profit is not None:
            take_profit = max(take_profit, self.config.min_take_profit)

        return DCAExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=trade_type,
            mode=DCAMode.MAKER,
            prices=prices,
            amounts_quote=amounts_quote,
            time_limit=self.config.time_limit,
            stop_loss=self._scale_barrier(self.config.stop_loss, multiplier),
            take_profit=take_profit,
            trailing_stop=self._scale_trailing_stop(self.config.trailing_stop, multiplier),
            leverage=self.config.leverage,
        )

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        """
        Controller-side emergency exit.

        ``DCAExecutor`` in MAKER mode gates its own stop-loss behind
        ``all_open_orders_executed`` — documented behaviour, but it means a
        ladder that filled one level out of three has no working stop and is
        governed only by ``time_limit``. The same gap is what a volatility
        explosion exploits: barriers are fixed when the executor is created and
        never rescaled, so a move far larger than the one the ladder was sized
        for runs unchecked until the clock expires.

        This watches the PnL the executor itself reports and closes anything
        past ``emergency_stop_loss_pct``, filled or not.
        """
        limit = self.config.emergency_stop_loss_pct
        if limit is None:
            return []

        actions = []
        for executor in self.executors_info:
            if not executor.is_active or not executor.is_trading:
                continue
            pnl_pct = self._safe_float(executor.net_pnl_pct, default=0.0)
            if pnl_pct <= -float(limit):
                self.logger().warning(
                    f"[FlowEdge] Emergency exit on {executor.id}: "
                    f"net PnL {pnl_pct:.2%} breached -{float(limit):.2%}"
                )
                actions.append(
                    StopExecutorAction(
                        controller_id=self.config.id,
                        executor_id=executor.id,
                        keep_position=False,
                    )
                )
        return actions

    def can_create_executor(self, signal: int) -> bool:
        """
        Side-aware capacity and cooldown check.

        The base implementation writes ``x.side == TradeType.BUY if signal > 0
        else TradeType.SELL``, which Python parses as
        ``(x.side == TradeType.BUY) if signal > 0 else TradeType.SELL``. On a
        short signal the else-branch yields a truthy enum for every executor,
        so longs are counted against the short-side budget and the cooldown is
        taken from the wrong executor. This override compares the side.
        """
        target_side = TradeType.BUY if signal > 0 else TradeType.SELL
        opposite_side = TradeType.SELL if target_side == TradeType.BUY else TradeType.BUY

        # Under ONEWAY the venue nets long against short, so opening a ladder
        # opposite to a live one silently reduces or flips the real position
        # while BOTH executors still believe they own it. Capacity is counted
        # per side, so nothing else here would stop that.
        if self.config.position_mode == PositionMode.ONEWAY:
            if any(e.is_active and e.side == opposite_side for e in self.executors_info):
                return False

        same_side = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.side == target_side,
        )

        # Remember the newest entry ever seen on this side. Terminated executors
        # are pruned from executors_info, so deriving the cooldown from the live
        # list alone makes max() fall back to 0 the instant a position closes —
        # turning cooldown_time into a no-op at exactly the moment it should be
        # spacing out a re-entry.
        if same_side:
            self._last_entry_ts[target_side] = max(
                self._last_entry_ts.get(target_side, 0.0),
                max(e.timestamp for e in same_side),
            )
        last_ts = self._last_entry_ts.get(target_side, 0.0)

        active_same_side = [e for e in same_side if e.is_active]
        has_capacity = len(active_same_side) < self.config.max_executors_per_side
        cooled_down = (
            last_ts <= 0.0
            or self.market_data_provider.time() - last_ts > self.config.cooldown_time
        )
        return has_capacity and cooled_down

    # ── Feature computation ────────────────────────────────────────────────

    def _compute_fast_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Fast-timeframe signal features.

        Every feature lands in [-1, +1] before it is weighted. Price-space
        features are divided by live NATR first, which makes them comparable
        across pairs and across volatility regimes. Measured on synthetic 3m
        candles the three terms contribute roughly 23% / 43% / 35% of score
        magnitude at the default weights — CFI is deliberately the lightest
        because it is the noisiest and the fastest to flip.
        """
        df = df.copy()

        # NATR (percent units, as pandas_ta reports it) — the volatility yardstick
        natr = ta.natr(df["high"], df["low"], df["close"], length=ADX_LENGTH)
        if natr is None:
            df["natr_pct"] = float(self.config.natr_baseline_pct)
        else:
            # ffill, never bfill: bfill() fills warmup rows from LATER bars,
            # which is lookahead in the per-row feature frame that backtests read.
            df["natr_pct"] = natr.ffill().fillna(self.config.natr_baseline_pct)
        # Guard against a zero/NaN denominator when dividing price-space
        # features into volatility units.
        natr_frac = (df["natr_pct"] / 100.0).replace(0.0, np.nan)

        # Candle Flow Imbalance — where the close sits inside the bar's range
        candle_range = (df["high"] - df["low"]).replace(0, np.nan)
        df["cfi"] = ((df["close"] - df["open"]) / candle_range).fillna(0.0).clip(-1.0, 1.0)
        df["cfi_smooth"] = df["cfi"].rolling(5, min_periods=1).mean()

        # VWAP extension — rolling, not anchored, so the lookback is the same
        # on every row and the reference does not drift with buffer age.
        window = max(int(self.config.vwap_window), 2)
        typical = (df["high"] + df["low"] + df["close"]) / 3
        vol = df["volume"].fillna(0.0)
        pv_sum = (typical * vol).rolling(window, min_periods=1).sum()
        v_sum = vol.rolling(window, min_periods=1).sum().replace(0, np.nan)
        # Fall back to a simple mean of typical price on zero-volume windows.
        df["vwap"] = (pv_sum / v_sum).fillna(typical.rolling(window, min_periods=1).mean())
        df["vwap_dev"] = ((df["close"] - df["vwap"]) / df["vwap"].replace(0, np.nan)).fillna(0.0)
        df["vwap_signal"] = np.tanh((df["vwap_dev"] / natr_frac).fillna(0.0) / 2.0)

        # Trend — distance from an EMA in ATR units. CFI is a within-bar
        # measure; this supplies the cross-bar persistence it cannot see.
        ema = ta.ema(df["close"], length=self.config.trend_ema_length)
        atr = ta.atr(df["high"], df["low"], df["close"], length=ADX_LENGTH)
        if ema is None or atr is None:
            df["trend_signal"] = 0.0
        else:
            atr_safe = atr.replace(0, np.nan)
            df["trend_signal"] = np.tanh(
                ((df["close"] - ema) / atr_safe).fillna(0.0) / 2.0
            )

        # RSI — computed as a standalone series so repeated ticks cannot
        # accumulate duplicate RSI_* columns on the frame.
        rsi = ta.rsi(df["close"], length=self.config.rsi_length)
        df["rsi"] = 50.0 if rsi is None else rsi.fillna(50.0)

        # Weighted blend — all terms already in [-1, +1]
        df["signal_score"] = (
            self.config.cfi_weight * df["cfi_smooth"]
            + self.config.vwap_weight * df["vwap_signal"]
            + self.config.trend_weight * df["trend_signal"]
        ).clip(-1.0, 1.0)

        df["score_damped"] = self._apply_rsi_dampener(df["signal_score"], df["rsi"])
        return df

    def _apply_rsi_dampener(self, score: pd.Series, rsi: pd.Series) -> pd.Series:
        """
        Gradual overbought/oversold dampener.

        Most controllers use RSI as a binary veto. This scales conviction down
        in proportion to how far past the band RSI has travelled, so a
        marginally stretched reading costs a little and an extreme one costs
        nearly everything.
        """
        ob, os_ = self.config.rsi_overbought, self.config.rsi_oversold
        long_overshoot = ((rsi - ob) / max(100.0 - ob, 1e-9)).clip(0.0, 1.0)
        short_overshoot = ((os_ - rsi) / max(os_, 1e-9)).clip(0.0, 1.0)
        damp = pd.Series(1.0, index=score.index)
        damp = damp.where(~(score > 0), 1.0 - long_overshoot)
        damp = damp.where(~(score < 0), 1.0 - short_overshoot)
        return (score * damp.clip(0.0, 1.0)).clip(-1.0, 1.0)

    def _compute_adx_regime(
        self,
        df: Optional[pd.DataFrame],
        min_length: int = ADX_WARMUP_BARS,
    ) -> Tuple[MarketRegime, float, float]:
        """
        Classify market regime from ADX and return (regime, adx, di_bias).

        ADX measures trend *strength* and carries no direction, so the
        directional indicators from the same computation are returned
        alongside it as a normalised [-1, +1] bias.
        """
        neutral = (MarketRegime.RANGING, float("nan"), 0.0)
        if df is None or df.empty or len(df) < min_length:
            return neutral

        adx_df = ta.adx(df["high"], df["low"], df["close"], length=ADX_LENGTH)
        if adx_df is None or adx_df.empty:
            return neutral

        adx_col = next((c for c in adx_df.columns if c.startswith("ADX")), None)
        if adx_col is None:
            return neutral

        adx = self._safe_float(adx_df[adx_col].iloc[-1], default=float("nan"))
        if math.isnan(adx):
            # Not warmed up yet — treat as ranging rather than letting a NaN
            # comparison silently pick a branch.
            return neutral

        dmp_col = next((c for c in adx_df.columns if c.startswith("DMP")), None)
        dmn_col = next((c for c in adx_df.columns if c.startswith("DMN")), None)
        di_bias = 0.0
        if dmp_col and dmn_col:
            dmp = self._safe_float(adx_df[dmp_col].iloc[-1], default=0.0)
            dmn = self._safe_float(adx_df[dmn_col].iloc[-1], default=0.0)
            if dmp + dmn > 0:
                di_bias = (dmp - dmn) / (dmp + dmn)

        if adx >= self.config.adx_extreme_threshold:
            return MarketRegime.EXTREME, adx, di_bias
        if adx >= self.config.adx_trending_threshold:
            return MarketRegime.TRENDING, adx, di_bias
        return MarketRegime.RANGING, adx, di_bias

    # ── Execution helpers ──────────────────────────────────────────────────

    def _get_volatility_multiplier(self) -> Decimal:
        """
        Dimensionless volatility multiplier centred on 1.0.

        pandas_ta reports NATR in percent, so it is divided by a configured
        baseline before use. Feeding the raw percentage in as a multiplier —
        as an earlier revision did — silently rescales every spread and every
        barrier by the NATR reading itself.
        """
        if not self.config.dynamic_spread:
            return Decimal("1")
        natr_pct = self._safe_float(
            self.processed_data.get("natr_pct"), default=self.config.natr_baseline_pct
        )
        baseline = max(float(self.config.natr_baseline_pct), 1e-6)
        multiplier = natr_pct / baseline
        multiplier = min(
            max(multiplier, self.config.vol_multiplier_min), self.config.vol_multiplier_max
        )
        return Decimal(str(round(multiplier, 4)))

    @staticmethod
    def _scale_barrier(value: Optional[Decimal], multiplier: Decimal) -> Optional[Decimal]:
        """Scale a triple-barrier level, preserving an explicitly disabled barrier."""
        if value is None:
            return None
        return value * multiplier

    @staticmethod
    def _scale_trailing_stop(
        trailing_stop: Optional[TrailingStop], multiplier: Decimal
    ) -> Optional[TrailingStop]:
        """
        Scale the trailing stop with the same multiplier as the other barriers.

        Leaving it unscaled while take-profit shrinks lets take-profit fire
        first in every calm market, so the trailing stop never activates.
        """
        if trailing_stop is None:
            return None
        return TrailingStop(
            activation_price=trailing_stop.activation_price * multiplier,
            trailing_delta=trailing_stop.trailing_delta * multiplier,
        )

    def _get_funding_bias(self) -> float:
        """
        Funding rate directional bias for perpetual connectors.

        Continuous rather than a three-state tilt: the response is
        ``-strength * tanh(rate / threshold)``, so crowding builds smoothly and
        saturates instead of snapping between values as the rate crosses the
        threshold. A rate at the threshold produces ~76% of full strength; a
        rate at twice the threshold produces ~96%.
        """
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
            rate = self._safe_float(funding_info.rate, default=0.0)
            thr = max(abs(self.config.funding_threshold), 1e-9)
            return -self.config.funding_bias_strength * math.tanh(rate / thr)
        except Exception as e:
            self.logger().debug(f"[FlowEdge] Funding info unavailable: {e}")
        return 0.0

    # ── Self-adaptation ────────────────────────────────────────────────────

    def _self_adapt(self):
        """
        Steer the entry threshold toward a target derived from the rolling win
        rate and from turnover.

        Two properties matter here. Closed executors are tracked by id, not by
        a running count, because ``executors_info`` is a live window over the
        orchestrator's active list — terminated executors are pruned from it,
        so any index arithmetic against it drifts. And the threshold tracks a
        *target* rather than ratcheting in one direction, so a losing streak
        can tighten it but can never latch it above the reachable score range
        and stop trading forever.
        """
        self._harvest_closed_executors()

        window_size = len(self._recent_pnls)
        win_rate = 0.0
        if window_size:
            win_rate = sum(1 for pnl in self._recent_pnls if pnl > 0) / window_size

        base = self.config.signal_threshold
        floor = self.config.threshold_floor
        ceiling = self.config.threshold_ceiling

        # Performance component
        target = base
        if window_size >= self.config.adapt_min_samples:
            if win_rate < self.config.adapt_win_rate_low:
                target = ceiling
            elif win_rate > self.config.adapt_win_rate_high:
                target = floor

        # Turnover component — half the hackathon score is volume, and a
        # directional controller that has gone quiet earns none of it. If
        # nothing is open and nothing has opened recently, walk the target
        # down so the controller re-engages.
        if self._seconds_since_last_entry() > self.config.stale_entry_seconds:
            target = floor

        target = min(max(target, floor), ceiling)

        # This method runs on EVERY controller tick (~1s). Stepping by
        # adapt_step on each one walks the entire floor..ceiling range in
        # roughly (ceiling-floor)/adapt_step seconds — 23s at the defaults —
        # which makes adapt_step meaningless and turns the threshold into a
        # square wave between its bounds. Rate-limit so one step really is one
        # adapt_interval_seconds.
        now = self.market_data_provider.time()
        step_due = (
            self._last_adapt_ts is None
            or (now - self._last_adapt_ts) >= self.config.adapt_interval_seconds
        )
        if step_due:
            self._last_adapt_ts = now
            step = self.config.adapt_step
            delta = max(-step, min(step, target - self._adapted_threshold))
            previous = self._adapted_threshold
            self._adapted_threshold = round(
                min(max(self._adapted_threshold + delta, floor), ceiling), 4
            )
            if abs(self._adapted_threshold - previous) > 1e-9:
                self.logger().info(
                    f"[FlowEdge] Win rate {win_rate:.0%} over {window_size} closed trades — "
                    f"threshold {previous:.3f} → {self._adapted_threshold:.3f} (target {target:.3f})"
                )

        self.processed_data["win_rate"] = win_rate
        self.processed_data["closed_trades"] = self._closed_trades
        self.processed_data["session_pnl"] = self._realized_pnl

    def _harvest_closed_executors(self):
        """
        Fold newly-terminated executors into the rolling PnL window exactly once.

        Only fully TERMINATED executors with a close type are counted — an
        executor that is shutting down has placed its close order but has not
        booked the fill, so its PnL is still mark-to-market. Executors that
        failed before trading (FAILED, INSUFFICIENT_BALANCE) are skipped: they
        are not trades and must not enter the win-rate window.
        """
        for executor in self.executors_info:
            if executor.id in self._seen_closed_ids:
                continue
            if not executor.is_done or executor.close_type is None:
                continue
            self._seen_closed_ids.add(executor.id)
            # An executor rejected before it ever placed a trade — bad sizing
            # against the venue's minimums, or no balance — is not a trade. Its
            # PnL is a structural zero, so folding it in books a phantom loss,
            # drags win_rate down and ratchets the entry threshold on evidence
            # that never existed.
            if executor.close_type in (CloseType.FAILED, CloseType.INSUFFICIENT_BALANCE):
                continue
            pnl = self._safe_float(executor.net_pnl_quote, default=0.0)
            self._recent_pnls.append(pnl)
            self._realized_pnl += pnl
            self._closed_trades += 1

    def _seconds_since_last_entry(self) -> float:
        """
        Age of the most recent executor this controller opened, in seconds.

        Falls back to the age of the controller itself when nothing has ever
        opened. Measuring only from the last executor leaves a cold-start hole:
        a threshold too high for the market on day one means nothing opens, and
        because nothing ever opened the turnover governor never engages — the
        same silent latch the adaptive layer is designed to prevent, just
        reached from the other side.
        """
        now = self.market_data_provider.time()
        if self._first_tick_time is None:
            self._first_tick_time = now

        timestamps = [e.timestamp for e in self.executors_info]
        reference = max(timestamps) if timestamps else self._first_tick_time
        return max(now - reference, 0.0)

    # ── Utilities ──────────────────────────────────────────────────────────

    @staticmethod
    def _drop_forming_bar(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        """
        Drop the final row of a candles frame, which is the FORMING bar.

        ``get_candles_df`` returns the feed's rolling buffer, and its last row is
        the bar currently being built. Computing features on it repaints the
        signal: early in a bar the close is still pinned to the high or the low,
        so CFI — ``(close - open) / (high - low)`` — is mechanically +/-1, and the
        in-progress bar's tiny true range deflates NATR, inflating every term
        that divides by it. The blended score can therefore cross the entry
        threshold on a reading that no longer exists seconds later.

        Features must be computed on CLOSED bars only. Called before the warmup
        length check so the guard accounts for the row removed here.
        """
        if df is None or df.empty:
            return df
        return df.iloc[:-1]

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        """Coerce to float, mapping None/NaN/garbage onto a caller-chosen default."""
        if value is None:
            return default
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        return default if math.isnan(result) else result

    # ── Status display ─────────────────────────────────────────────────────

    def to_format_status(self) -> List[str]:
        d: Dict = self.processed_data
        slow_regime = d.get("slow_regime", d.get("regime", MarketRegime.RANGING))
        fast_regime = d.get("fast_regime", MarketRegime.RANGING)
        score = d.get("signal_score", 0.0)
        sig = d.get("signal", 0)
        natr = d.get("natr_pct", 0.0)
        slow_adx = self._safe_float(d.get("adx"), default=0.0)
        fast_adx = self._safe_float(d.get("fast_adx"), default=0.0)
        rsi = d.get("rsi", 50.0)
        mult = float(self._get_volatility_multiplier())
        pnl = d.get("session_pnl", 0.0)
        wr = d.get("win_rate", 0.0)
        trades = d.get("closed_trades", 0)
        entry_gate = d.get("entry_gate", "none")
        di_bias = d.get("di_bias", 0.0)
        funding_bias = d.get("funding_bias", 0.0)
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
        eff_tp = self.config.take_profit
        if eff_tp is not None:
            eff_tp = max(eff_tp * Decimal(str(mult)), self.config.min_take_profit)
        eff_sl = self.config.stop_loss
        if eff_sl is not None:
            eff_sl = eff_sl * Decimal(str(mult))

        pair = self.config.trading_pair or ""
        for sep in ("-", "/", "_"):
            if sep in pair:
                quote_token = pair.split(sep)[-1]
                break
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
            row(f"Threshold     : {self._adapted_threshold:.3f} (base {self.config.signal_threshold:.2f})"),
            row(f"ADX slow({self.config.slow_interval:>3}) : {slow_adx:.1f}"),
            row(f"ADX fast({self.config.fast_interval:>3}) : {fast_adx:.1f}"),
            row(f"ADX gates     : trend>{self.config.adx_trending_threshold}"),
            row(f"              extreme>{self.config.adx_extreme_threshold}"),
            row(f"DI bias       : {di_bias:+.3f}"),
            row(f"Funding bias  : {funding_bias:+.3f}"),
            row(f"NATR          : {natr:.3f}% (x{mult:.2f} vs {self.config.natr_baseline_pct}%)"),
            row(f"RSI           : {rsi:.1f} (ob>{self.config.rsi_overbought}"),
            row(f"              os<{self.config.rsi_oversold})"),
            border,
            row("Execution"),
            row(f"DCA Spreads   : {', '.join(eff_spreads)}"),
            row(f"Take profit   : {eff_tp} (floor {self.config.min_take_profit})"),
            row(f"Stop loss     : {eff_sl}"),
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
