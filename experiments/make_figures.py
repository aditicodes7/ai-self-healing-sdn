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


FAULT_LABEL = {
    "link_failure": "link failure\n(cable cut)",
    "gradual_degradation": "gradual\ndegradation",
    "congestion": "congestion",
    "high_latency": "high latency",
    "packet_loss": "packet loss",
    "rising_errors": "rising errors",
}


def figure_e2(by_fault: dict, out_dir: Path) -> bool:
    """E2: detection rate and recovery time, per fault type.

    Drawn from the per-fault breakdown rather than E2's pooled row,
    because the pooled row is the one thing this experiment must not
    report: a single detection rate averages a fault caught 5/5 with one
    caught 0/5 and describes neither. Faults are ordered by detection
    rate so the ones the system cannot see sit at the bottom, stated
    rather than buried mid-chart.
    """
    cells = {k.split("/")[2]: v for k, v in by_fault.items() if k.startswith("E2/P2_ML_RL/")}
    if not cells:
        return False

    order = sorted(cells, key=lambda f: (cells[f]["detection_rate"] or 0.0, f))
    ypos = list(range(len(order)))

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), facecolor="white")

    ax = axes[0]
    for y, fault in zip(ypos, order):
        s = cells[fault]
        rate = (s["detection_rate"] or 0.0) * 100
        n = s["trials_usable"]
        # A zero here is a real measured zero -- the fault was injected
        # five times and seen none of them -- so it gets a visible marker
        # rather than an empty row, which would read as missing data.
        if rate == 0:
            ax.plot([0], [y], marker="|", markersize=14, color=ACCENT_2, markeredgewidth=2)
            ax.text(2, y, f"  0%  (0 of {n} detected)", va="center", ha="left",
                    fontsize=8.5, color=ACCENT_2)
        else:
            ax.barh(y, rate, height=0.55, color=ACCENT, edgecolor=ACCENT, linewidth=0.8)
            ax.text(rate + 2, y, f"  {rate:.0f}%  ({round(rate / 100 * n)} of {n})",
                    va="center", ha="left", fontsize=8.5, color=INK)
    ax.set_yticks(ypos)
    ax.set_yticklabels([FAULT_LABEL.get(f, f) for f in order], fontsize=8.5, color=INK)
    ax.set_xlim(0, 100)
    ax.set_xlabel("faults detected (%)", fontsize=9, color=INK_MUTED)
    ax.set_title("What the system detects, by fault type", fontsize=10.5, color=INK, loc="left", pad=8)
    _style(ax)

    ax = axes[1]
    for y, fault in zip(ypos, order):
        s = cells[fault]
        entry = s["recovery_time_s"]
        if entry is None or entry.get("mean") is None:
            ax.text(0, y, "  never detected, so never repaired", va="center", ha="left",
                    fontsize=8, color=INK_MUTED, style="italic")
            continue
        n = entry["n"]
        under = n < MIN_SAMPLES_FOR_CONFIDENCE
        ax.barh(y, entry["mean"], height=0.55, color=ACCENT,
                alpha=0.45 if under else 1.0, hatch="//" if under else None,
                edgecolor=ACCENT, linewidth=0.8)
        if entry.get("ci95"):
            ax.errorbar(entry["mean"], y, xerr=entry["ci95"], fmt="none",
                        ecolor=INK_MUTED, elinewidth=1.2, capsize=3)
        note = f"{entry['mean']:.0f}s" + (f"  (n={n}, provisional)" if under else "")
        ax.text(entry["mean"] + (entry.get("ci95") or 0), y, "  " + note,
                va="center", ha="left", fontsize=8.5, color=INK)
    ax.set_yticks(ypos)
    ax.set_yticklabels([])
    ax.set_xlabel("time from fault to verified repair (s)", fontsize=9, color=INK_MUTED)
    ax.set_title("How long the repair takes", fontsize=10.5, color=INK, loc="left", pad=8)
    _style(ax)
    ax.margins(x=0.30)

    fig.suptitle("E2  Coverage across six fault types  \u2014  P2 (ML + RL). "
                 "Static routing detected 0 of 29.",
                 fontsize=11, color=INK, x=0.012, ha="left", y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out_dir / "e2_by_fault.png", dpi=200, facecolor="white")
    plt.close(fig)
    return True


