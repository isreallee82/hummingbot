# FlowEdge — Regime-Adaptive Directional DCA

**Author:** Isreal Lee (@isreallee82)
**Entry form:** Hummingbot V2 Controller *and* Condor Agent (same signal, two runtimes)
**Venue:** Perpetuals — developed on Hyperliquid, venue-agnostic by construction

---

## Summary

FlowEdge takes directional entries only when a trend regime is confirmed on two
timeframes, sizes every entry and every exit barrier to live volatility, and adapts
its own entry threshold from its realised win rate and its turnover.

The core design commitment is that **every input is normalised into the same unit
before it is combined**. Most multi-factor retail strategies add a bounded oscillator
to a raw price return and call the weights a blend; in practice one term is 20x the
other and the weights are fiction. FlowEdge divides every price-space feature by live
NATR and squashes it with `tanh`, so the score means the same thing on a 0.15%/bar
major and a 0.6%/bar alt. Measured on synthetic candles, the |score| distribution
moves less than 1% between those two regimes.

---

## Signal

Four features, each mapped into `[-1, +1]`, then weighted:

| Feature | Definition | Weight | What it sees |
|---|---|---:|---|
| Candle Flow Imbalance | `(close-open)/(high-low)`, 5-bar mean | 0.35 | Within-bar buy/sell pressure |
| VWAP Extension | `tanh( ((close-vwap)/vwap) / NATR / 2 )` | 0.25 | Stretch from fair value, in volatility units |
| Trend | `tanh( ((close-EMA21)/ATR) / 2 )` | 0.25 | Cross-bar persistence |
| DI Bias | `(DI+ − DI−)/(DI+ + DI−)` on the regime frame | 0.15 | Direction of the confirmed trend |

The VWAP is a **rolling** window, not an anchored cumulative sum, so the lookback is
identical on every bar and the reference does not drift as the candle buffer ages.

Two adjustments are applied after the blend:

- **RSI dampener.** Proportional, not binary. RSI 72 on a long trims conviction
  slightly; RSI 90 erases it. A conventional hard veto throws away a strong signal
  for being one point over an arbitrary line.
- **Funding bias.** On perpetuals the score is tilted by
  `-strength * tanh(rate / threshold)` — a continuous crowding penalty that builds
  smoothly and saturates toward ∓0.15 rather than snapping as the rate crosses the
  threshold. Crowded longs paying premium is a real, cheap contrarian input that most
  retail bots ignore entirely.

### Regime gate

ADX(14) is computed independently on the fast (3m) and slow (15m) frames and each is
classified `RANGING` / `TRENDING` / `EXTREME`. Entries require at least one frame to
confirm `TRENDING`.

The two frames are not treated symmetrically, and this matters. The **slow frame is
the risk-off filter**: `EXTREME` there means a crash or a parabolic squeeze, and a
maker DCA ladder is the wrong instrument for both, so it halts entries outright.
`EXTREME` on the **fast** frame is just a strong short-term impulse — ADX(3m) above 50
is routine in crypto — so it merely fails to confirm rather than halting the strategy.
Treating both frames as hard halts would sit out a large share of ordinary trending
markets.

ADX measures trend *strength* and carries no direction, so the directional indicators
from the same computation supply the `DI Bias` term rather than being discarded.

---

## Execution

Entries are 3-level **maker** DCA ladders (`DCAExecutorConfig`, `DCAMode.MAKER`) —
limit orders, weighted 50/30/20 toward the near level.

Every distance scales with one dimensionless multiplier:

```
multiplier = clamp(live_NATR% / natr_baseline_pct, 0.6, 2.5)
```

`natr_baseline_pct` (default 0.35%) is the volatility at which the configured spreads
and barriers apply exactly as written. This is the parameter to retune per venue and
per pair — nothing else needs to move.

The multiplier scales the DCA spreads, the stop-loss, the take-profit **and** the
trailing stop together. Scaling only some of them is a silent failure mode: if the
take-profit shrinks in calm markets but the trailing stop does not, the take-profit
always fires first and the trailing stop never activates.

Take-profit is then floored at `min_take_profit` (default 0.15%). A take-profit
tighter than the round-trip fee books a realised loss every single time it fills —
a winning-looking exit that loses money.

---

## Self-Adaptation

FlowEdge tracks its last 20 closed executors and steers `signal_threshold` toward a
target each tick:

- Rolling win rate below 40% → target the ceiling (trade less, demand more conviction).
- Rolling win rate above 65% → target the floor (trade more).
- Nothing opened for 30 minutes → target the floor regardless of win rate. Staleness
  falls back to the controller's own age when nothing has *ever* opened, so a
  threshold too high for day-one conditions still relaxes.

Two properties matter more than the rule itself:

1. **It tracks a target; it does not ratchet.** A one-way ratchet can raise the
   threshold above the score range the features can actually produce, at which point
   no entry ever fires, no trade ever closes, the win-rate window never updates, and
   the loosening branch is unreachable. That is a permanent, silent halt. Tracking a
   target makes every state recoverable.
