# FlowEdge — Botcamp Strategy Submission

Copy each block below into the matching field on the Botcamp strategy form.
Fields marked **TODO** need something from you that I cannot generate.

---

## Summary *

> *A short summary shown on strategy cards (required)*

An evolution of the Cohort 13 FlowEdge controller, rebuilt around one fix: every
signal input is now normalised into the same volatility unit before it is combined, so
the feature weights are real and a single threshold holds across pairs and regimes —
the original blend added a bounded score to a raw price fraction and could not reach
its own threshold. Adds a trend term and ADX directional bias, rolling VWAP, a
fee-floored take-profit, barriers that all scale together with live NATR, and a
self-adaptive threshold that tracks a target instead of ratcheting, so it can no longer
latch itself out of trading. Now ships in both forms the competition accepts: a
Hummingbot V2 controller and a Condor agent whose deterministic routine computes a
verified-identical signal, with the LLM handling leftover positions and cross-tick
context the controller cannot see.

---

## Detailed Description *

### Description

FlowEdge takes directional entries only when a trend regime is confirmed, sizes every
entry and exit to live volatility, and adapts its own threshold from its own results.

**The core commitment: every input is normalised before it is combined.**

Most multi-factor retail strategies add a bounded oscillator to a raw price return and
call the weights a blend. In practice one term is 20x the other and the weights are
fiction. FlowEdge divides every price-space feature by live NATR and squashes it with
`tanh`, so the score means the same thing on a 0.15%/bar major and a 0.6%/bar alt.
Measured on synthetic candles, the |score| distribution moves less than 1% between
those two regimes — one threshold works across pairs and venues.

**Signal — four features, each mapped into [-1, +1], then weighted:**

| Feature | Definition | Weight | What it sees |
| --- | --- | ---: | --- |
| Candle Flow Imbalance | `(close-open)/(high-low)`, 5-bar mean | 0.35 | Within-bar buy/sell pressure |
| VWAP Extension | `tanh( ((close-vwap)/vwap) / NATR / 2 )` | 0.25 | Stretch from fair value, in volatility units |
| Trend | `tanh( ((close-EMA21)/ATR) / 2 )` | 0.25 | Cross-bar persistence |
| DI Bias | `(DI+ − DI−)/(DI+ + DI−)` on the regime frame | 0.15 | Direction of the confirmed trend |

The VWAP is a **rolling** window, not an anchored cumulative sum, so the lookback is
identical on every bar and the reference does not drift as the candle buffer ages.

Two adjustments follow the blend:

- **RSI dampener** — proportional, not binary. RSI 72 on a long trims conviction
  slightly; RSI 90 erases it. A hard veto throws away a strong signal for being one
  point over an arbitrary line.
- **Funding bias** — on perpetuals the score is tilted by
  `-strength * tanh(rate / threshold)`, a continuous crowding penalty that saturates
  toward ∓0.15 instead of snapping at the threshold. Crowded longs paying premium is
  a real, cheap contrarian input most bots ignore.

**Regime gate.** ADX(14) is computed independently on the fast (3m) and slow (15m)
frames, each classified `RANGING` / `TRENDING` / `EXTREME`. Entries need at least one
frame confirming `TRENDING`. The frames are deliberately asymmetric: the **slow frame
is the risk-off filter** — `EXTREME` there means a crash or parabolic squeeze, and a
maker DCA ladder is the wrong instrument for both, so it halts entries. `EXTREME` on
the **fast** frame is just a strong short-term impulse (ADX(3m) above 50 is routine in
crypto), so it merely fails to confirm. Treating both as hard halts would sit out a
large share of ordinary trending markets.

ADX measures trend *strength* and carries no direction, so the directional indicators
from the same computation supply the DI Bias term rather than being discarded.

**Execution.** Entries are 3-level maker DCA ladders weighted 50/30/20 toward the near
level. Every distance scales with one dimensionless multiplier:

```text
multiplier = clamp(live_NATR% / natr_baseline_pct, 0.6, 2.5)
```