def figure_e3(by_fault: dict, out_dir: Path) -> bool:
    """E3: does prediction actually detect earlier than a threshold?

    Two panels because the answer has two halves that point in opposite
    directions, and showing only one of them would misrepresent the
    result. Left: how often each system detects the fault while it is
    still happening. Right: how quickly, when it does.

    The reactive baseline's late "detections" are drawn as a separate
    hatched marker rather than being dropped silently or folded into its
    rate -- they are real events the system logged, they are just not
    detections of the fault.
    """
    faults = ["gradual_degradation", "congestion"]
    systems = ["B2_RULE_BASED", "P2_ML_RL"]
    cells = {(s, f): by_fault.get(f"E3/{s}/{f}") for s in systems for f in faults}
    if not any(cells.values()):
        return False

    label = {"B2_RULE_BASED": "B2  reactive\nthresholds", "P2_ML_RL": "P2  ML + RL\n(full system)"}
    colors = {"B2_RULE_BASED": ACCENT_2, "P2_ML_RL": ACCENT}

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4), facecolor="white")
    height, gap = 0.34, 0.42
    ypos = {f: i for i, f in enumerate(reversed(faults))}

    ax = axes[0]
    for f in faults:
        for k, s in enumerate(systems):
            cell = cells[(s, f)]
            if not cell:
                continue
            y = ypos[f] + (gap / 2 if k == 0 else -gap / 2)
            rate = (cell["detection_rate_in_fault"] or 0.0) * 100
            n = cell["trials_usable"]
            ax.barh(y, rate, height=height, color=colors[s], edgecolor=colors[s], linewidth=0.8)
            late = cell["detections_after_fault_ended"]
            note = f"  {rate:.0f}%  ({round(rate / 100 * n)}/{n})"
            if late:
                note += f"  + {late} after the fault ended"
            ax.text(rate + 1.5, y, note, va="center", ha="left", fontsize=8.5,
                    color=INK if not late else ACCENT_2)
    ax.set_xlim(0, 100)
    ax.set_yticks([ypos[f] for f in faults])
    ax.set_yticklabels([FAULT_LABEL.get(f, f) for f in faults], fontsize=8.5, color=INK)
    ax.set_xlabel("faults detected while still happening (%)", fontsize=9, color=INK_MUTED)
    ax.set_title("How often the fault is caught", fontsize=10.5, color=INK, loc="left", pad=8)
    _style(ax)

    ax = axes[1]
    for f in faults:
        for k, s in enumerate(systems):
            cell = cells[(s, f)]
            if not cell:
                continue
            entry = cell["detection_latency_in_fault_s"] or {}
            if entry.get("mean") is None:
                continue
            y = ypos[f] + (gap / 2 if k == 0 else -gap / 2)
            n = entry["n"]
            under = n < MIN_SAMPLES_FOR_CONFIDENCE
            ax.barh(y, entry["mean"], height=height, color=colors[s],
                    alpha=0.45 if under else 1.0, hatch="//" if under else None,
                    edgecolor=colors[s], linewidth=0.8)
            if entry.get("ci95"):
                ax.errorbar(entry["mean"], y, xerr=entry["ci95"], fmt="none",
                            ecolor=INK_MUTED, elinewidth=1.2, capsize=3)
            note = f"{entry['mean']:.0f}s" + (f"  (n={n}, provisional)" if under else "")
            ax.text(entry["mean"] + (entry.get("ci95") or 0) + 1, y, "  " + note,
                    va="center", ha="left", fontsize=8.5, color=INK)
    ax.set_yticks([ypos[f] for f in faults])
    ax.set_yticklabels([])
    ax.set_xlabel("time from fault onset to detection (s)  \u2014  lower is earlier",
                  fontsize=9, color=INK_MUTED)
    ax.set_title("How early, when it is caught", fontsize=10.5, color=INK, loc="left", pad=8)
    _style(ax)
    ax.margins(x=0.34)

    # Figure-level, in the title band: every in-axes corner is occupied by
    # a value label, and a legend that sits on top of one costs more than
    # it explains.
    handles = [Patch(facecolor=colors[s], edgecolor=colors[s], label=label[s].replace("\n", " "))
               for s in systems]
    fig.legend(handles=handles, loc="upper right", bbox_to_anchor=(0.995, 1.015),
               ncol=2, frameon=False, fontsize=8.5)

    fig.suptitle("E3  Prediction lead time  \u2014  the predictive system detects MORE, not EARLIER.",
                 fontsize=11, color=INK, x=0.012, ha="left", y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out_dir / "e3_lead_time.png", dpi=200, facecolor="white")
    plt.close(fig)
    return True


