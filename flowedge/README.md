# FlowEdge — Submission Bundle

Regime-adaptive directional DCA, shipped in both forms the Agent Builders Cup accepts.
See [strategy.md](strategy.md) for the strategy description, parameters and intended
market conditions.

## What is where

| Path | What it is |
|---|---|
| `../controllers/directional_trading/flow_edge.py` | The Hummingbot V2 controller |
| `conf/conf_directional_trading.flow_edge_2.yml` | Controller config (`conf/` is gitignored, so the shipping copy lives here — copy it to `conf/controllers/` to run) |
| `conf/conf_directional_trading.flow_edge_hackathon.yml` | Competition config — tuned from the 12h Derive live run (see header comments) |
| `../test/controllers/directional_trading/test_flow_edge.py` | 39 controller unit tests |
| `../test/controllers/directional_trading/test_flow_edge_parity.py` | 10 parity tests: controller vs Condor routine |
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

## Deployment layout


FlowEdge spans two servers, mirroring Condor's split between reasoning and
execution. This repo is the source of truth; both deployments are copies of it.

```
                 CONDOR SERVER                      HUMMINGBOT-API SERVER
              (reasoning, LLM tick)                  (execution, no LLM)
        ┌──────────────────────────────┐        ┌──────────────────────────────┐
        │  agents/flow_edge/           │  REST  │  bots/controllers/           │
        │    AGENT.md                  │ ─────► │    directional_trading/      │
        │    routines/                 │        │      flow_edge.py            │
        │      flow_edge_signal.py     │        │  bots/conf/controllers/      │
        │    strategies/flowedge_dca/  │        │    flow_edge.yml             │
        │      strategy.md             │        └──────────────────────────────┘
        │      learnings.md            │                      │
        │      sessions/session_N/     │                      ▼
        └──────────────────────────────┘              exchange connectors
```

Either half runs alone. The controller needs no LLM and no Condor; the agent needs
the API server but not the controller — it drives executors directly.

**Controller → `hummingbot-api`**

| From this repo | To the API server |
|---|---|
| `controllers/directional_trading/flow_edge.py` | `bots/controllers/directional_trading/flow_edge.py` |
| `flowedge/conf/conf_directional_trading.flow_edge_hackathon.yml` | `bots/conf/controllers/flow_edge.yml` |

```bash
cp controllers/directional_trading/flow_edge.py \
   <hummingbot-api>/bots/controllers/directional_trading/
cp flowedge/conf/conf_directional_trading.flow_edge_hackathon.yml \
   <hummingbot-api>/bots/conf/controllers/flow_edge.yml
```

The API imports it as `bots.controllers.directional_trading.flow_edge`, so the
filename must stay `flow_edge.py` to match `controller_name: flow_edge` in the
config. Deploy with the `Deploy V2 Controllers` endpoint, or from Condor with
`/bots`. Per-instance overrides land in `bots/instances/{bot}/conf/controllers/`.

To run it inside a plain Hummingbot checkout instead, the controller stays at
`controllers/directional_trading/` and the config goes to `conf/controllers/`.

**Agent → Condor**

| From this repo | To the Condor server |
|---|---|
| *(agent files live in the Condor repo)* | `agents/flow_edge/AGENT.md` |
| | `agents/flow_edge/routines/flow_edge_signal.py` |
| | `agents/flow_edge/strategies/flowedge_dca/strategy.md` |
| | `agents/flow_edge/strategies/flowedge_dca/learnings.md` |

The layout is fixed by Condor's loaders, not by preference: `AgentStore` scans
`agents/*/AGENT.md`, the strategy folder name must equal the slug of its `name:`
(`FlowEdge DCA` → `flowedge_dca`), and agent-local routines are discovered only at
`agents/{slug}/routines/`. `sessions/` and `dry_runs/` are created on first run.

Older Condor builds use the pre-refactor layout — `condor/trading_agent/` and a
single `trading_agents/{slug}/agent.md`. If `condor/agents/` does not exist on your
checkout, you are on that build and the split above will not be discovered.

**Keeping the two in step.** `test_flow_edge_parity.py` compares the controller
against the agent routine and fails if they drift. Point it at your Condor checkout:

```bash
FLOWEDGE_ROUTINE=<condor>/agents/flow_edge/routines/flow_edge_signal.py \
  python3 -m pytest test/controllers/directional_trading/ -q
```