The multiplier scales DCA spreads, stop-loss, take-profit **and** the trailing stop
together. Scaling only some is a silent failure: if take-profit shrinks in calm
markets but the trailing stop does not, take-profit always fires first and the
trailing stop never activates. Take-profit is then floored at `min_take_profit`
(0.15%) — a take-profit tighter than the round-trip fee books a realised loss every
time it fills, a winning-looking exit that loses money.

**Self-adaptation.** FlowEdge tracks its last 20 closed executors and steers
`signal_threshold` toward a target each tick: win rate under 40% targets the ceiling,
over 65% targets the floor, and nothing opened for 30 minutes targets the floor
regardless — measured from the controller's own age when nothing has ever opened, so
a threshold too high for day-one conditions still relaxes. Two properties matter more
than the rule:

1. **It tracks a target; it does not ratchet.** A one-way ratchet can raise the
   threshold above the range the features can actually produce, at which point no
   entry fires, no trade closes, the win-rate window never updates, and the loosening
   branch is unreachable — a permanent, silent halt. Tracking a target makes every
   state recoverable.
2. **Closed executors are keyed by id, not counted by index.** The controller's view
   of its executors is a live window over the orchestrator's active list; terminated
   executors are pruned out of it, so index arithmetic double-counts some trades and
   silently drops others.

The adapted threshold is held separately from the config, so the config is never
mutated and hot-reload stays safe. The status panel shows both: `0.42 (base 0.30)`.

**Two runtimes, one signal — enforced by a test, not by discipline.** The Condor
agent's routine implements ATR, NATR, RSI, EMA and ADX/DI directly rather than
importing a TA library. A parity suite drives both runtimes over identical candles
across five volatility regimes and asserts that the blended score, every feature
component, both regimes, the entry gate, the direction, the volatility multiplier,
the ladder prices and sizes and all three barriers agree — plus a guard that every
parameter both sides expose defaults to the same value. The hand-rolled indicators
match `pandas_ta` to 1e-6.

Writing it was worth more than the guarantee. It found three real defects that had
been live: the routine computed on the **forming candle** while the controller
deliberately drops it (early in a bar, close is pinned to the high or low, so CFI is
mechanically ±1 and the bar's tiny range deflates NATR — the agent was trading a
reading that repaints seconds later); the two loaded **different amounts of candle
history**, and Wilder's RMA never forgets, so the same bar computed differently on
each side; and the routine had no `allow_fast_regime_entry` flag, so it opened gates
the controller kept shut.

### Markets

**Designed for:** liquid perpetuals with genuine intraday trends — majors and
high-volume alts. Best when ADX(15m) sits in the 22–50 band. Developed and tested on
Hyperliquid perpetuals (XRP-USD), but nothing in the signal is venue-specific.

**Deliberately sits out:** chop (ADX below the trending threshold), and crashes or
parabolic squeezes (ADX above 50 on the regime frame). Suppressed entries are the
strategy working, not the strategy stalling.

**Weakest in:** low-volume pairs where the VWAP term is noisy, and in fast regime
flips where a maker ladder can fill on one side of a reversal. The `time_limit`
barrier is the backstop for the latter.

**Porting to a new pair or venue:** set `natr_baseline_pct` to that pair's typical
NATR% on the signal timeframe. Every spread and barrier follows from it. Because the
score itself is volatility-normalised, the signal parameters do not need retuning.

### Parameters