def _bars(ax, labels, values, errors, notes, colors, xlabel, title):
    """One horizontal bar per arm, with the value written past the bar so
    a reader never has to measure against the axis."""
    ypos = list(range(len(labels)))[::-1]
    for y, lab, val, err, note, col in zip(ypos, labels, values, errors, notes, colors):
        if val is None:
            ax.text(0, y, "  no data", va="center", ha="left", fontsize=8,
                    color=INK_MUTED, style="italic")
            continue
        ax.barh(y, val, height=0.5, color=col, edgecolor=col, linewidth=0.8)
        if err:
            ax.errorbar(val, y, xerr=err, fmt="none", ecolor=INK_MUTED,
                        elinewidth=1.2, capsize=3)
        ax.text(val + (err or 0), y, "  " + note, va="center", ha="left",
                fontsize=8.5, color=INK)
    ax.set_yticks(ypos)
    ax.set_yticklabels(labels, fontsize=8.5, color=INK)
    ax.set_xlabel(xlabel, fontsize=9, color=INK_MUTED)
    ax.set_title(title, fontsize=10.5, color=INK, loc="left", pad=8)
    _style(ax)
    ax.margins(x=0.30)


def figure_e4(results: dict, out_dir: Path) -> bool:
    """E4: does the system flap? Reroutes per run is the primary metric.

    Late detections are annotated rather than dropped: an incident opened
    after the fault was already gone still installed flow rules, so it
    belongs in a chart about how much the system churns.
    """
    order = ["E4/B2_RULE_BASED", "E4/P1_ML_HEURISTIC", "E4/P2_ML_RL"]
    cells = [(k, results.get(k)) for k in order]
    if not any(v for _, v in cells):
        return False
    labels, values, errors, notes, colors = [], [], [], [], []
    for k, s in cells:
        if not s:
            continue
        rr = s["reroute_count"]
        labels.append(SYSTEM_LABEL.get(k.split("/")[1], k))
        values.append(rr["mean"]); errors.append(rr.get("ci95"))
        late = s["detections_after_fault_ended"]
        note = f"{rr['mean']:.2f} per run"
        if late:
            note += f"   ({late} reroute{'s' if late > 1 else ''} after the fault had gone)"
        notes.append(note)
        colors.append(ACCENT_2 if late else ACCENT)

    fig, ax = plt.subplots(figsize=(9.2, 3.2), facecolor="white")
    _bars(ax, labels, values, errors, notes, colors,
          "reroutes per run  \u2014  lower is calmer", "")
    fig.suptitle("E4  Route stability  \u2014  the reactive baseline churns 3x more than the full system.",
                 fontsize=11, color=INK, x=0.012, ha="left", y=0.985)
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    fig.savefig(out_dir / "e4_stability.png", dpi=200, facecolor="white")
    plt.close(fig)
    return True


def figure_e5(results: dict, out_dir: Path) -> bool:
    """E5: the ablation. Both halves of the trade, side by side.

    Reporting only reroutes would overstate the gates; reporting only
    detection would overstate their cost. The two panels are the result.
    """
    on, off = results.get("E5/P2_ML_RL"), results.get("E5/P2_ML_RL_nogates")
    if not on or not off:
        return False
    labels = ["gates ON\n(persistence + hysteresis)", "gates OFF\n(ablated)"]
    colors = [ACCENT, ACCENT_2]

    fig, axes = plt.subplots(1, 2, figsize=(11, 3.4), facecolor="white")
    _bars(axes[0], labels,
          [on["reroute_count"]["mean"], off["reroute_count"]["mean"]],
          [on["reroute_count"].get("ci95"), off["reroute_count"].get("ci95")],
          [f"{on['reroute_count']['mean']:.1f}", f"{off['reroute_count']['mean']:.1f}  \u2014  7x more"],
          colors, "reroutes per run", "What the gates prevent")
    _bars(axes[1], labels,
          [on["detection_rate_in_fault"] * 100, off["detection_rate_in_fault"] * 100],
          [None, None],
          [f"{on['detection_rate_in_fault']*100:.0f}%  ({round(on['detection_rate_in_fault']*on['trials_usable'])}/{on['trials_usable']})",
           f"{off['detection_rate_in_fault']*100:.0f}%  ({round(off['detection_rate_in_fault']*off['trials_usable'])}/{off['trials_usable']})"],
          colors, "faults detected (%)", "What they cost")
    axes[1].set_xlim(0, 100)
    axes[1].set_yticklabels([])

    fig.suptitle("E5  Ablation  \u2014  removing the gates catches every fault, and thrashes.",
                 fontsize=11, color=INK, x=0.012, ha="left", y=0.985)
    fig.tight_layout(rect=(0, 0, 1, 0.88))
    fig.savefig(out_dir / "e5_ablation.png", dpi=200, facecolor="white")
    plt.close(fig)
    return True


