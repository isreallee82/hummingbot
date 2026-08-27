"""
Generate the FlowEdge submission diagrams as SVG.

Diagram 3 is not drawn by hand — it runs the real FlowEdgeProController
adaptation code against a scripted trade sequence and plots the resulting
threshold trajectory against the old ratcheting rule, so the picture cannot
drift away from the implementation.

Usage:  python3 flowedge/diagrams/build_diagrams.py
"""

import asyncio
import os
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
OUT = os.path.dirname(os.path.abspath(__file__))

# ── palette ───────────────────────────────────────────────────────────────
INK = "#0f172a"
MUTED = "#64748b"
LINE = "#94a3b8"
WHITE = "#ffffff"

BLUE = ("#0284c7", "#e0f2fe")
ORANGE = ("#c2410c", "#ffedd5")
GREEN = ("#047857", "#d1fae5")
PURPLE = ("#6d28d9", "#ede9fe")
RED = ("#b91c1c", "#fee2e2")
SLATE = ("#475569", "#f1f5f9")

FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO = "'SF Mono', Menlo, Consolas, monospace"


def esc(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class Canvas:
    def __init__(self, w, h, label):
        self.w, self.h, self.label = w, h, label
        self.parts = []

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, t, size=13, weight="normal", fill=INK, anchor="middle",
             font=FONT, style=""):
        st = f' font-style="{style}"' if style else ""
        self.add(f'<text x="{x}" y="{y}" font-family="{font}" font-size="{size}" '
                 f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{st}>{esc(t)}</text>')

    def box(self, x, y, w, h, colour, title=None, lines=None, rx=8, title_size=13,
            line_size=11, dashed=False, fill=None):
        stroke, bg = colour
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
                 f'fill="{fill or bg}" stroke="{stroke}" stroke-width="1.8"{dash}/>')
        cx = x + w / 2
        lines = lines or []
        block_h = (title_size + 5 if title else 0) + len(lines) * (line_size + 4)
        cur = y + (h - block_h) / 2 + title_size
        if title:
            self.text(cx, cur, title, size=title_size, weight="bold", fill=stroke)
            cur += title_size + 2
        for ln in lines:
            cur += line_size + 2
            self.text(cx, cur, ln, size=line_size, fill=MUTED)
        return (cx, y, cx, y + h)

    def banner(self, x, y, w, h, colour, title, sub=None):
        stroke, _ = colour
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" '
                 f'fill="{stroke}"/>')
        cx = x + w / 2
        if sub:
            self.text(cx, y + h / 2 - 2, title, size=14, weight="bold", fill=WHITE)
            self.text(cx, y + h / 2 + 15, sub, size=11, fill="#e2e8f0")
        else:
            self.text(cx, y + h / 2 + 5, title, size=14, weight="bold", fill=WHITE)

    def diamond(self, cx, cy, w, h, colour, title, lines=None):
        stroke, bg = colour
        pts = f"{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}"
        self.add(f'<polygon points="{pts}" fill="{bg}" stroke="{stroke}" stroke-width="1.8"/>')
        lines = lines or []
        if lines:
            self.text(cx, cy - 3, title, size=12, weight="bold", fill=stroke)
            for i, ln in enumerate(lines):
                self.text(cx, cy + 13 + i * 13, ln, size=10, fill=MUTED)
        else:
            self.text(cx, cy + 4, title, size=12, weight="bold", fill=stroke)

    def arrow(self, x1, y1, x2, y2, label=None, colour=LINE, dashed=False,
              label_dx=0, label_dy=-6, label_anchor="middle", width=1.8):
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        self.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{colour}" '
                 f'stroke-width="{width}"{dash} marker-end="url(#ah)"/>')
        if label:
            self.text((x1 + x2) / 2 + label_dx, (y1 + y2) / 2 + label_dy, label,
                      size=10, fill=MUTED, anchor=label_anchor, style="italic")

    def elbow(self, pts, label=None, colour=LINE, dashed=False, label_at=None):
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        d = " ".join(f"{x},{y}" for x, y in pts)
        self.add(f'<polyline points="{d}" fill="none" stroke="{colour}" '
                 f'stroke-width="1.8"{dash} marker-end="url(#ah)"/>')
        if label and label_at:
            self.text(label_at[0], label_at[1], label, size=10, fill=MUTED, style="italic")

    def render(self):
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
            f'width="{self.w}" height="{self.h}" role="img" aria-label="{esc(self.label)}">'
            f'<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{LINE}"/></marker></defs>'
            f'<rect width="{self.w}" height="{self.h}" fill="{WHITE}"/>'
            + "".join(self.parts) + "</svg>"
        )

    def save(self, name):
        path = os.path.join(OUT, name)
        with open(path, "w") as f:
            f.write(self.render())
        print("wrote", path)