| Parameter | Default | Notes |
| --- | ---: | --- |
| `fast_interval` / `slow_interval` | 3m / 15m | Signal frame / regime frame |
| `signal_threshold` | 0.30 | Fires on ~37% of bars pre-gate; gate and cooldown do the rest |
| `adx_trending_threshold` | 22.0 | Entry gate opens |
| `adx_extreme_threshold` | 50.0 | Hard halt on the slow frame |
| `cfi_weight` / `vwap_weight` / `trend_weight` / `di_weight` | 0.35 / 0.25 / 0.25 / 0.15 | Sum to 1.0 |
| `vwap_window` | 24 | Rolling VWAP lookback in bars |
| `trend_ema_length` | 21 | EMA for the trend term |
| `natr_baseline_pct` | 0.35 | **Primary retuning knob per pair/venue** |
| `vol_multiplier_min` / `max` | 0.6 / 2.5 | Clamp on volatility scaling |
| `stop_loss` / `take_profit` | 0.02 / 0.006 | At baseline volatility |
| `min_take_profit` | 0.0015 | Round-trip fee floor |
| `emergency_stop_loss_pct` | 0.05 | Controller-side backstop for MAKER's deferred stop-loss |
| `time_limit` | 1800s | Third barrier |
| `dca_spreads` | 0.002, 0.005, 0.01 | Ladder depth |
| `dca_amounts_pct` | 0.5, 0.3, 0.2 | Ladder weighting |
| `trailing_stop` | 0.004, 0.0015 | Activation, delta — both volatility-scaled |
| `rsi_length` / `overbought` / `oversold` | 14 / 70 / 30 | Dampener band |
| `adapt_window` / `adapt_min_samples` | 20 / 4 | Rolling performance window |
| `adapt_win_rate_low` / `high` | 0.40 / 0.65 | Tighten / loosen triggers |
| `adapt_step` | 0.02 | Max threshold move per tick |
| `threshold_floor` / `ceiling` | 0.15 / 0.60 | Adaptation bounds |
| `stale_entry_seconds` | 1800 | Turnover governor trigger |
| `funding_threshold` / `funding_bias_strength` | 0.0005 / 0.15 | Crowding tilt |
| `cooldown_time` | 180s | Minimum gap between same-side entries |
| `max_executors_per_side` | 2 | Capacity |
| `leverage` | 2 | Conservative by default |

### Status

**Current state:** implemented, unit-tested, and calibrated — not yet live-validated
in its current form.

- 49 unit tests pass — 39 against the controller, covering config validation, volatility
  scaling, barrier construction, the regime gate, the RSI dampener, capacity and
  cooldown gating, and every self-adaptation invariant. There were previously no tests
  for any repo-root Hummingbot controller.
- Signal distribution calibrated on synthetic candles across four volatility regimes;
  scale-invariance verified (|score| distribution within 1% between 0.15%/bar and
  0.6%/bar).
- 10 cross-runtime parity tests assert the controller and the Condor routine compute the same signal, ladder and barriers from the same candles.
- An earlier revision ran live sessions on Hyperliquid XRP-USD. That revision had two
  defects since fixed: the signal could not reach its own threshold, and the adaptive
  layer could latch permanently into not trading.

**Limitations that were closed rather than documented:**

- **MAKER's deferred stop-loss.** `DCAExecutor` gates its own stop-loss behind
  "all levels filled", leaving a partially filled ladder with no working stop —
  documented framework behaviour, not a bug, but a real exposure. A controller-side
  `stop_actions_proposal` now watches the PnL each active executor reports and closes
  anything past `emergency_stop_loss_pct` (default 5%), filled or not. This is also
  what catches a volatility explosion, since barriers are fixed at executor creation
  and cannot be rescaled afterwards.
- **Stepped funding bias.** Replaced the three-state ±0.15 tilt with
  `-strength * tanh(rate / threshold)` — continuous, saturating, no cliff at the
  threshold.
- **Cold-start latch.** Turnover staleness now falls back to the controller's own age
  when nothing has ever opened. Measuring only from the last executor left a hole: a
  threshold too high on day one means nothing opens, and because nothing ever opened
  the turnover governor never engaged — the same silent latch the adaptive layer
  exists to prevent, reached from the other side.

**Limitations that remain, stated plainly:**

- The volatility multiplier is clamped at 2.5x and barriers are never rescaled after
  an executor is created, so an outsized move is caught by the emergency exit and
  `time_limit` rather than by the ladder's own barriers.
- The funding bias is still a single-rate tilt, not a term-structure or carry model.
- Self-adaptation needs 4 closed trades before the win-rate branch acts. The turnover
  branch works from the first tick, so early behaviour is driven by turnover rather
  than performance.
- Calibration is synthetic. A live paper run on the target pair is the remaining
  validation step.

### Events

- **Agent Builders Cup · Series 1** (2026) — current submission, as both a Hummingbot
  V2 Controller and a Condor Agent.
- **Botcamp Cohort 13 Demo Day** (2026) — earlier revision, published at
  `botcamp.xyz/strategies/cohort-13-flowedge`.