def figure_e6(out_dir: Path) -> bool:
    """E6: decision quality, from the recorded inputs.

    P1 agreeing with the formula on every decision is the control for the
    method -- P1 IS the formula, so anything else would mean the replay
    was misreading the record. It is drawn, not just asserted.
    """
    path = REPO_ROOT / "results" / "tables" / "e6_decision_quality.json"
    if not path.exists():
        return False
    arms = json.loads(path.read_text())["arms"]
    p1, p2 = arms.get("P1_ML_HEURISTIC"), arms.get("P2_ML_RL")
    if not p1 or not p2:
        return False

    fig, ax = plt.subplots(figsize=(9.6, 3.0), facecolor="white")
    rows = [("P1  hand-tuned formula", p1, 0), ("P2  learned policy", p2, 1)]
    for label, r, y in rows:
        y = 1 - y
        total = r["decisions"] or 1
        segs = [("agreed with formula", r["agreed_with_formula"], GRID),
                ("chose a WIDER path", r["chose_wider"], ACCENT),
                ("chose a narrower path", r["chose_narrower"], ACCENT_2),
                ("same capacity, different path", r["chose_same_capacity"], INK_MUTED)]
        left = 0
        for name, n, col in segs:
            if not n:
                continue
            ax.barh(y, n, left=left, height=0.5, color=col, edgecolor="white", linewidth=1.2)
            if n / total > 0.07:
                ax.text(left + n / 2, y, str(n), va="center", ha="center", fontsize=9,
                        color="white" if col != GRID else INK)
            left += n
        ax.text(left + 0.6, y, f"  {r['decisions']} decisions", va="center", ha="left",
                fontsize=8.5, color=INK_MUTED)
    ax.set_yticks([1, 0]); ax.set_yticklabels([rows[0][0], rows[1][0]], fontsize=9, color=INK)
    ax.set_xlabel("routing decisions, replayed against the inputs each selector was shown",
                  fontsize=9, color=INK_MUTED)
    _style(ax); ax.margins(x=0.16)
    handles = [Patch(facecolor=c, edgecolor=c, label=n) for n, c in
               (("agreed with formula", GRID), ("chose a WIDER path", ACCENT),
                ("chose a narrower path", ACCENT_2), ("same capacity", INK_MUTED))]
    # Below the plot, not beside the title: the title is a full sentence
    # and a four-item legend on the same line overlapped it.
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.0),
               ncol=4, frameon=False, fontsize=8.5)
    fig.suptitle("E6  Decision quality  \u2014  where they differ, the policy picks the wider path 8x as often.",
                 fontsize=11, color=INK, x=0.012, ha="left", y=0.97)
    fig.tight_layout(rect=(0, 0.10, 1, 0.88))
    fig.savefig(out_dir / "e6_decision_quality.png", dpi=200, facecolor="white")
    plt.close(fig)
    return True


