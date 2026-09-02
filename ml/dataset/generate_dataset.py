#!/usr/bin/env python3
"""Generates the labelled dataset for Phase 9's ML degradation predictor.

Runs >=8 seeded Mininet scenarios (each >=10 minutes), driving continuous
iperf3 background traffic and a randomized schedule of the 6
network/failure_injector.py fault types interspersed with fault-free
"normal" periods. After each run, every telemetry sample is converted to
a feature vector via common/features.py -- the SAME function Phase 10's
live inference will use -- and labelled via ml/dataset/label_rules.py
against that run's real ground truth. Labelled rows are written to
results/raw/dataset/run_<seed>.jsonl; a summary (seeds, per-run and
overall class balance) goes to results/tables/dataset_summary.json.

SPLIT BY RUN, NOT BY ROW: ml/train_model.py (Phase 9) must hold out whole
runs for its test split, never individual rows -- rows within one run are
temporally correlated (the same fault's effect spans many consecutive
windows), and row-wise splitting would leak that correlation across
train/test, inflating reported metrics. State this in the paper.

Must run under the VM's SYSTEM Python 3 (needs Mininet + root), same as
topo_builder.py/traffic_generator.py/failure_injector.py. Assumes a
controller (controller.main_app + controller.telemetry_collector) is
already running and reachable, exactly like those scripts -- this script
only builds the data-plane network and orchestrates traffic/faults.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.config_loader import load_config  # noqa: E402
from common.features import RawWindow, compute_base_metrics, compute_feature_vector  # noqa: E402
from common.schema import DataSource  # noqa: E402
from ml.dataset.label_rules import label_window  # noqa: E402
from network.failure_injector import FAULT_TYPES, FailureInjector  # noqa: E402

DEFAULT_RUN_DURATION_S = 600.0  # ">=10 minutes" per the phase plan
BACKGROUND_TRAFFIC_PAIRS = [("h1", "h6"), ("h2", "h5"), ("h3", "h4")]
BACKGROUND_UDP_MBIT = 2


def _build_fault_schedule(rng: random.Random, links: list[tuple[str, str]], duration_s: float) -> list[dict]:
    """A sequence of {gap_s, link, fault_type, fault_duration_s, params}
    events covering the whole run. Gaps are genuine fault-free ("normal")
    periods -- roughly half the run, by construction below -- since a
    dataset that is nothing but faults would not teach the model what
    healthy traffic looks like."""
    schedule = []
    elapsed = 0.0
    while elapsed < duration_s:
        gap_s = rng.uniform(20, 50)
        elapsed += gap_s
        if elapsed >= duration_s:
            schedule.append({"gap_s": gap_s, "link": None, "fault_type": None})
            break

        fault_type = rng.choice(FAULT_TYPES)
        link = rng.choice(links)
        fault_duration_s = rng.uniform(15, 40)
        elapsed += fault_duration_s

        params: dict = {}
        if fault_type == "congestion":
            params = {"bw_fraction": rng.uniform(0.1, 0.3)}
        elif fault_type == "packet_loss":
            params = {"loss_pct": rng.uniform(10, 35)}
        elif fault_type == "high_latency":
            params = {"delay_ms": rng.uniform(30, 80)}
        elif fault_type == "rising_errors":
            params = {"corrupt_pct": rng.uniform(5, 15)}
        elif fault_type == "gradual_degradation":
            params = {
                "steps": 5,
                "target_bw_fraction": rng.uniform(0.05, 0.2),
                "target_delay_ms": rng.uniform(30, 60),
            }

        schedule.append(
            {
                "gap_s": gap_s,
                "link": link,
                "fault_type": fault_type,
                "fault_duration_s": fault_duration_s,
                "params": params,
            }
        )
    return schedule


def _start_background_traffic(net, duration_s: float) -> None:
    """Fire-and-forget long-lived UDP iperf3 clients between several host
    pairs, so telemetry has continuous, non-zero load to actually reflect
    fault effects against. Not awaited: these run for the whole scenario
    in the background, in Mininet's own host network namespaces."""
    for client_id, server_id in BACKGROUND_TRAFFIC_PAIRS:
        client = net.get(client_id)
        server = net.get(server_id)
        server.popen(["iperf3", "-s", "-p", "5301", "-D"])
        client.popen(
            [
                "iperf3", "-c", server.IP(), "-p", "5301", "-u",
                "-b", f"{BACKGROUND_UDP_MBIT}M", "-t", str(int(duration_s) + 10),
            ]
        )


