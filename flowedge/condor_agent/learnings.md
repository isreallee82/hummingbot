# Learnings

<!--
Deploys to trading_agents/flow_edge/learnings.md

This file is injected into EVERY tick prompt, so every line costs tokens on every
tick. Keep it under ~20 entries. An entry earns its place only if it will still be
true next session and would change what the agent does. Move anything that goes
stale to Retired Insights rather than deleting it, so the same lesson is not
re-learned from scratch.

Entries below dated 2026-08-06 were seeded from the controller rewrite and the
review of sessions 1-5 — they are not agent observations. Everything the agent
writes from here on is its own.
-->

## Market Observations

- [2026-08-06] XRP-USD perp funding has historically sat near zero (~0.0007%) on
  Hyperliquid. The funding term contributes almost nothing on this pair — read the
  live rate, but do not build a directional story on it. On pairs with real carry
  it matters much more.
- [2026-08-06] ADX on the 3m frame routinely exceeds 50 in crypto. That is a
  short-term impulse, not a crash. Only EXTREME on the 15m regime frame means
  risk-off, and the routine already encodes this — `entry_gate: halt` is the signal
  to stand down, not a high `fast_adx` reading.

## Execution Notes

- [2026-08-06] A take-profit tighter than the round-trip fee books a realised loss
  every time it fills. The executor still reports the close as TAKE_PROFIT, so a
  run can show a high win rate while losing money. Never lower `take_profit` below
  `min_take_profit` (0.15%) to chase fills.
- [2026-08-06] In `DCAMode.MAKER` the executor defers its own stop-loss until every
  ladder level has filled. A ladder that filled 1 of 3 has **no working stop** — it
  is governed by `time_limit` and by the -5% emergency exit only. Do not assume a
  configured `stop_loss` is protecting a partially filled position.
- [2026-08-06] The emergency exit takes priority over opening anything new. Closing
  a bleeding executor is always the first action of a tick, never the second.
- [2026-08-06] Do not hand-raise `signal_threshold` above ~0.60. The feature blend
  saturates near there, so a higher threshold can never be reached and the agent
  goes permanently quiet without any error appearing.
- [2026-08-06] When porting to a new pair or venue, retune `natr_baseline_pct` to
  that pair's typical NATR% and leave the signal weights alone. The score is
  volatility-normalised so it transfers unchanged; the barriers and spreads do not.
- [2026-08-06] If nothing has opened in the first 30 minutes of a session, check
  `entry_gate` before concluding the market is quiet. A closed gate and a genuinely
  flat market look identical from the P&L, and only one of them is worth waiting out.

## Operational Notes

- [2026-08-06] When the Hummingbot API is unreachable: journal it **once**, send
  **one** notification, then hold silently until it recovers. Session 5 wrote 155
  near-identical HOLD entries over ~2.5 hours — after the second entry every one of
  them was pure context bloat carrying no new information.
- [2026-08-06] A long run of zero-action ticks is worth one summary line at the end,
  not one line per tick. Journal state *changes*, not state.

## Retired Insights

- [retired 2026-08-06] May 2026 XRP-USD levels — $1.44 intraday support, $1.46 supply
  zone rejected on three attempts, $1.41-1.42 compression after the 5/16 dump. Three
  months stale; treating these as live levels would be worse than having no levels at
  all. Re-derive support and resistance from current candles.
- [retired 2026-08-06] "Hummingbot API URL malformed (`http://https://...`)" — this
  was a Condor server-config bug that blocked all of session 5. Fixed upstream:
  `config_manager._build_base_url` now parses a scheme embedded in the `host` field.
  If it reappears, the Condor checkout is older than that fix.