def figure_e7(results: dict, out_dir: Path) -> bool:
    """E7: behaviour across offered load, not at one operating point."""
    levels = [(1, "E7/P2_ML_RL_bg1"), (2, "E7/P2_ML_RL"), (6, "E7/P2_ML_RL_bg6")]
    pts = [(mb, results[k]) for mb, k in levels if k in results]
    if len(pts) < 2:
        return False
    x = [mb for mb, _ in pts]
    rate = [s["detection_rate_in_fault"] * 100 for _, s in pts]
    lat = [(s["detection_latency_in_fault_s"] or {}).get("mean") for _, s in pts]
    err = [(s["detection_latency_in_fault_s"] or {}).get("ci95") for _, s in pts]

    fig, axes = plt.subplots(1, 2, figsize=(10.6, 3.4), facecolor="white")
    ax = axes[0]
    ax.plot(x, rate, marker="o", color=ACCENT, linewidth=2)
    for xi, yi in zip(x, rate):
        ax.annotate(f"{yi:.0f}%", (xi, yi), textcoords="offset points", xytext=(0, 9),
                    ha="center", fontsize=8.5, color=INK)
    ax.set_ylim(0, 100); ax.set_xticks(x)
    ax.set_xlabel("offered background load per host pair (Mbit/s)", fontsize=9, color=INK_MUTED)
    ax.set_ylabel("faults detected (%)", fontsize=9, color=INK_MUTED)
    ax.set_title("Detection holds across a 6x load range", fontsize=10.5, color=INK, loc="left", pad=8)
    _style(ax); ax.yaxis.grid(True, color=GRID, linewidth=0.6)

    ax = axes[1]
    ax.errorbar(x, lat, yerr=[e or 0 for e in err], marker="o", color=ACCENT_2,
                linewidth=2, ecolor=INK_MUTED, elinewidth=1.2, capsize=3)
    for xi, yi in zip(x, lat):
        if yi is not None:
            ax.annotate(f"{yi:.0f}s", (xi, yi), textcoords="offset points", xytext=(0, 10),
                        ha="center", fontsize=8.5, color=INK)
    ax.set_xticks(x); ax.set_ylim(0, max(v for v in lat if v) * 1.45)
    ax.set_xlabel("offered background load per host pair (Mbit/s)", fontsize=9, color=INK_MUTED)
    ax.set_ylabel("time to detection (s)", fontsize=9, color=INK_MUTED)
    ax.set_title("But it notices roughly twice as slowly", fontsize=10.5, color=INK, loc="left", pad=8)
    _style(ax); ax.yaxis.grid(True, color=GRID, linewidth=0.6)

    fig.suptitle("E7  Sensitivity to offered load  \u2014  static routing detected 0 of 15 at every level.",
                 fontsize=11, color=INK, x=0.012, ha="left", y=0.985)
    fig.tight_layout(rect=(0, 0, 1, 0.88))
    fig.savefig(out_dir / "e7_load_sweep.png", dpi=200, facecolor="white")
    plt.close(fig)
    return True


def figure_e8(results: dict, out_dir: Path) -> bool:
    """E8: the safety claim is a count of zero, which is awkward to draw.

    Drawn as counts per trial with an explicit zero marker, because a bar
    chart of five zeroes is an empty frame that reads as missing data
    rather than as the result.
    """
    s = results.get("E8/P2_ML_RL")
    safety = (s or {}).get("safety")
    if not safety:
        return False
    per = safety["predictions_after_kill_per_trial"]
    fig, ax = plt.subplots(figsize=(9.2, 2.9), facecolor="white")
    ypos = list(range(len(per)))[::-1]
    for y, n in zip(ypos, per):
        if n == 0:
            ax.plot([0], [y], marker="|", markersize=16, color=ACCENT, markeredgewidth=2.5)
            ax.text(0.06, y, "  0 predictions after the model became unreachable",
                    va="center", ha="left", fontsize=9, color=ACCENT)
        else:
            ax.barh(y, n, height=0.5, color=ACCENT_2)
            ax.text(n + 0.06, y, f"  {n} FABRICATED", va="center", ha="left",
                    fontsize=9, color=ACCENT_2)
    ax.set_yticks(ypos)
    ax.set_yticklabels([f"trial {i+1}" for i in range(len(per))], fontsize=9, color=INK)
    ax.set_xlim(0, 3)
    ax.set_xlabel("predictions made after the inference service was killed", fontsize=9, color=INK_MUTED)
    _style(ax)
    fig.suptitle(f"E8  Safety under service failure  \u2014  {safety['fallbacks_after_kill']} "
                 "routing fallback recorded with a reason.",
                 fontsize=11, color=INK, x=0.012, ha="left", y=0.97)
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    fig.savefig(out_dir / "e8_safety.png", dpi=200, facecolor="white")
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

    parsed = json.loads(path.read_text())
    results = parsed["results"]
    by_fault = parsed.get("by_fault", {})
    out_dir = REPO_ROOT / "results" / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)

    drawn = []
    if figure_e1(results, out_dir):
        drawn.append("e1_comparison.png")
    if figure_ladder(results, out_dir):
        drawn.append("e1_ladder.png")
    if figure_e2(by_fault, out_dir):
        drawn.append("e2_by_fault.png")
    if figure_e3(by_fault, out_dir):
        drawn.append("e3_lead_time.png")
    if figure_e4(results, out_dir):
        drawn.append("e4_stability.png")
    if figure_e5(results, out_dir):
        drawn.append("e5_ablation.png")
    if figure_e6(out_dir):
        drawn.append("e6_decision_quality.png")
    if figure_e7(results, out_dir):
        drawn.append("e7_load_sweep.png")
    if figure_e8(results, out_dir):
        drawn.append("e8_safety.png")

    if not drawn:
        print("no experiment had enough recorded trials to draw yet")
        return 1
    for name in drawn:
        print(f"wrote {out_dir / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