def _run_one_scenario(
    seed: int, topo_name: str, duration_s: float, controller_ip: str, controller_port: int,
    output_dir: Path, db_path: Path, config,
) -> dict:
    from mininet.log import setLogLevel

    from network.topo_builder import build_network, load_topology_spec

    setLogLevel("output")  # quiet; this runs 8 times for 10+ minutes each
    rng = random.Random(seed)

    spec = load_topology_spec(topo_name)
    links = [tuple(link["endpoints"]) for link in spec["links"]]
    schedule = _build_fault_schedule(rng, links, duration_s)

    ground_truth_path = output_dir / f"ground_truth_seed{seed}.jsonl"
    if ground_truth_path.exists():
        ground_truth_path.unlink()

    net = build_network(spec, controller_ip, controller_port, fail_mode="secure")
    net.start()
    net.pingAll()  # populate ARP/MAC tables before real traffic starts

    injector = FailureInjector(net, spec, ground_truth_path)
    run_start_ts = time.time()

    try:
        traffic_thread = threading.Thread(target=_start_background_traffic, args=(net, duration_s), daemon=True)
        traffic_thread.start()

        for event in schedule:
            time.sleep(event["gap_s"])
            if event["link"] is None:
                continue
            sw_a, sw_b = event["link"]
            injector.inject(sw_a, sw_b, event["fault_type"], event["fault_duration_s"], **event["params"])
    finally:
        run_end_ts = time.time()
        net.stop()

    fault_events = [json.loads(line) for line in ground_truth_path.read_text().splitlines() if line.strip()]

    link_capacities_bps = {
        "-".join(sorted(link["endpoints"])): link["bw"] * 1_000_000
        for link in spec["links"] if "bw" in link
    }

    windows_written, positive, negative = _extract_labelled_windows(
        db_path, links, run_start_ts, run_end_ts, fault_events, config, seed, output_dir,
        link_capacities_bps,
    )

    return {
        "seed": seed,
        "run_start_ts": run_start_ts,
        "run_end_ts": run_end_ts,
        "duration_s": run_end_ts - run_start_ts,
        "num_fault_events": sum(1 for e in fault_events if e["event"] == "fault_start"),
        "windows_written": windows_written,
        "positive": positive,
        "negative": negative,
    }


def _extract_labelled_windows(
    db_path: Path, links: list[tuple[str, str]], run_start_ts: float, run_end_ts: float,
    fault_events: list[dict], config, seed: int, output_dir: Path,
    link_capacities: dict[str, float],
) -> tuple[int, int, int]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    windows_written = 0
    positive = 0
    negative = 0

    out_path = output_dir / f"run_{seed}.jsonl"
    with open(out_path, "w") as out_f:
        for sw_a, sw_b in links:
            link_id = "-".join(sorted((sw_a, sw_b)))
            rows = conn.execute(
                "SELECT * FROM telemetry WHERE link_id = ? AND timestamp BETWEEN ? AND ? "
                "ORDER BY switch_id, timestamp ASC",
                (link_id, run_start_ts, run_end_ts),
            ).fetchall()

            # Base metrics are derived per (switch_id, port) time series
            # (one side of the link at a time), matching how
            # telemetry_collector.py polls each switch independently.
            by_switch: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_switch.setdefault(row["switch_id"], []).append(row)

            for switch_id, switch_rows in by_switch.items():
                history: list[RawWindow] = []
                for i in range(1, len(switch_rows)):
                    prev, curr = switch_rows[i - 1], switch_rows[i]
                    dt_s = curr["timestamp"] - prev["timestamp"]
                    capacity_bps = link_capacities.get(link_id)
                    base = compute_base_metrics(dict(prev), dict(curr), dt_s, capacity_bps)
                    if base is None:
                        continue
                    history.append(base)

                    fv = compute_feature_vector(
                        history, len(history) - 1,
                        windows=config.features.windows,
                        ewma_alpha=config.features.ewma_alpha,
                        baseline_windows=config.features.baseline_healthy_windows,
                    )
                    label = label_window(curr["timestamp"], link_id, fault_events, config.label.horizon_s)

                    out_f.write(
                        json.dumps(
                            {
                                "seed": seed, "link_id": link_id, "switch_id": switch_id,
                                "timestamp": curr["timestamp"], "features": fv, "label": label,
                                "data_source": DataSource.EMULATED_MININET.value,
                            }
                        )
                        + "\n"
                    )
                    windows_written += 1
                    positive += label
                    negative += 1 - label

    conn.close()
    return windows_written, positive, negative


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topo", default="scenario_a")
    parser.add_argument("--duration", type=float, default=DEFAULT_RUN_DURATION_S)
    parser.add_argument("--seeds", type=int, nargs="+", default=None)
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "results" / "raw" / "dataset"))
    args = parser.parse_args(argv)

    config = load_config()
    seeds = args.seeds or list(config.seeds.dataset_runs)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = REPO_ROOT / config.database.path

    run_summaries = []
    for seed in seeds:
        print(f"=== dataset run seed={seed} (duration={args.duration}s) starting at {time.strftime('%H:%M:%S')} ===", flush=True)
        summary = _run_one_scenario(
            seed, args.topo, args.duration, args.controller_ip, args.controller_port,
            output_dir, db_path, config,
        )
        run_summaries.append(summary)
        print(f"=== seed={seed} done: {summary} ===", flush=True)

    total_positive = sum(s["positive"] for s in run_summaries)
    total_negative = sum(s["negative"] for s in run_summaries)
    total_windows = total_positive + total_negative

    report = {
        "topo": args.topo,
        "num_runs": len(run_summaries),
        "seeds": seeds,
        "target_run_duration_s": args.duration,
        "total_windows": total_windows,
        "total_positive": total_positive,
        "total_negative": total_negative,
        "positive_fraction": (total_positive / total_windows) if total_windows else None,
        "runs": run_summaries,
        "data_source": DataSource.EMULATED_MININET.value,
    }

    tables_dir = REPO_ROOT / "results" / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    with open(tables_dir / "dataset_summary.json", "w") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
