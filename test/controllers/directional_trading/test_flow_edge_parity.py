"""
Cross-runtime parity: the Hummingbot controller and the Condor agent routine
must compute the SAME signal from the same candles.

FlowEdge ships as two independent implementations — `flow_edge.py` uses
pandas_ta inside a Hummingbot bot, and `flow_edge_signal.py` reimplements the
indicators in numpy inside a Condor routine. Nothing at runtime forces them to
agree, so the submission's "verified-identical signal" claim is only as good as
this test. Change a weight in one and this fails.

The routine lives in the Condor repo, outside this tree. Point FLOWEDGE_ROUTINE
at it, or keep the default checkout path; the whole module skips when absent so
this never breaks CI for people without Condor.
"""

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd

from controllers.directional_trading.flow_edge import FlowEdgeProConfig, FlowEdgeProController
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider

ROUTINE_PATH = os.path.expanduser(
    os.environ.get(
        "FLOWEDGE_ROUTINE",
        "~/condor-1/agents/flow_edge/routines/flow_edge_signal.py",
    )
)
NOW = 1_700_000_000.0


def _load_routine(path):
    """Import the Condor routine with its Telegram/Condor deps stubbed out."""
    if not os.path.exists(path):
        return None
    if "telegram" not in sys.modules:
        tg = types.ModuleType("telegram")
        ext = types.ModuleType("telegram.ext")
        ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
        tg.ext = ext
        sys.modules["telegram"], sys.modules["telegram.ext"] = tg, ext
    if "config_manager" not in sys.modules:
        cm = types.ModuleType("config_manager")
        cm.get_client = None
        sys.modules["config_manager"] = cm
    spec = importlib.util.spec_from_file_location("flow_edge_signal_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROUTINE = _load_routine(ROUTINE_PATH)


def candles(n=400, seed=7, sigma=0.0025, drift=0.0, start=100.0, interval_s=180):
    """Synthetic OHLCV with real intrabar range, so CFI/ATR/ADX are meaningful."""
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


# Market shapes chosen to exercise every gate branch and both signal directions.
SCENARIOS = [
    ("calm_chop", dict(seed=7, sigma=0.0012, drift=0.0)),
    ("trend_up", dict(seed=3, sigma=0.0025, drift=0.0009)),
    ("trend_down", dict(seed=11, sigma=0.0025, drift=-0.0009)),
    ("volatile", dict(seed=19, sigma=0.0075, drift=0.0004)),
    ("violent", dict(seed=23, sigma=0.02, drift=0.003)),
]
FUNDING_RATES = [None, 0.0, 0.0004, 0.0021, -0.0013]


@unittest.skipUnless(
    ROUTINE is not None,
    f"Condor routine not found at {ROUTINE_PATH}; set FLOWEDGE_ROUTINE to run parity tests",
)
class FlowEdgeParityTests(IsolatedAsyncioWrapperTestCase):
    """Every assertion here compares controller output against routine output."""

    @staticmethod
    def build_pair(allow_fast=True, **overrides):
        """A controller config and a routine config holding identical settings."""
        shared = dict(
            fast_interval="3m",
            slow_interval="15m",
            fast_max_records=150,
            slow_max_records=100,
            signal_threshold=0.30,
            adx_trending_threshold=22.0,
            adx_extreme_threshold=50.0,
            allow_fast_regime_entry=allow_fast,
            cfi_weight=0.35,
            vwap_weight=0.25,
            trend_weight=0.25,
            di_weight=0.15,
            vwap_window=24,
            trend_ema_length=21,
            rsi_length=14,
            rsi_overbought=70.0,
            rsi_oversold=30.0,
            natr_baseline_pct=0.35,
            vol_multiplier_min=0.6,
            vol_multiplier_max=2.5,
            funding_threshold=0.0005,
            funding_bias_strength=0.15,
            time_limit=1800,
        )
        shared.update(overrides)

        controller_config = FlowEdgeProConfig(
            id="parity",
            controller_name="flow_edge",
            connector_name="hyperliquid_perpetual",
            trading_pair="XRP-USD",
            candles_connector="hyperliquid_perpetual",
            candles_trading_pair="XRP-USD",
            total_amount_quote=Decimal("100"),
            leverage=2,
            stop_loss=Decimal("0.02"),
            take_profit=Decimal("0.006"),
            min_take_profit=Decimal("0.0015"),
            dca_spreads="0.002,0.005,0.01",
            dca_amounts_pct="0.5,0.3,0.2",
            **shared,
        )
        routine_config = ROUTINE.Config(
            connector_name="hyperliquid_perpetual",
            trading_pair="XRP-USD",
            total_amount_quote=100.0,
            stop_loss=0.02,
            take_profit=0.006,
            min_take_profit=0.0015,
            **shared,
        )
        return controller_config, routine_config

    @staticmethod
    def build_controller(config, fast, slow, funding_rate):
        mdp = MagicMock(spec=MarketDataProvider)
        mdp.time.return_value = NOW
        mdp.get_funding_info.return_value = (
            None if funding_rate is None else SimpleNamespace(rate=Decimal(str(funding_rate)))
        )

        def get_candles_df(connector_name, trading_pair, interval, max_records):
            src = fast if interval == config.fast_interval else slow
            return src.iloc[-max_records:].reset_index(drop=True)

        mdp.get_candles_df.side_effect = get_candles_df
        return FlowEdgeProController(
            config=config, market_data_provider=mdp, actions_queue=AsyncMock(spec=asyncio.Queue)
        )

    async def run_both(self, allow_fast=True, scenario=None, funding_rate=None, **overrides):
        """Drive both runtimes over identical candles; return (controller, routine)."""
        params = dict(scenario or {})
        fast = candles(interval_s=180, **params)
        slow = candles(n=200, interval_s=900, seed=params.get("seed", 7) + 100,
                       sigma=params.get("sigma", 0.0025) * 2,
                       drift=params.get("drift", 0.0) * 3)

        controller_config, routine_config = self.build_pair(allow_fast=allow_fast, **overrides)
        controller = self.build_controller(controller_config, fast, slow, funding_rate)
        await controller.update_processed_data()

        # Wilder's RMA has infinite memory, so both runtimes must see the same
        # slice of history — the controller trims to its max_records, and the
        # routine requests its own. Equal windows are part of parity.
        signal = ROUTINE._compute_signal(
            routine_config,
            fast.iloc[-routine_config.fast_max_records:].reset_index(drop=True),
            slow.iloc[-routine_config.slow_max_records:].reset_index(drop=True),
            funding_rate,
        )
        return controller, signal

    # ── Indicator primitives ───────────────────────────────────────────

    def test_indicator_primitives_match_pandas_ta(self):
        """The routine hand-rolls ATR/NATR/RSI/ADX; they must equal pandas_ta."""
        import pandas_ta as ta
        df = candles(n=300, seed=5)

        natr = ta.natr(df["high"], df["low"], df["close"], length=14)
        self.assertAlmostEqual(
            float(ROUTINE._natr_pct(df).iloc[-1]), float(natr.iloc[-1]), places=6
        )

        rsi = ta.rsi(df["close"], length=14)
        self.assertAlmostEqual(
            float(ROUTINE._rsi(df["close"], 14).iloc[-1]), float(rsi.iloc[-1]), places=6
        )

        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        adx, di_bias = ROUTINE._adx_di(df)
        self.assertAlmostEqual(adx, float(adx_df["ADX_14"].iloc[-1]), places=6)

        dmp, dmn = float(adx_df["DMP_14"].iloc[-1]), float(adx_df["DMN_14"].iloc[-1])
        self.assertAlmostEqual(di_bias, (dmp - dmn) / (dmp + dmn), places=6)

    # Deployment-specific — expected to differ between a controller config and
    # a routine config, so they are not part of parity.
    NOT_SHARED = {"connector_name", "trading_pair", "total_amount_quote",
                  "candles_connector", "candles_trading_pair", "controller_name", "id"}

    def test_shared_parameter_defaults_match(self):
        """Every knob both runtimes expose must default to the same value.

        The rest of this class passes identical settings to both configs, which
        proves the *math* agrees but says nothing about what each side does when
        nobody sets a value. In production nobody does — the controller reads its
        yml and the routine takes Config() defaults. A weight changed on one side
        only would sail through every other test here.
        """
        routine_defaults = ROUTINE.Config()
        shared = (set(FlowEdgeProConfig.model_fields)
                  & set(type(routine_defaults).model_fields)) - self.NOT_SHARED
        self.assertGreater(len(shared), 15, "parity surface unexpectedly small")

        mismatched = []
        for name in sorted(shared):
            controller_default = FlowEdgeProConfig.model_fields[name].default
            routine_default = getattr(routine_defaults, name)
            if controller_default is None or repr(controller_default) == "PydanticUndefined":
                continue
            try:
                same = float(controller_default) == float(routine_default)
            except (TypeError, ValueError):
                same = str(controller_default) == str(routine_default)
            if not same:
                mismatched.append(f"{name}: controller={controller_default!r} "
                                  f"routine={routine_default!r}")
        self.assertEqual([], mismatched, "defaults drifted between runtimes:\n  " +
                         "\n  ".join(mismatched))

    # ── Full signal parity ─────────────────────────────────────────────

    async def test_signal_parity_across_market_regimes(self):
        for name, scenario in SCENARIOS:
            with self.subTest(scenario=name):
                controller, routine = await self.run_both(scenario=scenario)
                data = controller.processed_data
                features = data["features"].iloc[-1]

                self.assertAlmostEqual(
                    data["signal_score"], routine["score"], places=3,
                    msg=f"{name}: blended score diverged")
                self.assertAlmostEqual(
                    float(features["cfi_smooth"]), routine["components"]["cfi"], places=3,
                    msg=f"{name}: candle-flow term diverged")
                self.assertAlmostEqual(
                    float(features["vwap_signal"]), routine["components"]["vwap"], places=3,
                    msg=f"{name}: VWAP term diverged")
                self.assertAlmostEqual(
                    float(features["trend_signal"]), routine["components"]["trend"], places=3,
                    msg=f"{name}: trend term diverged")
                # DI comes from pandas_ta on one side and a hand-rolled Wilder
                # RMA on the other. On the ~99-bar regime frame the two seedings
                # have not fully converged (~7e-4 apart); they agree to 1e-6 on a
                # long frame, which test_indicator_primitives_match_pandas_ta
                # pins down. A changed weight or formula moves this far more than
                # 0.01, so this still catches real drift.
                self.assertAlmostEqual(
                    data["di_bias"], routine["components"]["di"], places=2,
                    msg=f"{name}: DI bias diverged")
                self.assertAlmostEqual(
                    data["natr_pct"], routine["natr_pct"], places=3,
                    msg=f"{name}: NATR diverged")
                self.assertAlmostEqual(
                    data["rsi"], routine["rsi"], places=1,
                    msg=f"{name}: RSI diverged")

    async def test_regime_and_gate_parity(self):
        for name, scenario in SCENARIOS:
            with self.subTest(scenario=name):
                controller, routine = await self.run_both(scenario=scenario)
                data = controller.processed_data
                self.assertEqual(
                    str(data["slow_regime"].value).upper(), routine["slow_regime"],
                    msg=f"{name}: slow regime diverged")
                self.assertEqual(
                    str(data["fast_regime"].value).upper(), routine["fast_regime"],
                    msg=f"{name}: fast regime diverged")
                self.assertEqual(
                    data["entry_gate"], routine["entry_gate"],
                    msg=f"{name}: entry gate diverged")

    async def test_gate_parity_when_fast_frame_entries_are_disabled(self):
        """The routine originally had no allow_fast_regime_entry knob, so it
        opened gates the controller kept shut. Both settings must agree."""
        for allow_fast in (True, False):
            for name, scenario in SCENARIOS:
                with self.subTest(allow_fast=allow_fast, scenario=name):
                    controller, routine = await self.run_both(
                        allow_fast=allow_fast, scenario=scenario)
                    self.assertEqual(
                        controller.processed_data["entry_gate"], routine["entry_gate"],
                        msg=f"{name} (allow_fast={allow_fast}): entry gate diverged")

    async def test_funding_bias_parity_across_rates(self):
        for rate in FUNDING_RATES:
            with self.subTest(funding_rate=rate):
                controller, routine = await self.run_both(
                    scenario=dict(SCENARIOS[1][1]), funding_rate=rate)
                self.assertAlmostEqual(
                    controller.processed_data["funding_bias"],
                    routine["components"]["funding"],
                    places=3, msg=f"funding bias diverged at rate={rate}")

    async def test_direction_parity(self):
        """Whatever one runtime decides to do, the other must decide too."""
        expected = {"LONG": 1, "SHORT": -1, "HOLD": 0}
        for name, scenario in SCENARIOS:
            for rate in (None, 0.0021, -0.0013):
                with self.subTest(scenario=name, funding_rate=rate):
                    controller, routine = await self.run_both(
                        scenario=scenario, funding_rate=rate)
                    self.assertEqual(
                        controller.processed_data["signal"], expected[routine["direction"]],
                        msg=f"{name} @ funding={rate}: "
                            f"controller said {controller.processed_data['signal']}, "
                            f"routine said {routine['direction']}")

    # ── Executor parity ────────────────────────────────────────────────

    async def test_volatility_multiplier_parity(self):
        for name, scenario in SCENARIOS:
            with self.subTest(scenario=name):
                controller, routine = await self.run_both(scenario=scenario)
                self.assertAlmostEqual(
                    float(controller._get_volatility_multiplier()),
                    routine["vol_multiplier"],
                    places=2, msg=f"{name}: volatility multiplier diverged")

    async def test_ladder_and_barrier_parity(self):
        """The numbers actually sent to the exchange must match."""
        for name, scenario in SCENARIOS:
            controller, routine = await self.run_both(scenario=scenario)
            _, routine_config = self.build_pair()
            # Force a decision so a ladder is always built for comparison.
            for direction, trade_type in (("LONG", TradeType.BUY), ("SHORT", TradeType.SELL)):
                with self.subTest(scenario=name, direction=direction):
                    forced = dict(routine, direction=direction)
                    ladder = ROUTINE._build_ladder(routine_config, forced)
                    price = Decimal(str(forced["price"]))
                    executor = controller.get_executor_config(
                        trade_type, price, Decimal("100") / price
                    )

                    self.assertEqual(len(executor.prices), len(ladder["prices"]))
                    for got, want in zip(executor.prices, ladder["prices"]):
                        self.assertAlmostEqual(
                            float(got) / float(want), 1.0, places=4,
                            msg=f"{name}/{direction}: ladder price diverged")
                    for got, want in zip(executor.amounts_quote, ladder["amounts_quote"]):
                        self.assertAlmostEqual(
                            float(got), float(want), places=3,
                            msg=f"{name}/{direction}: ladder size diverged")
                    self.assertAlmostEqual(
                        float(executor.stop_loss), ladder["stop_loss"], places=4,
                        msg=f"{name}/{direction}: stop loss diverged")
                    self.assertAlmostEqual(
                        float(executor.take_profit), ladder["take_profit"], places=4,
                        msg=f"{name}/{direction}: take profit diverged")
                    self.assertEqual(executor.time_limit, ladder["time_limit"])
                    self.assertEqual(
                        1 if executor.side == TradeType.BUY else 2, ladder["side"])

    async def test_take_profit_fee_floor_holds_in_both_runtimes(self):
        """A take-profit under the round-trip fee books a loss on every fill."""
        controller, routine = await self.run_both(
            scenario=dict(seed=7, sigma=0.00005, drift=0.0))  # near-zero volatility
        _, routine_config = self.build_pair()
        ladder = ROUTINE._build_ladder(routine_config, dict(routine, direction="LONG"))
        price = Decimal(str(routine["price"]))
        executor = controller.get_executor_config(TradeType.BUY, price, Decimal("100") / price)

        self.assertGreaterEqual(ladder["take_profit"], float(routine_config.min_take_profit))
        self.assertGreaterEqual(executor.take_profit, controller.config.min_take_profit)
        self.assertAlmostEqual(float(executor.take_profit), ladder["take_profit"], places=6)
