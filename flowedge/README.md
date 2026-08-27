# FlowEdge — Submission Bundle

Regime-adaptive directional DCA, shipped in both forms the Agent Builders Cup accepts.
See [strategy.md](strategy.md) for the strategy description, parameters and intended
market conditions.

## What is where

| Path | What it is |
|---|---|
| `../controllers/directional_trading/flow_edge.py` | The Hummingbot V2 controller |
| `conf/conf_directional_trading.flow_edge_2.yml` | Controller config (`conf/` is gitignored, so the shipping copy lives here — copy it to `conf/controllers/` to run) |
| `../test/controllers/directional_trading/test_flow_edge.py` | 32 unit tests |
| `strategy.md` | Strategy description (submission deliverable) |

The Condor agent lives in the Condor repo, not here — see *Condor agent* below.
| `diagrams/` | Submission diagrams (SVG + PNG) and the generator that builds them |

## Running the controller

```bash
# from the hummingbot root
./start
# then, in the Hummingbot CLI:
start --script v2_with_controllers.py --conf conf_v2_with_controllers_flow_edge.yml
```

The controller config is `conf/controllers/conf_directional_trading.flow_edge_2.yml`.
The one parameter to retune per pair and venue is `natr_baseline_pct` — set it to the
pair's typical NATR% on the signal timeframe, and every spread and barrier follows.

## Running the tests

```bash
python -m pytest test/controllers/directional_trading/test_flow_edge.py -q
```

## Installing the Condor agent

Copy the agent into your Condor checkout:

```bash
mkdir -p <condor>/trading_agents/flow_edge/routines
# the agent now lives in the Condor repo under agents/flow_edge/
```

Then, from Condor:

```
# 1. verify the routine returns clean data
manage_trading_agent(action="run_routine", strategy_id="2d3359c76636",
                     name="flow_edge_signal",
                     config={"trading_pair": "XRP-USD",
                             "connector_name": "hyperliquid_perpetual"})

# 2. dry run — one tick, no trading
manage_trading_agent(action="start_agent", strategy_id="2d3359c76636",
                     config={"execution_mode": "dry_run"})

# 3. live
manage_trading_agent(action="start_agent", strategy_id="2d3359c76636",
                     config={"execution_mode": "loop", "frequency_sec": 60,
                             "total_amount_quote": 100,
                             "risk_limits": {"max_position_size_quote": 300,
                                             "max_open_executors": 4,
                                             "max_drawdown_pct": -8}})
```

The routine depends only on `pandas` and `numpy` — both already Condor dependencies.
It implements ATR, NATR, RSI, EMA and ADX/DI directly so it needs no TA library, and
those implementations are verified to match the controller's `pandas_ta` results to
floating-point precision.

### Server config note

Condor builds its API base URL from the `servers:` block in `config.yml`. A host with
an embedded scheme (`host: https://api.example.com`, `port: 443`) is handled
correctly by `_build_base_url`. If you ever see requests going to `http://https://...`,
the Condor checkout predates that fix — update it rather than editing the host string.

## Condor agent

The agent half of this submission lives in the Condor repo, in the layout current
Condor discovers:

```
<condor>/agents/flow_edge/
    AGENT.md                              # identity + domain knowledge
    routines/flow_edge_signal.py          # deterministic signal (shared by strategies)
    strategies/flowedge_dca/
        strategy.md                       # the tick playbook
        learnings.md                      # cross-session lessons
```

The routine reimplements this controller's indicators directly rather than importing
a TA library, and agrees with the `pandas_ta` versions to floating-point precision —
so the two runtimes cannot silently disagree about what the market is doing.