# ══════════════════════════════════════════════════════════════════════════
# 1. Signal pipeline — the claim is that every feature is normalised into a
#    common unit BEFORE it is weighted.
# ══════════════════════════════════════════════════════════════════════════

def diagram_signal():
    c = Canvas(1180, 980, "FlowEdge signal pipeline: three price features are "
                          "divided by live volatility and squashed into a common "
                          "range before they are weighted and blended")
    c.text(590, 44, "FlowEdge — Signal Pipeline", size=24, weight="bold")
    c.text(590, 70, "Every feature is normalised into the same unit before it is weighted",
           size=13, fill=MUTED)

    # sources
    c.banner(70, 108, 470, 46, BLUE, "FAST CANDLES · 3m", "signal generation")
    c.banner(650, 108, 460, 46, ORANGE, "SLOW CANDLES · 15m", "regime classification")

    # raw features
    feat = [
        (70, "Candle Flow", "(close−open) / (high−low)", "5-bar mean"),
        (232, "VWAP Extension", "(close − VWAP) / VWAP", "rolling 24-bar window"),
        (394, "Trend", "close − EMA(21)", "in ATR units"),
    ]
    for x, t, l1, l2 in feat:
        c.box(x, 196, 146, 66, SLATE, t, [l1, l2], line_size=9.5, title_size=11.5)
        c.arrow(x + 73, 154, x + 73, 194)

    # normaliser band — the load-bearing step
    c.box(70, 300, 470, 70, GREEN, None,
          [], fill="#d1fae5")
    c.text(305, 322, "÷ live NATR   →   tanh( x / 2 )", size=14, weight="bold",
           fill=GREEN[0], font=MONO)
    c.text(305, 341, "price-space features become volatility units, then saturate into [−1, +1]",
           size=10.5, fill=MUTED)
    c.text(305, 356, "all three now share one range, so the weights below are the weights that apply",
           size=10, fill=MUTED, style="italic")
    for x, *_ in feat:
        c.arrow(x + 73, 262, x + 73, 298)

    # regime side
    c.box(650, 196, 460, 66, ORANGE, "ADX(14) + Directional Indicators",
          ["ADX gives trend strength · DI+/DI− give its direction"], line_size=10.5)
    c.arrow(880, 154, 880, 194)
    c.box(650, 300, 215, 54, ORANGE, "Regime gate",
          ["RANGING / TRENDING / EXTREME"], line_size=10, title_size=12)
    c.box(895, 300, 215, 54, ORANGE, "DI bias",
          ["(DI+ − DI−) / (DI+ + DI−)"], line_size=10, title_size=12)
    c.arrow(757, 262, 757, 298)
    c.arrow(1002, 262, 1002, 298)

    # blend
    c.box(230, 404, 660, 62, PURPLE, "Weighted blend  →  score ∈ [−1, +1]",
          ["0.35 · flow    +    0.25 · vwap    +    0.25 · trend    +    0.15 · DI bias"],
          line_size=11.5)
    c.elbow([(305, 370), (305, 388), (450, 388), (450, 402)])
    c.elbow([(1002, 354), (1002, 384), (760, 384), (760, 402)], )

    # dampener + funding
    c.box(230, 498, 315, 70, SLATE, "RSI dampener",
          ["proportional, not a veto:", "RSI 72 trims · RSI 90 erases"], line_size=10)
    c.box(575, 498, 315, 70, SLATE, "Funding bias · perps",
          ["− strength · tanh(rate / threshold)", "continuous, saturating"], line_size=10)
    c.arrow(387, 466, 387, 496)
    c.arrow(732, 466, 732, 496)

    # threshold
    c.diamond(560, 630, 300, 92, BLUE, "|score| ≥ adapted threshold ?",
              ["and the regime gate is open"])
    c.elbow([(387, 568), (387, 592), (470, 592), (500, 614)])
    c.elbow([(732, 568), (732, 592), (650, 592), (620, 614)])

    c.box(60, 602, 190, 56, RED, "signal = 0", ["no entry this tick"], line_size=10)
    c.arrow(410, 630, 252, 630, "no")

    # execution
    c.box(330, 706, 460, 56, GREEN, "signal = +1 LONG   /   −1 SHORT", [], title_size=13)
    c.arrow(560, 676, 560, 704, "yes")

    c.box(330, 790, 460, 68, BLUE, "DCA ladder · MAKER · 3 levels",
          ["prices, stop-loss, take-profit and trailing stop",
           "all scaled by  clamp(NATR / baseline, 0.6, 2.5)"], line_size=10)
    c.arrow(560, 762, 560, 788)

    c.box(330, 886, 460, 58, RED, "Emergency exit  ·  stop_actions_proposal()",
          ["closes any executor past −5% — MAKER defers its own stop"], line_size=10)
    c.arrow(560, 858, 560, 884)

    # feedback
    c.box(880, 706, 250, 152, PURPLE, "Self-adaptation",
          ["closed executors →", "rolling win rate + turnover", "→ steers the threshold"],
          line_size=10.5)
    c.elbow([(790, 915), (1005, 915), (1005, 860)], colour=PURPLE[0], dashed=True)
    c.elbow([(880, 782), (830, 782), (830, 660), (712, 660)],
            colour=PURPLE[0], dashed=True)
    c.text(1005, 934, "closed P&L feeds back each tick", size=10, fill=PURPLE[0], style="italic")

    c.save("01-signal-pipeline.svg")


