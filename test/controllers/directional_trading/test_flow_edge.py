import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pydantic

from controllers.directional_trading.flow_edge import FlowEdgeProConfig, FlowEdgeProController, MarketRegime
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.executors.position_executor.data_types import TrailingStop
from hummingbot.strategy_v2.models.executors import CloseType

NOW = 1_700_000_000.0


def make_candles(n=400, seed=7, sigma=0.0025, drift=0.0, start=100.0, interval_s=180):
    """Synthetic OHLCV with genuine intrabar range so CFI/ATR/ADX are meaningful."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, sigma, n)
    close = start * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start], close[:-1]])
    wick = np.abs(rng.normal(0, sigma * 0.8, n)) * close
    return pd.DataFrame({
        "timestamp": np.arange(n) * interval_s,
        "open": open_,
        "high": np.maximum(open_, close) + wick,
        "low": np.minimum(open_, close) - wick,
        "close": close,
        "volume": np.abs(rng.normal(1000, 300, n)),
    })


def closed_executor(executor_id, pnl, timestamp=NOW, side=TradeType.BUY, is_done=True,
                    close_type=CloseType.TAKE_PROFIT):
    """A terminated executor as _harvest_closed_executors sees it."""
    return SimpleNamespace(
        id=executor_id,
        timestamp=timestamp,
        net_pnl_quote=Decimal(str(pnl)),
        is_done=is_done,
        close_type=close_type,
        is_active=False,
        side=side,
    )


def active_executor(executor_id, timestamp=NOW, side=TradeType.BUY):
    return SimpleNamespace(
        id=executor_id,
        timestamp=timestamp,
        net_pnl_quote=Decimal("0"),
        is_done=False,
        close_type=None,
        is_active=True,
        side=side,
    )


class FlowEdgeControllerTests(IsolatedAsyncioWrapperTestCase):

    def setUp(self):
        super().setUp()
        self.config = self.build_config()
        self.controller = self.build_controller(self.config)

    @staticmethod
    def build_config(**overrides):
        kwargs = dict(
            id="test",
            controller_name="flow_edge",
            connector_name="hyperliquid_perpetual",
            trading_pair="XRP-USD",
            total_amount_quote=Decimal("100"),
            candles_connector="hyperliquid_perpetual",
            candles_trading_pair="XRP-USD",
            leverage=2,
            stop_loss=Decimal("0.02"),
            take_profit=Decimal("0.006"),
            time_limit=1800,
            natr_baseline_pct=0.35,
        )
        kwargs.update(overrides)
        return FlowEdgeProConfig(**kwargs)

    @staticmethod
    def build_controller(config):
        mdp = MagicMock(spec=MarketDataProvider)
        mdp.time.return_value = NOW
        mdp.get_funding_info.return_value = None
        controller = FlowEdgeProController(
            config=config,
            market_data_provider=mdp,
            actions_queue=AsyncMock(spec=asyncio.Queue),
        )
        return controller

    def feed_candles(self, controller, fast, slow):
        def get_candles_df(connector_name, trading_pair, interval, max_records):
            src = fast if interval == controller.config.fast_interval else slow
            return src.iloc[-max_records:].reset_index(drop=True)

        controller.market_data_provider.get_candles_df.side_effect = get_candles_df

    # ── Config validation ──────────────────────────────────────────────

    def test_dca_amounts_default_to_equal_split(self):
        config = self.build_config(dca_spreads="0.001,0.002,0.003,0.004")
        self.assertEqual(len(config.dca_amounts_pct), 4)
        self.assertEqual(sum(config.dca_amounts_pct), Decimal("1"))

    def test_mismatched_dca_ladder_is_rejected(self):
        """A ladder whose weights do not line up with its spreads truncates the
        DCA levels at executor build time, so it must fail at config time."""
        with self.assertRaises(pydantic.ValidationError) as ctx:
            self.build_config(dca_spreads="0.002,0.005,0.01", dca_amounts_pct="0.5,0.5")
        self.assertIn("same length", str(ctx.exception))

    def test_empty_dca_spreads_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self.build_config(dca_spreads=[])

    def test_threshold_floor_above_ceiling_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self.build_config(threshold_floor=0.8, threshold_ceiling=0.2)

    # ── Volatility multiplier ──────────────────────────────────────────

    def test_multiplier_is_one_at_baseline_volatility(self):
        """NATR is a percentage; it must be normalised against the baseline
        rather than used directly as a multiplier."""
        self.controller.processed_data["natr_pct"] = 0.35
        self.assertEqual(self.controller._get_volatility_multiplier(), Decimal("1"))

    def test_multiplier_scales_and_clamps(self):
        self.controller.processed_data["natr_pct"] = 0.70
        self.assertEqual(self.controller._get_volatility_multiplier(), Decimal("2"))
        self.controller.processed_data["natr_pct"] = 100.0
        self.assertEqual(
            float(self.controller._get_volatility_multiplier()),
            self.config.vol_multiplier_max,
        )
        self.controller.processed_data["natr_pct"] = 0.0001
        self.assertEqual(
            float(self.controller._get_volatility_multiplier()),
            self.config.vol_multiplier_min,
        )

    def test_multiplier_survives_missing_or_nan_natr(self):
        self.controller.processed_data.pop("natr_pct", None)
        self.assertEqual(self.controller._get_volatility_multiplier(), Decimal("1"))
        self.controller.processed_data["natr_pct"] = float("nan")
        self.assertEqual(self.controller._get_volatility_multiplier(), Decimal("1"))

    def test_dynamic_spread_disabled_pins_multiplier(self):
        controller = self.build_controller(self.build_config(dynamic_spread=False))
        controller.processed_data["natr_pct"] = 5.0
        self.assertEqual(controller._get_volatility_multiplier(), Decimal("1"))

    # ── Executor config ────────────────────────────────────────────────

    def test_take_profit_never_falls_below_fee_floor(self):
        """A take-profit tighter than the round-trip fee books a loss on every
        fill, so the scaled value is floored."""
        self.controller.processed_data["natr_pct"] = 0.01  # deeply calm
        executor = self.controller.get_executor_config(
            TradeType.BUY, Decimal("100"), Decimal("1")
        )
        self.assertGreaterEqual(executor.take_profit, self.config.min_take_profit)

    def test_disabled_barriers_do_not_crash(self):
        controller = self.build_controller(
            self.build_config(stop_loss=None, take_profit=None, trailing_stop=None)
        )
        controller.processed_data["natr_pct"] = 0.35
        executor = controller.get_executor_config(TradeType.SELL, Decimal("100"), Decimal("1"))
        self.assertIsNone(executor.stop_loss)
        self.assertIsNone(executor.take_profit)
        self.assertIsNone(executor.trailing_stop)

    def test_trailing_stop_scales_with_the_other_barriers(self):
        """If take-profit shrinks with volatility but the trailing stop does
        not, take-profit always fires first and the trailing stop is dead."""
        controller = self.build_controller(
            self.build_config(trailing_stop=TrailingStop(
                activation_price=Decimal("0.01"), trailing_delta=Decimal("0.002")))
        )
        controller.processed_data["natr_pct"] = 0.70  # 2x baseline
        executor = controller.get_executor_config(TradeType.BUY, Decimal("100"), Decimal("1"))
        self.assertEqual(executor.trailing_stop.activation_price, Decimal("0.02"))
        self.assertEqual(executor.trailing_stop.trailing_delta, Decimal("0.004"))

    def test_dca_ladder_prices_and_amounts_line_up(self):
        self.controller.processed_data["natr_pct"] = 0.35
        price = Decimal("100")
        buy = self.controller.get_executor_config(TradeType.BUY, price, Decimal("2"))
        self.assertEqual(len(buy.prices), len(buy.amounts_quote))
        self.assertTrue(all(p < price for p in buy.prices))
        self.assertEqual(sum(buy.amounts_quote), price * Decimal("2"))

        sell = self.controller.get_executor_config(TradeType.SELL, price, Decimal("2"))
        self.assertTrue(all(p > price for p in sell.prices))

    # ── Capacity / cooldown gating ─────────────────────────────────────

    def test_short_signal_only_counts_short_executors(self):
        """The base implementation's ternary binds so that a short signal
        counts every active executor against the short budget."""
        self.controller.executors_info = [
            active_executor("long-1", timestamp=0, side=TradeType.BUY),
            active_executor("long-2", timestamp=0, side=TradeType.BUY),
        ]
        # max_executors_per_side defaults to 2 and both longs are full, but the
        # short side is untouched and its cooldown has long expired.
        self.assertTrue(self.controller.can_create_executor(-1))
        self.assertFalse(self.controller.can_create_executor(1))

    def test_cooldown_blocks_a_fresh_entry_on_the_same_side(self):
        self.controller.executors_info = [
            active_executor("long-1", timestamp=NOW - 10, side=TradeType.BUY),
        ]
        self.assertFalse(self.controller.can_create_executor(1))

    # ── Signal construction ────────────────────────────────────────────

    def test_score_is_bounded_and_scale_free(self):
        """Each feature is normalised into [-1, 1], so the score distribution
        should barely move between a calm market and a volatile one."""
        stats = {}
        for label, sigma in (("calm", 0.0015), ("volatile", 0.0060)):
            frame = self.controller._compute_fast_features(
                make_candles(n=1500, sigma=sigma, seed=3)
            )
            score = frame["score_damped"]
            self.assertTrue(score.between(-1.0, 1.0).all())
            stats[label] = score.abs().mean()
        self.assertAlmostEqual(stats["calm"], stats["volatile"], delta=0.05)

    async def test_features_carry_a_per_row_signal_column(self):
        """Backtesting reads a signal *series* off the features frame; a
        controller that only sets the scalar backtests as one frozen value."""
        controller = self.controller
        self.feed_candles(controller, make_candles(n=400, drift=0.0004),
                          make_candles(n=200, sigma=0.005, interval_s=900))
        await controller.update_processed_data()
        features = controller.processed_data["features"]
        self.assertIn("signal", features.columns)
        self.assertEqual(len(features), len(features["signal"].dropna()))
        self.assertTrue(set(features["signal"].unique()).issubset({-1, 0, 1}))

    def test_rsi_dampener_is_gradual_not_binary(self):
        score = pd.Series([0.8, 0.8, 0.8, 0.8])
        rsi = pd.Series([50.0, 72.0, 85.0, 100.0])
        damped = self.controller._apply_rsi_dampener(score, rsi)
        self.assertAlmostEqual(damped.iloc[0], 0.8)          # untouched inside the band
        self.assertLess(damped.iloc[1], 0.8)                 # mildly overbought -> small cut
        self.assertGreater(damped.iloc[1], damped.iloc[2])   # more overbought -> bigger cut
        self.assertAlmostEqual(damped.iloc[3], 0.0)          # fully extended -> erased

    def test_rsi_dampener_leaves_the_opposing_side_alone(self):
        """An overbought reading should not penalise a short."""
        damped = self.controller._apply_rsi_dampener(
            pd.Series([-0.8]), pd.Series([85.0])
        )
        self.assertAlmostEqual(damped.iloc[0], -0.8)

    async def test_warmup_returns_flat_signal_without_crashing(self):
        self.feed_candles(self.controller, make_candles(n=5), make_candles(n=5))
        await self.controller.update_processed_data()
        self.assertEqual(self.controller.processed_data["signal"], 0)
        self.assertEqual(self.controller.processed_data["entry_gate"], "warmup")

    async def test_regime_gate_suppresses_signal_when_not_trending(self):
        controller = self.build_controller(
            # An unreachable trending threshold forces both frames to RANGING.
            self.build_config(adx_trending_threshold=999.0, adx_extreme_threshold=1000.0)
        )
        self.feed_candles(controller, make_candles(n=400, drift=0.0006),
                          make_candles(n=200, sigma=0.005, interval_s=900))
        await controller.update_processed_data()
        self.assertEqual(controller.processed_data["entry_gate"], "none")
        self.assertEqual(controller.processed_data["signal"], 0)
        self.assertTrue((controller.processed_data["features"]["signal"] == 0).all())

    async def test_hot_fast_frame_alone_does_not_halt(self):
        """ADX(3m) above the extreme band is routine in crypto. Only the slow
        frame is the risk-off filter; a hot fast frame merely fails to confirm,
        so a trending slow frame must still be able to open."""
        controller = self.build_controller(
            # These fixtures measure ADX 100 (fast) and ADX 37 (slow), so this
            # band puts the fast frame in EXTREME and the slow frame in TRENDING.
            self.build_config(adx_trending_threshold=1.0, adx_extreme_threshold=60.0)
        )
        fast = make_candles(n=400, drift=0.0025, sigma=0.001)   # very strong ADX
        slow = make_candles(n=200, drift=0.0004, sigma=0.006, interval_s=900)
        self.feed_candles(controller, fast, slow)
        await controller.update_processed_data()
        self.assertEqual(controller.processed_data["fast_regime"], MarketRegime.EXTREME)
        self.assertEqual(controller.processed_data["slow_regime"], MarketRegime.TRENDING)
        self.assertEqual(controller.processed_data["entry_gate"], "slow")

    async def test_extreme_slow_regime_halts_entries(self):
        controller = self.build_controller(
            self.build_config(adx_trending_threshold=1.0, adx_extreme_threshold=2.0,
                              allow_fast_regime_entry=True)
        )
        self.feed_candles(controller, make_candles(n=400, drift=0.0006),
                          make_candles(n=200, sigma=0.005, interval_s=900))
        await controller.update_processed_data()
        self.assertEqual(controller.processed_data["regime"], MarketRegime.EXTREME)
        self.assertEqual(controller.processed_data["entry_gate"], "halt")
        self.assertEqual(controller.processed_data["signal"], 0)

    def test_adx_regime_reports_neutral_before_warmup(self):
        regime, adx, di = self.controller._compute_adx_regime(make_candles(n=20))
        self.assertEqual(regime, MarketRegime.RANGING)
        self.assertEqual(di, 0.0)

    def test_funding_bias_tilts_against_crowded_positioning(self):
        """Crowded longs paying premium tilt the score bearish, and vice versa.
        The response saturates toward the configured strength rather than
        reaching it exactly."""
        strength = self.config.funding_bias_strength

        self.controller.market_data_provider.get_funding_info.return_value = SimpleNamespace(
            rate=Decimal("0.002")  # 4x threshold — deep in crowded-long territory
        )
        bias = self.controller._get_funding_bias()
        self.assertLess(bias, 0.0)
        self.assertAlmostEqual(bias, -strength, places=3)
        self.assertGreater(bias, -strength)

        self.controller.market_data_provider.get_funding_info.return_value = SimpleNamespace(
            rate=Decimal("-0.002")
        )
        bias = self.controller._get_funding_bias()
        self.assertGreater(bias, 0.0)
        self.assertAlmostEqual(bias, strength, places=3)
        self.assertLess(bias, strength)

    def test_funding_bias_is_continuous_not_stepped(self):
        """A three-state tilt snaps as the rate crosses the threshold. The
        response should build smoothly and saturate."""
        biases = []
        for rate in ("0.0", "0.00025", "0.0005", "0.001", "0.01"):
            self.controller.market_data_provider.get_funding_info.return_value = (
                SimpleNamespace(rate=Decimal(rate))
            )
            biases.append(self.controller._get_funding_bias())

        self.assertAlmostEqual(biases[0], 0.0)
        # Strictly increasing crowding penalty, no plateau between steps
        for earlier, later in zip(biases, biases[1:]):
            self.assertLess(later, earlier)
        # Saturates at the configured strength rather than overshooting it
        self.assertGreater(biases[-1], -self.config.funding_bias_strength - 1e-9)
        self.assertAlmostEqual(biases[-1], -self.config.funding_bias_strength, places=3)

    def test_funding_bias_is_symmetric(self):
        def bias_at(rate):
            self.controller.market_data_provider.get_funding_info.return_value = (
                SimpleNamespace(rate=Decimal(rate))
            )
            return self.controller._get_funding_bias()

        self.assertAlmostEqual(bias_at("0.0008"), -bias_at("-0.0008"))

    def test_funding_bias_is_skipped_on_spot(self):
        controller = self.build_controller(self.build_config(connector_name="binance"))
        self.assertEqual(controller._get_funding_bias(), 0.0)

    # ── Self-adaptation ────────────────────────────────────────────────

    def test_closed_executors_are_counted_exactly_once(self):
        """executors_info is a live window over the orchestrator's active list;
        terminated executors are pruned from it, so counting by index drifts."""
        controller = self.controller
        controller.executors_info = [closed_executor("a", 1.0), closed_executor("b", -2.0)]
        controller._self_adapt()
        controller._self_adapt()  # same executors seen again
        self.assertEqual(controller.processed_data["closed_trades"], 2)
        self.assertAlmostEqual(controller.processed_data["session_pnl"], -1.0)

        # 'a' is pruned from the report and a new executor closes.
        controller.executors_info = [closed_executor("b", -2.0), closed_executor("c", 3.0)]
        controller._self_adapt()
        self.assertEqual(controller.processed_data["closed_trades"], 3)
        self.assertAlmostEqual(controller.processed_data["session_pnl"], 2.0)

    def test_shutting_down_executors_are_not_booked_yet(self):
        """An executor that has placed its close order has not booked the fill,
        so its PnL is still mark-to-market."""
        controller = self.controller
        controller.executors_info = [
            closed_executor("a", 5.0, is_done=False, close_type=None),
        ]
        controller._self_adapt()
        self.assertEqual(controller.processed_data["closed_trades"], 0)

        controller.executors_info = [closed_executor("a", 4.0)]
        controller._self_adapt()
        self.assertEqual(controller.processed_data["closed_trades"], 1)
        self.assertAlmostEqual(controller.processed_data["session_pnl"], 4.0)

    def adapt_over_time(self, controller, outcomes, prefix):
        """Feed one closed executor per adaptation interval.

        _self_adapt() runs on every controller tick but only steps once every
        adapt_interval_seconds, so a test that holds the clock still gets
        exactly one step no matter how many times it calls the method. The
        executor timestamps follow the clock too, so the turnover governor
        stays out of the way and only the win-rate branch is under test.
        """
        dt = controller.config.adapt_interval_seconds
        mdp = controller.market_data_provider
        t = mdp.time.return_value
        for i, pnl in enumerate(outcomes):
            t += dt
            mdp.time.return_value = t
            controller.executors_info = [
                closed_executor(f"{prefix}-{i}", pnl, timestamp=t)
            ]
            controller._self_adapt()

    def test_losing_streak_tightens_then_recovers(self):
        """The threshold must track a target rather than ratchet, or a losing
        streak latches it above the reachable score range forever."""
        controller = self.build_controller(self.build_config(adapt_step=0.05))
        base = controller.config.signal_threshold

        self.adapt_over_time(controller, [-1.0] * 6, "loss")
        tightened = controller._adapted_threshold
        self.assertGreater(tightened, base)

        # Wins arrive; the threshold must be able to come back down.
        self.adapt_over_time(controller, [1.0] * 20, "win")
        self.assertLess(controller._adapted_threshold, tightened)

    def test_threshold_stays_inside_its_bounds(self):
        controller = self.build_controller(
            self.build_config(threshold_floor=0.2, threshold_ceiling=0.45, adapt_step=0.5)
        )
        self.adapt_over_time(controller, [-1.0] * 30, "loss")
        self.assertLessEqual(controller._adapted_threshold, 0.45)
        self.assertGreaterEqual(controller._adapted_threshold, 0.2)

    def test_stale_entries_relax_the_threshold_toward_the_floor(self):
        """Half the competition score is volume; a controller that has gone
        quiet has to re-engage on its own."""
        controller = self.build_controller(
            self.build_config(stale_entry_seconds=60, adapt_step=0.05, threshold_floor=0.1)
        )
        controller._adapted_threshold = 0.5
        controller.executors_info = [
            closed_executor("old", 1.0, timestamp=NOW - 10_000),
        ]
        for _ in range(10):
            controller._self_adapt()
        self.assertLess(controller._adapted_threshold, 0.5)

    def test_cold_start_staleness_measures_from_controller_start(self):
        """If the threshold is too high for day-one conditions nothing opens,
        and measuring staleness only from the last executor means the turnover
        governor never engages — the same latch, reached from the other side."""
        controller = self.controller
        controller.executors_info = []
        # First observation establishes the reference point.
        self.assertEqual(controller._seconds_since_last_entry(), 0.0)
        controller.market_data_provider.time.return_value = NOW + 3600
        self.assertAlmostEqual(controller._seconds_since_last_entry(), 3600.0)

    def test_cold_start_relaxes_the_threshold_without_any_trade(self):
        controller = self.build_controller(
            self.build_config(stale_entry_seconds=60, adapt_step=0.05,
                              threshold_floor=0.1, signal_threshold=0.5)
        )
        controller.executors_info = []
        controller._self_adapt()  # establishes the start reference
        t = NOW + 10_000
        for _ in range(10):
            t += controller.config.adapt_interval_seconds
            controller.market_data_provider.time.return_value = t
            controller._self_adapt()
        self.assertLess(controller._adapted_threshold, 0.5)

    # ── Emergency exit ─────────────────────────────────────────────────

    def test_emergency_exit_closes_a_bleeding_executor(self):
        """A MAKER ladder defers its own stop-loss until every level fills, so
        a partially filled ladder needs a controller-side backstop."""
        controller = self.build_controller(
            self.build_config(emergency_stop_loss_pct=Decimal("0.05"))
        )
        bleeding = active_executor("bleeding")
        bleeding.is_trading = True
        bleeding.net_pnl_pct = Decimal("-0.06")
        healthy = active_executor("healthy")
        healthy.is_trading = True
        healthy.net_pnl_pct = Decimal("-0.01")
        controller.executors_info = [bleeding, healthy]

        actions = controller.stop_actions_proposal()
        self.assertEqual([a.executor_id for a in actions], ["bleeding"])
        self.assertFalse(actions[0].keep_position)
        self.assertEqual(actions[0].controller_id, controller.config.id)

    def test_emergency_exit_ignores_executors_with_no_fills(self):
        controller = self.controller
        unfilled = active_executor("unfilled")
        unfilled.is_trading = False
        unfilled.net_pnl_pct = Decimal("-0.99")
        controller.executors_info = [unfilled]
        self.assertEqual(controller.stop_actions_proposal(), [])

    def test_emergency_exit_can_be_disabled(self):
        controller = self.build_controller(
            self.build_config(emergency_stop_loss_pct=None)
        )
        bleeding = active_executor("bleeding")
        bleeding.is_trading = True
        bleeding.net_pnl_pct = Decimal("-0.90")
        controller.executors_info = [bleeding]
        self.assertEqual(controller.stop_actions_proposal(), [])

    # ── Status ─────────────────────────────────────────────────────────

    def test_format_status_renders_without_data(self):
        rows = self.controller.to_format_status()
        self.assertTrue(any("FlowEdge" in row for row in rows))

    async def test_format_status_renders_after_a_tick(self):
        self.feed_candles(self.controller, make_candles(n=400, drift=0.0004),
                          make_candles(n=200, sigma=0.005, interval_s=900))
        await self.controller.update_processed_data()
        rows = self.controller.to_format_status()
        self.assertTrue(any("Take profit" in row for row in rows))
        self.assertTrue(any("DI bias" in row for row in rows))