---

## Flowchart & Images

> *Save your strategy first to upload images*

Three diagrams are built and ready to upload from `flowedge/diagrams/` (PNG at 2x for
upload, SVG alongside for anything that accepts vector):

| File | What it shows |
| --- | --- |
| `01-signal-pipeline.png` | The full tick: three price features divided by live NATR and squashed into a common range **before** they are weighted, the regime branch supplying the gate and DI bias, then dampener, threshold, ladder and emergency exit. |
| `02-regime-gate.png` | Why the two timeframes are not symmetric — the slow frame halts on EXTREME and bypasses the gate entirely; the fast frame merely fails to confirm. |
| `03-adaptation.png` | The threshold trajectory under the current target-tracking rule against the ratcheting rule it replaced, over an identical trade sequence. |

Diagram 3 is worth leading with. It is not drawn by hand — `build_diagrams.py` runs the
real `FlowEdgeProController` adaptation code and plots the result, so it cannot drift
away from the implementation. Both rules receive a trade outcome only while their own
threshold still permits an entry to fire, which is the feedback that makes the ratchet
fail: it climbs to 0.75, passes the range the features can actually produce, and
flat-lines there forever because no trade ever closes again to update its window. The
current rule tightens to 0.56 under the same losses and recovers to the floor.

Regenerate any time the code changes:

```bash
python3 flowedge/diagrams/build_diagrams.py
cd flowedge/diagrams && for f in *.svg; do rsvg-convert -z 2 "$f" -o "${f%.svg}.png"; done
```

---

## Code Files *

> *Save your strategy first to upload files*

| File | What it is |
| --- | --- |
| `controllers/directional_trading/flow_edge.py` | The Hummingbot V2 controller |
| `flowedge/conf/conf_directional_trading.flow_edge_2.yml` | Controller configuration |
| `test/controllers/directional_trading/test_flow_edge.py` | 39 controller unit tests |
| `test/controllers/directional_trading/test_flow_edge_parity.py` | 10 controller-vs-agent parity tests |
| `agents/flow_edge/AGENT.md` | Condor agent identity (in the Condor repo) |
| `agents/flow_edge/strategies/flowedge_dca/strategy.md` | Condor tick playbook |
| `agents/flow_edge/routines/flow_edge_signal.py` | Deterministic signal routine |
| `flowedge/strategy.md` | Strategy description (Rule 05 deliverable) |
| `flowedge/README.md` | Install and run instructions for both runtimes |

---

## Video Link *

> *YouTube, Vimeo, Google Drive, or Loom link (required)*

**TODO — not yet recorded.** Suggested ~5 minute structure:

1. **The problem (45s).** Show the old signal arithmetic: a bounded score added to a
   raw price fraction, the 0.40 weight contributing 4% of magnitude, and the threshold
   the strategy could not reach. This is a concrete, verifiable failure most strategies
   share and almost nobody demonstrates.
2. **The fix (90s).** Volatility normalisation, and the chart showing the score
   distribution barely moving between calm and volatile markets.
3. **Execution (60s).** Ladder and barriers scaling together; the fee floor on
   take-profit.
4. **Self-adaptation (60s).** Target-tracking vs ratcheting, and why a ratchet is a
   permanent halt.
5. **Running (60s).** Live status panel, then the Condor agent tick producing the same
   decision.
6. **Close (15s).** Two runtimes, one signal — and a parity suite that fails the
   build if they ever drift apart.

---

## Tags & Exchanges

### Tags

```text
Controller, Condor Agent, Directional, DCA, Perpetuals, Regime Detection,
Multi-Timeframe, Volatility Scaling, Self-Adaptive, ADX, VWAP, Funding Rate,
Agent Builders Cup
```

### Exchanges

```text
Hyperliquid
```

Developed and tested on Hyperliquid perpetuals. The strategy is venue-agnostic — it
needs only candles and, optionally, a funding rate — so add any sponsor venue you
actually run it against before submitting. Add `Bitget` if you validate it there;
Bitget and Derive were the least-contested team seats, and your Bitget UTA connector
work is a direct fit for that team.