# ══════════════════════════════════════════════════════════════════════════
# 2. Regime gate — the claim is that the two timeframes are NOT symmetric.
# ══════════════════════════════════════════════════════════════════════════

def diagram_regime():
    c = Canvas(1180, 760, "FlowEdge regime gate: the slow timeframe halts on an "
                          "extreme reading while the fast timeframe merely fails "
                          "to confirm")
    c.text(590, 44, "Regime Gate — Why the Two Timeframes Differ", size=24, weight="bold")
    c.text(590, 70, "ADX(14) is computed on both frames, but only the slow frame can halt the strategy",
           size=13, fill=MUTED)

    c.banner(120, 110, 380, 46, ORANGE, "SLOW · 15m", "the risk-off filter")
    c.banner(680, 110, 380, 46, BLUE, "FAST · 3m", "the confirmation vote")

    # slow branch
    c.diamond(310, 240, 220, 84, ORANGE, "ADX on 15m")
    c.arrow(310, 156, 310, 198)

    c.box(120, 330, 150, 62, RED, "EXTREME", ["ADX ≥ 50"], line_size=10)
    c.box(285, 330, 150, 62, GREEN, "TRENDING", ["22 ≤ ADX < 50"], line_size=10)
    c.box(450, 330, 150, 62, SLATE, "RANGING", ["ADX < 22"], line_size=10)
    c.arrow(255, 265, 200, 328)
    c.arrow(310, 282, 355, 328)
    c.arrow(365, 265, 520, 328)

    # fast branch
    c.diamond(870, 240, 220, 84, BLUE, "ADX on 3m")
    c.arrow(870, 156, 870, 198)

    c.box(680, 330, 150, 62, SLATE, "EXTREME", ["ADX ≥ 50"], line_size=10)
    c.box(845, 330, 150, 62, GREEN, "TRENDING", ["22 ≤ ADX < 50"], line_size=10)
    c.box(1010, 330, 150, 62, SLATE, "RANGING", ["ADX < 22"], line_size=10)
    c.arrow(815, 265, 760, 328)
    c.arrow(870, 282, 915, 328)
    c.arrow(925, 265, 1080, 328)

    # the asymmetry, stated on the marks themselves
    c.text(195, 412, "halts everything", size=11, weight="bold", fill=RED[0])
    c.text(195, 428, "crash / parabolic squeeze", size=10, fill=MUTED, style="italic")
    c.text(755, 412, "does NOT halt", size=11, weight="bold", fill=INK)
    c.text(755, 428, "ADX(3m) > 50 is routine in crypto", size=10, fill=MUTED, style="italic")
    c.text(755, 444, "it just fails to confirm", size=10, fill=MUTED, style="italic")

    # gate
    c.box(390, 490, 400, 70, PURPLE, "Entry gate",
          ["at least one frame TRENDING, and the slow frame not EXTREME"],
          line_size=10.5)
    c.elbow([(360, 392), (360, 452), (520, 452), (520, 488)])
    c.elbow([(920, 392), (920, 452), (660, 452), (660, 488)])

    # outcomes
    outs = [
        (60, GREEN, "both", "slow + fast agree", "strongest conviction"),
        (345, GREEN, "slow", "15m confirms alone", "standard mode"),
        (630, GREEN, "fast", "3m confirms alone", "needs allow_fast_regime_entry"),
        (915, RED, "none / halt", "no confirmation, or", "slow frame EXTREME"),
    ]
    for x, col, t, l1, l2 in outs:
        c.box(x, 620, 205, 76, col, f'gate = "{t}"', [l1, l2], line_size=9.5)
    c.elbow([(590, 560), (590, 590), (162, 590), (162, 618)])
    c.elbow([(590, 560), (590, 590), (447, 590), (447, 618)])
    c.elbow([(590, 560), (590, 590), (732, 590), (732, 618)])
    c.elbow([(590, 560), (590, 590), (1017, 590), (1017, 618)])
    # EXTREME on the slow frame bypasses the gate entirely
    c.elbow([(195, 392), (195, 578), (1017, 578), (1017, 618)], colour=RED[0])
    c.text(600, 570, "slow frame EXTREME bypasses the gate — nothing can open",
           size=10, fill=RED[0], style="italic")

    c.text(590, 726, "signal is forced to 0 whenever the gate is \"none\" or \"halt\"",
           size=11, fill=MUTED, style="italic")

    c.save("02-regime-gate.svg")