2. **Closed executors are keyed by id, not counted by index.** The controller's view
   of its executors is a live window over the orchestrator's active list — terminated
   executors are pruned out of it. Any index arithmetic against that list
   double-counts some trades and silently drops others.

The adapted threshold is held separately from the config, so the config is never
mutated and hot-reload stays safe. The status panel shows both: `0.42 (base 0.30)`.

---

## Intended Market Conditions

**Works in:** liquid perpetuals with genuine intraday trends — majors and
high-volume alts. Best when ADX(15m) sits in the 22–50 band.

**Deliberately sits out:** chop (ADX below the trending threshold), and crashes or
parabolic squeezes (ADX above 50). Suppressed entries are the strategy working, not
the strategy stalling.

**Weakest in:** low-volume pairs where the VWAP term is noisy, and in fast regime
flips where a maker ladder can fill on one side of a reversal. The `time_limit`
barrier is the backstop for the latter.

---

## Parameters

| Parameter | Default | Notes |
|---|---:|---|
| `fast_interval` / `slow_interval` | 3m / 15m | Signal frame / regime frame |
| `signal_threshold` | 0.30 | Fires on ~37% of bars pre-gate; the gate and cooldown do the rest |
| `adx_trending_threshold` | 22.0 | Entry gate opens |
| `adx_extreme_threshold` | 50.0 | Hard halt |
| `cfi/vwap/trend/di_weight` | 0.35/0.25/0.25/0.15 | Sum to 1.0 |
| `natr_baseline_pct` | 0.35 | **Primary retuning knob per pair/venue** |
| `vol_multiplier_min/max` | 0.6 / 2.5 | Clamp on volatility scaling |
| `stop_loss` / `take_profit` | 0.02 / 0.006 | At baseline volatility |
| `min_take_profit` | 0.0015 | Round-trip fee floor |
| `emergency_stop_loss_pct` | 0.05 | Controller-side backstop for MAKER's deferred stop-loss |
| `time_limit` | 1800s | Third barrier |
| `dca_spreads` / `dca_amounts_pct` | 0.002,0.005,0.01 / 0.5,0.3,0.2 | Ladder shape |
| `threshold_floor` / `ceiling` | 0.15 / 0.60 | Adaptation bounds |
| `cooldown_time` | 180s | Minimum gap between same-side entries |
| `max_executors_per_side` | 2 | Capacity |

---

## Two Runtimes, One Signal

The same signal ships in both forms the rules accept:

- **Controller** — `controllers/directional_trading/flow_edge.py`. Runs natively
  inside Hummingbot with no LLM in the loop and no external services.
- **Condor Agent** — `flowedge/condor_agent/`. A deterministic routine computes the
  identical signal; the LLM handles what the controller cannot — leftover positions
  after a `keep_position` close, cross-tick context, and journalled reasoning.

The agent's routine implements ATR, NATR, RSI, EMA and ADX/DI directly rather than
importing a TA library, and is verified to agree with the controller's `pandas_ta`
computations to floating-point precision (max observed difference 1.4e-14). The two
runtimes cannot silently disagree about what the market is doing.

---

## Known Limitations

- The volatility multiplier is clamped at 2.5x, and barriers are fixed when an
  executor is created — they are never rescaled afterwards. A move far larger than
  the one a ladder was sized for is therefore caught by the emergency exit and
  `time_limit` rather than by the ladder's own barriers.
- In `DCAMode.MAKER` the executor defers its stop-loss until the whole ladder is
  filled — documented framework behaviour, not a bug, but it leaves a partially
  filled ladder with no working stop. The `emergency_stop_loss_pct` backstop below
  covers this; without it, `time_limit` is the only control.
- The funding bias saturates toward its configured strength but is still a
  single-rate tilt, not a term-structure or carry model.
- Self-adaptation needs at least 4 closed trades before the win-rate branch acts.
  The turnover branch works from the first tick, so the threshold still relaxes if
  nothing opens, but early behaviour is driven by turnover rather than performance.

### Fixes applied to the above

Three of these were closed rather than merely documented:

**Emergency exit (`stop_actions_proposal`).** The controller watches the PnL each
active executor reports and stops anything past `emergency_stop_loss_pct` (default
5%), filled or not. This closes the deferred-stop-loss gap in MAKER mode and is also
what catches a volatility explosion, since barriers set at creation cannot be
rescaled. Executors with no fills are ignored.

**Continuous funding bias.** Now `-strength * tanh(rate / threshold)` instead of a
three-state ±0.15 tilt. Crowding builds smoothly and saturates rather than snapping
as the rate crosses the threshold — a rate at the threshold gives ~76% of full
strength, at twice the threshold ~96%.

**Cold-start turnover governor.** Staleness now falls back to the age of the
controller when nothing has ever opened. Measuring only from the last executor left
a hole: a threshold too high for day-one conditions means nothing opens, and because
nothing ever opened the turnover governor never engaged — the same silent latch the
adaptive layer exists to prevent, reached from the other side.
