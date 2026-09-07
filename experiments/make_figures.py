#!/usr/bin/env python3
"""Turns recorded experiment trials into the paper's figures.

Reads results/tables/experiment_results.json (written by
experiments/analyze.py) and draws only what was actually measured. A
metric with no usable trials is drawn as an explicit "no data" gap, never
as a zero bar -- a zero bar reads as "measured zero", which is a
different and much stronger claim than "not measured".

Design decisions, so they are not re-litigated per figure:
  * Horizontal bars with the system named on the y-axis. Systems are
    already labelled, so color carries no additional information; using
    four hues would encode identity twice and imply an ordering the
    colors do not have. One hue, and the LADDER ORDER (B1 -> B2 -> P1 ->
    P2) does the work of showing added mechanism.
  * Where a figure genuinely has two series, it uses the two validated
    categorical slots (blue/orange), which clear CVD and normal-vision
    separation in both light and dark modes.
  * Every mean carries its 95% interval as an error bar. A mean from
    fewer than 3 usable trials is hatched and annotated, because a bare
    bar from 1-2 samples reads as a result when it is not one yet.
  * Values are direct-labelled, so a reader never has to measure a bar
    against the axis.

Run:
    python experiments/make_figures.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Validated categorical slots 1 and 2 (see the dataviz palette): they clear
# CVD separation, the normal-vision floor and contrast in both modes.
ACCENT = "#2a78d6"
ACCENT_2 = "#eb6834"
INK = "#0b0b0b"
INK_MUTED = "#52514e"
GRID = "#d9d9d4"

# The ladder, in the order that shows one mechanism being added at a time.
SYSTEM_ORDER = ["B1_SHORTEST_PATH", "B2_RULE_BASED", "P1_ML_HEURISTIC", "P2_ML_RL"]
SYSTEM_LABEL = {
    "B1_SHORTEST_PATH": "B1  static\nshortest path",
    "B2_RULE_BASED": "B2  reactive\nthresholds",
    "P1_ML_HEURISTIC": "P1  ML +\nheuristic path",
    "P2_ML_RL": "P2  ML + RL\n(full system)",
}
MIN_SAMPLES_FOR_CONFIDENCE = 3


def _style(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.xaxis.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _panel(ax, systems, values, errors, counts, title, xlabel, fmt="{:.1f}", scale=1.0):
    """One metric across the system ladder. Missing values are shown as
    an explicit gap plus a 'no data' note, never as a zero-height bar."""
    ypos = list(range(len(systems)))[::-1]
    for y, sysname, val, err, n in zip(ypos, systems, values, errors, counts):
        if val is None:
            ax.text(0, y, "  no data", va="center", ha="left", fontsize=8,
                    color=INK_MUTED, style="italic")
            continue
        underpowered = n < MIN_SAMPLES_FOR_CONFIDENCE
        ax.barh(
            y, val * scale, height=0.55, color=ACCENT,
            alpha=0.45 if underpowered else 1.0,
            hatch="//" if underpowered else None,
            edgecolor=ACCENT, linewidth=0.8,
        )
        if err:
            ax.errorbar(val * scale, y, xerr=err * scale, fmt="none",
                        ecolor=INK_MUTED, elinewidth=1.2, capsize=3)
        note = fmt.format(val * scale) + (f"  (n={n}, provisional)" if underpowered else "")
        ax.text(val * scale + (err or 0) * scale, y, "  " + note,
                va="center", ha="left", fontsize=8.5, color=INK)

    ax.set_yticks(ypos)
    ax.set_yticklabels([SYSTEM_LABEL.get(s, s) for s in systems], fontsize=8.5, color=INK)
    ax.set_xlabel(xlabel, fontsize=9, color=INK_MUTED)
    ax.set_title(title, fontsize=10.5, color=INK, loc="left", pad=8)
    _style(ax)
    ax.margins(x=0.28)


def _series(results: dict, experiment: str, metric: str, sub: str | None = None):
    """Pull one metric across the ladder, preserving None for unmeasured."""
    values, errors, counts, systems = [], [], [], []
    for system in SYSTEM_ORDER:
        entry = results.get(f"{experiment}/{system}")
        if entry is None:
            continue
        systems.append(system)
        counts.append(entry.get("trials_usable", 0))
        if sub is None:
            values.append(entry.get(metric))
            errors.append(None)
        else:
            block = entry.get(metric) or {}
            values.append(block.get("mean"))
            errors.append(block.get("ci95"))
    return systems, values, errors, counts


def figure_e1(results: dict, out_dir: Path) -> bool:
    systems, *_ = _series(results, "E1", "detection_rate")
    if not systems:
        return False

    fig, axes = plt.subplots(2, 2, figsize=(11, 7))
    fig.patch.set_facecolor("white")

    s, v, e, n = _series(results, "E1", "detection_rate")
    _panel(axes[0][0], s, v, e, n, "Faults detected", "% of trials",
           fmt="{:.0f}%", scale=100.0)

    s, v, e, n = _series(results, "E1", "recovery_time_s", sub="mean")
    _panel(axes[0][1], s, v, e, n, "Time to verified recovery",
           "seconds from fault onset", fmt="{:.1f}s")

    s, v, e, n = _series(results, "E1", "throughput_retained_frac", sub="mean")
    _panel(axes[1][0], s, v, e, n, "Throughput retained during fault",
           "% of that trial's own baseline", fmt="{:.0f}%", scale=100.0)

    s, v, e, n = _series(results, "E1", "reroute_count", sub="mean")
    _panel(axes[1][1], s, v, e, n, "Reroutes performed",
           "count per trial (lower = more stable)", fmt="{:.1f}")

    fig.suptitle(
        "E1  End-to-end comparison under a gradual link degradation",
        fontsize=13, color=INK, x=0.055, ha="left", y=0.98,
    )
    fig.text(0.055, 0.935,
             "Error bars are 95% confidence intervals across seeds. "
             "Hatched bars have fewer than 3 usable trials. Emulated Mininet.",
             fontsize=8.5, color=INK_MUTED, ha="left")
    fig.tight_layout(rect=[0, 0, 1, 0.92])
    fig.savefig(out_dir / "e1_comparison.png", dpi=200, facecolor="white")
    plt.close(fig)
    return True


def figure_ladder(results: dict, out_dir: Path) -> bool:
    """Detection vs recovery across the ladder, the two-series case."""
    systems, det, _, counts = _series(results, "E1", "detection_rate")
    _, rec, _, _ = _series(results, "E1", "recovery_rate")
    if not systems:
        return False

    fig, ax = plt.subplots(figsize=(8, 4.2))
    fig.patch.set_facecolor("white")
    ypos = list(range(len(systems)))[::-1]
    h = 0.34
    for y, d, r in zip(ypos, det, rec):
        if d is not None:
            ax.barh(y + h / 2, d * 100, height=h, color=ACCENT, edgecolor=ACCENT)
            ax.text(d * 100, y + h / 2, f"  {d*100:.0f}%", va="center",
                    fontsize=8.5, color=INK)
        if r is not None:
            ax.barh(y - h / 2, r * 100, height=h, color=ACCENT_2, edgecolor=ACCENT_2)
            ax.text(r * 100, y - h / 2, f"  {r*100:.0f}%", va="center",
                    fontsize=8.5, color=INK)

    ax.set_yticks(ypos)
    ax.set_yticklabels([SYSTEM_LABEL.get(s, s) for s in systems], fontsize=8.5, color=INK)
    ax.set_xlabel("% of trials", fontsize=9, color=INK_MUTED)
    ax.set_title("Detected vs fully recovered, across the system ladder",
                 fontsize=11, color=INK, loc="left", pad=10)
    ax.legend(handles=[Patch(color=ACCENT, label="detected the fault"),
                       Patch(color=ACCENT_2, label="verified recovery")],
              frameon=False, fontsize=8.5, loc="lower right")
    _style(ax)
    ax.margins(x=0.2)
    fig.tight_layout()
    fig.savefig(out_dir / "e1_ladder.png", dpi=200, facecolor="white")
    plt.close(fig)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--results", default=str(REPO_ROOT / "results" / "tables" / "experiment_results.json")
    )
    args = parser.parse_args()

    path = Path(args.results)
    if not path.exists():
        print(f"no results at {path} -- run experiments/analyze.py first")
        return 1

    results = json.loads(path.read_text())["results"]
    out_dir = REPO_ROOT / "results" / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)

    drawn = []
    if figure_e1(results, out_dir):
        drawn.append("e1_comparison.png")
    if figure_ladder(results, out_dir):
        drawn.append("e1_ladder.png")

    if not drawn:
        print("no experiment had enough recorded trials to draw yet")
        return 1
    for name in drawn:
        print(f"wrote {out_dir / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