# ══════════════════════════════════════════════════════════════════════════
# 3. Adaptation — generated from the REAL controller, compared against the
#    ratcheting rule it replaced.
# ══════════════════════════════════════════════════════════════════════════

def simulate_threshold():
    """Drive the real _self_adapt() and the old ratchet rule over one script."""
    from controllers.directional_trading.flow_edge import FlowEdgeProConfig, FlowEdgeProController
    from hummingbot.data_feed.market_data_provider import MarketDataProvider
    from hummingbot.strategy_v2.models.executors import CloseType

    now = [1_000_000.0]
    config = FlowEdgeProConfig(
        id="sim", controller_name="flow_edge", connector_name="hyperliquid_perpetual",
        trading_pair="XRP-USD", total_amount_quote=Decimal("100"),
        candles_connector="hyperliquid_perpetual", candles_trading_pair="XRP-USD",
        signal_threshold=0.30, adapt_step=0.02, adapt_min_samples=4,
        threshold_floor=0.15, threshold_ceiling=0.60, stale_entry_seconds=1800,
    )
    mdp = MagicMock(spec=MarketDataProvider)
    mdp.time.side_effect = lambda: now[0]
    mdp.get_funding_info.return_value = None
    ctrl = FlowEdgeProController(config=config, market_data_provider=mdp,
                                 actions_queue=AsyncMock(spec=asyncio.Queue))

    def executor(eid, pnl):
        return SimpleNamespace(id=eid, timestamp=now[0], net_pnl_quote=Decimal(str(pnl)),
                               is_done=True, close_type=CloseType.STOP_LOSS,
                               is_active=False, side=None, net_pnl_pct=Decimal("0"),
                               is_trading=False)

    # Scripted market outcomes: a losing streak, a quiet spell, then a
    # favourable stretch. Each rule only RECEIVES an outcome if its own
    # threshold still lets an entry fire — that feedback is the whole point.
    # Handing the ratchet wins it could never have earned would flatter it.
    REACHABLE = 0.62  # empirical max |score| the features produce
    script = ([-1.0] * 8) + ([None] * 25) + ([1.0] * 14) + ([None] * 8)

    new_series, old_series = [], []
    old_threshold = config.signal_threshold
    old_window = []
    n = 0
    for outcome in script:
        now[0] += 120.0

        # --- current rule: target-tracking, keyed by executor id
        if outcome is not None and ctrl._adapted_threshold < REACHABLE:
            n += 1
            ctrl.executors_info = [executor(f"e{n}", outcome)]
        else:
            ctrl.executors_info = []
        ctrl._self_adapt()
        new_series.append(ctrl._adapted_threshold)

        # --- the rule this replaced: ratchet up on a weak window, loosening
        # floored at the config base. Once it outruns the reachable score
        # range no entry fires, so no trade closes, so the window it needs in
        # order to loosen never refreshes.
        if outcome is not None and old_threshold < REACHABLE:
            old_window.append(outcome)
            old_window[:] = old_window[-20:]
        if len(old_window) >= 3:
            wr = sum(1 for p in old_window if p > 0) / len(old_window)
            if wr < 0.40 and old_threshold < 0.75:
                old_threshold = round(min(old_threshold + 0.05, 0.75), 2)
            elif wr > 0.65 and old_threshold > config.signal_threshold:
                old_threshold = round(max(old_threshold - 0.02, config.signal_threshold), 2)
        old_series.append(old_threshold)

    return new_series, old_series, config


def diagram_adaptation():
    new_s, old_s, config = simulate_threshold()
    c = Canvas(1180, 720, "Threshold trajectory: the target-tracking rule recovers "
                          "after a losing streak while the ratcheting rule it replaced "
                          "latches above the reachable score range and stops trading")
    c.text(590, 42, "Self-Adaptation — Target-Tracking vs. Ratcheting", size=24, weight="bold")
    c.text(590, 68, "Same trade sequence through both rules. Generated by running the real controller.",
           size=13, fill=MUTED)

    # plot frame
    px, py, pw, ph = 90, 110, 700, 420
    ymin, ymax = 0.10, 0.85
    n = len(new_s)

    def X(i):
        return px + pw * i / (n - 1)

    def Y(v):
        return py + ph * (ymax - v) / (ymax - ymin)

    c.add(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" fill="#fbfcfd" '
          f'stroke="{LINE}" stroke-width="1"/>')

    # reachable-score band
    reach = 0.62
    c.add(f'<rect x="{px}" y="{py}" width="{pw}" height="{Y(reach) - py}" '
          f'fill="{RED[1]}" opacity="0.65"/>')
    c.text(px + pw - 10, Y(reach) - 10, "above this, no score can ever clear the threshold",
           size=10.5, fill=RED[0], anchor="end", style="italic")
    c.add(f'<line x1="{px}" y1="{Y(reach)}" x2="{px + pw}" y2="{Y(reach)}" '
          f'stroke="{RED[0]}" stroke-width="1.4" stroke-dasharray="7 4"/>')

    # gridlines
    for v in (0.15, 0.30, 0.45, 0.60, 0.75):
        c.add(f'<line x1="{px}" y1="{Y(v)}" x2="{px + pw}" y2="{Y(v)}" '
              f'stroke="{LINE}" stroke-width="0.6" stroke-dasharray="2 4"/>')
        c.text(px - 12, Y(v) + 4, f"{v:.2f}", size=11, fill=MUTED, anchor="end")

    # bands
    for v, lbl, dy in ((config.threshold_floor, "floor 0.15", -7),
                       (config.threshold_ceiling, "ceiling 0.60", 15)):
        c.add(f'<line x1="{px}" y1="{Y(v)}" x2="{px + pw}" y2="{Y(v)}" '
              f'stroke="{GREEN[0]}" stroke-width="1.2"/>')
        c.text(px + 8, Y(v) + dy, lbl, size=10, fill=GREEN[0], anchor="start", style="italic")

    # phase markers
    for start, end, label in ((0, 8, "8 losing trades"), (8, 33, "quiet — nothing opens"),
                              (33, 47, "14 winning trades")):
        xm = (X(start) + X(min(end, n - 1))) / 2
        c.add(f'<line x1="{X(min(end, n - 1))}" y1="{py}" x2="{X(min(end, n - 1))}" '
              f'y2="{py + ph}" stroke="{LINE}" stroke-width="0.8" stroke-dasharray="3 3"/>')
        c.text(xm, py + ph + 20, label, size=10.5, fill=MUTED, style="italic")

    def path(series, colour, width=2.6, dashed=False):
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(series))
        dash = ' stroke-dasharray="7 4"' if dashed else ""
        c.add(f'<polyline points="{pts}" fill="none" stroke="{colour}" '
              f'stroke-width="{width}" stroke-linejoin="round"{dash}/>')

    path(old_s, RED[0], dashed=True)
    path(new_s, GREEN[0])

    # end labels
    c.text(X(n - 1) - 14, Y(old_s[-1]) - 12, f"ratchet — latched at {old_s[-1]:.2f}",
           size=12, weight="bold", fill=RED[0], anchor="end")
    c.text(X(n - 1) - 14, Y(new_s[-1]) - 14, f"target-tracking — {new_s[-1]:.2f}",
           size=12, weight="bold", fill=GREEN[0], anchor="end")

    c.text(px + pw / 2, py + ph + 46, "closed trades over time  →", size=11, fill=MUTED)
    c.add(f'<text x="30" y="{py + ph / 2}" font-family="{FONT}" font-size="11" '
          f'fill="{MUTED}" text-anchor="middle" transform="rotate(-90 30 {py + ph / 2})">'
          f'entry threshold</text>')

    # explanation column
    c.box(840, 110, 300, 130, RED, "The ratchet latches",
          ["Losses push it up in 0.05 steps and", "loosening floors at the config base.",
           "Once it passes the reachable range,", "no entry fires — so no trade closes,",
           "so the win-rate window never updates."], line_size=10)
    c.box(840, 258, 300, 130, GREEN, "Target-tracking recovers",
          ["Each tick it moves one small step", "toward a target set by win rate and",
           "turnover. Every state is reachable", "from every other, so a losing streak",
           "can tighten it but never trap it."], line_size=10)
    c.box(840, 406, 300, 124, PURPLE, "Turnover governor",
          ["When nothing has opened for 30 min", "the target drops to the floor —",
           "measured from the controller's own", "age when nothing has EVER opened,",
           "which closes the cold-start hole."], line_size=10)

    # mechanism strip
    c.text(590, 600, "How a closed trade reaches the threshold", size=13, weight="bold")
    steps = [
        (60, SLATE, "Executor terminates", "close_type set"),
        (295, SLATE, "Keyed by id", "_seen_closed_ids — never by index"),
        (530, SLATE, "Rolling window", "last 20 P&Ls (deque)"),
        (765, SLATE, "Target chosen", "win rate + turnover"),
        (1000, PURPLE, "Step 0.02", "toward target, clamped"),
    ]
    for x, col, t, sub in steps:
        c.box(x, 622, 180, 62, col, t, [sub], line_size=9.5, title_size=11.5)
    for x in (240, 475, 710, 945):
        c.arrow(x, 653, x + 55, 653)

    c.save("03-adaptation.svg")


if __name__ == "__main__":
    diagram_signal()
    diagram_regime()
    diagram_adaptation()
