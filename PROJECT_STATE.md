# PROJECT_STATE

Authoritative status. Updated every phase boundary, same commit as the code.
Decisions recorded here are settled and are not revisited.

## Environment (fixed 2026-08-30)

- **OS:** macOS 14.5 (Darwin 23.5.0), Apple Silicon-class Homebrew at
  `/opt/homebrew`. Not Linux — cannot run Mininet/OVS natively.
- **Sudo:** yes, the development account is sudo-capable.
- **RAM / cores:** 8 GB RAM / 8 cores.
- **Python already available on the dev machine:** `python3.11` at
  `/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11`
  (3.11.9) — used directly for `venv-ml`.
- **Linux VM:** Multipass `sdn-vm`, Ubuntu 22.04.5 LTS, 4 vCPU / 4GB RAM
  allocation (3.8GB usable), 20GB disk. Repo is delivered into it via
  `multipass transfer -r` snapshots (NOT a live mount — both mount types
  proved unreliable, see `docs/decisions.md` 2026-08-31); re-run after any
  Mac-side edit that needs VM-side testing:
  `multipass transfer -r <path> sdn-vm:/home/ubuntu/ai-self-healing-sdn/<path>`.
- **venv-ctrl:** Python 3.9.25, lives at
  `$HOME/venv-ctrl-ai-self-healing-sdn` inside the VM (outside the repo
  directory — see decisions.md). Built by `scripts/setup_env.sh ctrl`.
- **venv-ml:** Python 3.11.9, `<repo>/venv-ml` on macOS (used for
  training/dataset generation — Phases 8-9). Built by
  `scripts/setup_env.sh ml`. A SECOND `venv-ml` (Python 3.11.15) now
  also exists inside `sdn-vm` at `<repo>/venv-ml`, added Phase 10:
  `service/ml_rl_service.py` must be reachable via `localhost` from the
  controller, which runs in the VM, so the inference service runs there
  too — see decisions.md (2026-09-03).
- `project.controller_backend: ryu` in `config/config.yaml` (decided
  Phase 2 GO/NO-GO smoke test — no os-ken fallback needed).

## Current phase

**Phase 21 — COMPLETE (with a measured caveat).** The closed loop runs
end to end against a live network: fault -> detect -> reroute (RL) ->
verify -> incident closed SUCCESS, in ~18s. Verified on 3 consecutive
live runs; **2 of 3 passed**, the third opened no incident. That
reliability figure is NOT yet a claim -- 3 runs is far too small a
sample, and Phase 22 is where it gets measured across >=5 seeds. Next:
Phase 22 (the 8 experiments).

## Completed components

**Phase 1 — repo skeleton, config, logging, DB:**
- `config/config.yaml` — full tunable set (telemetry, features, label, ml,
  persistence, stability, path, rl, flow, verify, remediation, traffic,
  database, api, seeds).
- `common/config_loader.py` — frozen dataclasses, `load_config()`,
  `ConfigError` on missing/malformed sections, validates
  `uncertainty_band` ordering.
- `common/logging_setup.py` — stdlib logging, one-JSON-object-per-line
  formatter, `configure_logging(name)`.
- `common/schema.py` — shared enums (LinkHealthState, IncidentStatus,
  VerificationClassification, RouteSelector, DataSource, FlowRuleResult).
- `database/schema.sql` — all 11 tables + indexes on (timestamp),
  (link_id, timestamp), (incident_id).
- `database/db.py` — WAL-mode sqlite3 connection helper, `init_schema()`,
  `Database` context manager with generic `insert`/`query`/`execute`.
- `pytest.ini`, `Makefile`, `requirements-ctrl.txt`, `requirements-ml.txt`,
  `.gitignore`, `README.md`, `docs/decisions.md`,
  `docs/limitations.md`.

**Phase 2 — env setup, GO/NO-GO smoke test, of_compat:**
- `scripts/setup_env.sh` — reproducible setup for both venvs (`ml`, `ctrl`,
  `all` subcommands). `ctrl` works around a real Ryu 4.34 packaging bug
  (pbr reports version 0.0.0 outside a git checkout) by building the wheel
  manually with `PBR_VERSION=4.34` and a pinned `setuptools<60`; installs
  `dnspython==1.16.0` (not 2.2.1 — conflicts with eventlet 0.30.2's own
  `dnspython<2.0.0` requirement) plus ryu's full declared dependency set.
  Verified reproducible from a clean run (ran twice independently).
- `controller/of_compat.py` — the single ryu/os_ken import point;
  `tests/unit/test_of_compat.py` (AST-based hygiene check) passes under
  venv-ml with no ryu installed, and separately verified against the real
  venv-ctrl (`of_compat backend: ryu`).
- **GO/NO-GO smoke test: PASS, `ryu` backend.** `ryu-manager
  ryu.app.simple_switch_13` + real Mininet pingall → 0% dropped
  (6/6 received).
- Mininet, Open vSwitch, iperf3, python3-yaml installed in the VM via apt.
- `Makefile` `setup-ml`/`setup-ctrl` call `scripts/setup_env.sh`;
  `test-ctrl` checks `of_compat` against the real ryu install (pytest is
  not part of the approved venv-ctrl stack).

**Phase 3 — topology builder + traffic generator:**
- `network/topologies/scenario_a.yaml` — 6 switches, 6 hosts (one per
  switch), partial mesh giving h1->h6 4 loop-free routed paths of
  differing cost: s1-s2-s3-s6, s1-s4-s5-s6, s1-s3-s6, s1-s2-s5-s6
  (bw 5-10 Mbit, delay 1-5ms per link).
- `network/topo_builder.py` — generic YAML-driven Mininet builder
  (`load_topology_spec`, `build_network`); new scenarios are data, not
  code. `--fail-mode standalone` (default) enables RSTP and waits for
  convergence so pingall works without an SDN controller running yet;
  `--fail-mode secure` is for Phase 4 onward.
- `network/traffic_generator.py` — `start_iperf_server`/`run_iperf_client`/
  `summarize_result` library functions (reused by later dataset-generation
  and experiment phases) plus a standalone CLI.
- Found and fixed a real bug this phase: `scenario_a`'s redundant links
  form L2 cycles by design, so OVS's learning-switch fallback storms
  without RSTP (`topo_builder.enable_rstp_and_wait`) — see
  `docs/decisions.md` (2026-08-31).
- **Gate result:** `sudo python3 network/topo_builder.py --topo scenario_a
  --test pingall` → **0% dropped (30/30 received)**. iperf3 sanity check
  h1->h6: **9.27 Mbps throughput, 0 retransmits** (real measured
  `iperf3 -J` output).

**Phase 4 — Ryu L2 forwarding + topology discovery:**
- `controller/topology_manager.py` — Ryu app context (loaded via
  `L2Forwarding._CONTEXTS`) that listens to `ryu.topology.event`
  (requires `ryu-manager --observe-links`), maintains a `networkx.Graph`
  of the live topology, recomputes a minimum spanning tree on every
  change, and persists a `topology_snapshots` row (`source='discovered'`)
  each time. Exposes `get_all_ports`/`is_inter_switch_port`/
  `get_inter_switch_flood_ports` for loop-safe flooding.
- `controller/main_app.py` (`L2Forwarding`) — standard MAC-learning L2
  switch: table-miss flow, learns `src MAC -> (dpid, port)`, installs an
  exact-match flow once a destination MAC is known, and floods only when
  it's not. Flooding is restricted to host-facing ports plus
  spanning-tree-safe inter-switch ports (via TopologyManager) so the
  mesh topology's real L2 cycles don't storm — same underlying problem
  Phase 3 hit with OVS standalone/RSTP, solved controller-side here since
  the controller now decides forwarding.
- **Gate result — both conditions met:**
  - `sudo python3 network/topo_builder.py --topo scenario_a --fail-mode
    secure --test pingall` (real controller, no OVS RSTP fallback) →
    **0% dropped (30/30 received)**.
  - Discovered topology snapshot (queried from `database/network.db`,
    the fully-converged one, not the post-teardown empty one) has
    **6 switches (dpid 1-6) and 8 links**, and every edge exactly matches
    `network/topologies/scenario_a.yaml`'s link list (s1-s2, s1-s3,
    s1-s4, s2-s3, s2-s5, s3-s6, s4-s5, s5-s6).

**Phase 5 — telemetry collector + DB writes + latency validation:**
- `controller/telemetry_collector.py` — polls `OFPPortStatsRequest` every
  `telemetry.interval_s` for all connected switches, writes raw counters
  to `telemetry` (data_source=emulated_mininet); computes per-port
  byte-rate deltas, rejecting negative deltas from counter resets.
  Independently measures `OFPEchoRequest` RTT per switch. Sends a
  timestamped probe frame (EtherType `common.schema.PROBE_ETH_TYPE`) out
  each discovered link, both directions, every interval; on the matching
  PacketIn at the far end, estimates one-way latency as
  `elapsed - 0.5*echo_rtt` and writes it to `features`
  (`latency_is_estimate=1`, `window_s=0` — a raw sample, not yet a real
  windowed feature; that's Phase 6). `controller/main_app.py` and
  `of_compat.py` (added `DEAD_DISPATCHER`) updated accordingly.
- **Two real bugs found and fixed during this phase** (see
  `docs/decisions.md` 2026-08-31 for full detail): (1) `_probe_loop`
  crashed permanently on its first tick (`networkx.Graph` has no `.get()`
  method) with no visible symptom besides zero latency rows — fixed, and
  all three background loops now share a `_run_forever` wrapper so one
  bad iteration can't silently kill a loop again; (2) the probe's
  timestamp trailer was being read from the wrong byte offset because
  Ryu's `packet.Packet.serialize()` pads bare Ethernet frames to the
  60-byte minimum — fixed by reading the trailer from the end of the
  frame instead of a fixed offset.
- **Gate result:** telemetry rows grew continuously (308 rows over a
  ~20s run), deltas sane (byte counts match symmetrically between both
  ends of the same link). Latency probe validated against real `ping`:
  224 real probe rows written; **found and documented** (not hidden) that
  the estimate runs ~15-20ms average vs. ~4-8ms real ping RTT, dominated
  by controller-side eventlet scheduling overhead rather than physical
  link delay — see `docs/limitations.md` for the full measured table and
  its practical consequence (use relative changes, not absolute values,
  in later phases).

**Phase 6 — shared feature pipeline:**
- `common/features.py` — pure Python + numpy (no pandas; imports
  unchanged under venv-ctrl's Python 3.9, verified directly), so it's the
  one place both ML training (batch, Phase 9) and live controller
  inference (incremental, Phase 10) compute feature vectors.
  `compute_base_metrics()` derives utilization/loss_rate/error_rate/
  packet_rate from two consecutive raw telemetry counter rows (formulas
  documented in the module docstring, matching docs/architecture.md's
  eventual consolidated version); returns `None` (not zeroed fields) on
  a counter reset or non-positive `dt`. `compute_feature_vector()` takes
  a link's full RawWindow history plus an index and computes, using only
  `history[:index+1]`: rolling mean/std/linear-fit-slope over windows
  (3, 5, 10) for utilization/loss_rate/latency_ms/error_rate, first
  difference, deviation from a per-link EWMA baseline fixed from the
  first `baseline_windows` samples, and the current packet_rate — 45
  feature keys total. Every entry that needs more history than exists is
  explicitly `None`, never silently 0 (enforced by test).
- **Gate result:** `tests/unit/test_features.py::
  test_train_and_live_paths_produce_identical_feature_vectors` — builds
  25 raw samples, computes feature vectors via a batch/training call
  pattern and an incremental/live call pattern, asserts every single
  vector is identical at every index. **Green.** 13/13 feature tests
  passing (base-metric derivation, counter-reset handling, rolling
  stats correctness against manual computation, NaN policy, EWMA
  baseline, linear-fit slope on a known-linear series).
- Full `requirements-ml.txt` stack now installed in `venv-ml` (numpy,
  pandas, scikit-learn, matplotlib, gymnasium, stable-baselines3,
  sb3-contrib, fastapi, uvicorn, joblib) — needed starting this phase,
  installed once now rather than piecemeal per future phase.

**Phase 7 — failure injector:**
- `network/failure_injector.py` — `FailureInjector` class applying real
  Mininet/tc-level faults to a specific link (never a fabricated
  telemetry value written directly to the DB): `congestion` (bw reduced
  to a fraction of capacity), `gradual_degradation` (bw/delay ramped
  linearly across N sub-steps), `packet_loss` (tc netem loss%),
  `high_latency` (tc netem delay), `rising_errors` (tc netem corrupt%),
  `link_failure` (`net.configLinkStatus` down/up). Every injection and
  recovery writes a JSON-lines ground-truth record (event, link_id,
  fault_type, timestamps, applied params) — this is what Phase 9's
  dataset generator will use to label windows, never anything else.
  Runs under system Python 3 (needs Mininet), same as topo_builder.py.
- **Gate result — verified against real evidence for all 6 fault types**
  (run in `sdn-vm`, ground truth JSONL inspected directly): `high_latency`
  (Mininet confirms "(60ms delay)" applied), `packet_loss` 30% (mid-fault
  ping showed 50% RTT loss, matching the compound round-trip math
  1-(1-0.3)²≈51% almost exactly), `congestion` (iperf3 achieved only
  1.13Mbps against a 1.5Mbit target cap from a 10Mbit link),
  `link_failure` (100% ping loss during, 0% after recovery),
  `gradual_degradation` (Mininet's own status line shows a clean 5-step
  ramp: 8.2→6.4→4.6→2.8→1.0 Mbit, 10→20→30→40→50ms delay). All 6 fault
  types plus recovery correctly logged in the ground-truth file.
- **Found and honestly documented a real limitation** (not hidden):
  `rising_errors`' corruption is genuinely applied (confirmed via a
  massive real throughput drop during the fault window) but never
  registers in OVS's `rx_errors`/`tx_errors` counters, because Mininet's
  veth-based links don't perform the hardware-level FCS/CRC check a real
  NIC does. Confirmed across the whole test run: `SUM(rx_errors)=0,
  SUM(tx_errors)=0` (638 telemetry rows) vs. `packet_loss`'s effect,
  which IS visible (`SUM(rx_dropped)=232`). Full detail and its
  consequence for Phase 9's ML training in `docs/limitations.md`.

**Phase 8 — dataset generator:**
- `ml/dataset/label_rules.py` — `label_window()`: label=1 iff a link has
  a `fault_start` ground-truth event with
  `window_ts < start_ts <= window_ts + label.horizon_s` (10s default) —
  pure stdlib, one canonical labeling rule.
- `ml/dataset/generate_dataset.py` — orchestrates >=8 seeded 10-minute
  Mininet runs: builds `scenario_a`, starts continuous background UDP
  traffic (iperf3) between 3 host pairs, executes a randomized fault
  schedule (`_build_fault_schedule`, alternating ~20-50s fault-free gaps
  with all 6 `failure_injector.py` fault types), then post-processes that
  run's raw `telemetry` rows through `common/features.py` (the SAME
  function live inference will use, per Phase 6) and
  `label_rules.label_window()`, writing labelled rows to
  `results/raw/dataset/run_<seed>.jsonl`. Runs under system Python 3
  (needs Mininet), same as the other `network/` scripts; assumes the
  controller (`main_app` + `telemetry_collector`) is already running.
- **Found and fixed a real bug during validation** (short test run
  before committing to the full 8x600s generation): `link_capacities`
  was declared but never populated, so every `utilization`-derived
  feature was silently `None` for the whole dataset. Fixed by deriving
  `bw`(Mbit)→bps from the topology spec before feature extraction;
  re-validated with real non-None utilization values afterward.
- **Gate result — real 8-run generation, ~90 minutes wall-clock**
  (seeds 1-8, `scenario_a`, `--duration 600` each, actual per-run
  duration 605-644s including fault overshoot): **39,248 total windows,
  760 positive, 38,488 negative (positive_fraction ≈ 1.94%)**. Per-run:
  9-10 fault events and 90-100 positive windows each (seed-dependent,
  expected — not a bug). Full breakdown in
  `results/tables/dataset_summary.json` (committed; the raw
  `results/raw/dataset/run_*.jsonl` files, ~7-8MB each, live only in the
  VM and are regenerated via `sudo python3 ml/dataset/generate_dataset.py
  --duration 600`, per the existing `.gitignore` policy for
  `results/raw/*`).
  **Superseded 2026-09-03:** this exact dataset was regenerated with a
  fault-injector redesign (see Phase 9) after ML training on it revealed
  a real methodological problem; current numbers are 39,233 windows, 640
  positive (1.63%) — see Phase 9 below. This entry is kept accurate to
  what Phase 8 itself produced at the time.
- **Class imbalance is real and expected, not a flaw to fix by
  resampling silently:** ~1.94% positive rate reflects that most of a
  run is genuinely healthy traffic — Phase 9's ML training must account
  for this (class weighting, and PR-AUC as a primary metric alongside
  ROC-AUC per the original ML spec) rather than treating it as a bug.

**Phase 9 — ML train + evaluate:**
- `ml/train_model.py` — loads the Phase 8 dataset SPLIT BY RUN (train
  seeds 1-6, test seeds 7-8, fixed and documented, not re-randomized).
  Trains all 3 approved candidates (HistGradientBoosting, RandomForest,
  LogisticRegression baseline; no XGBoost) with `sample_weight='balanced'`
  for the ~1.94% positive class; HistGradientBoosting uses NaN features
  natively, RandomForest/LogisticRegression go through a
  `SimpleImputer(median)` pipeline (documented, not a silent fillna(0)
  inside `common/features.py`). Selects the best by PR-AUC (far more
  informative than ROC-AUC under this much imbalance). Reports precision/
  recall/F1/ROC-AUC/PR-AUC/confusion matrix at `config.ml.threshold`
  (justified: matches the live decision logic's operating point, not
  re-optimized after the fact), permutation feature importance (not
  impurity) for the chosen model, and a calibration curve. Serializes to
  `models/degradation_model.joblib` + `models/metadata.json` (sklearn
  version, feature order, train/test seeds, git commit).
  `ml/evaluate_model.py` independently reloads the saved model and
  reproduces the same metrics (used to verify save/load reproducibility
  — confirmed byte-identical numbers).
- **Found and fixed a real bug before training could even run:**
  `latency_ms` was 100% NaN across the whole dataset — `generate_dataset.py`
  never joined the `features` table's probe-latency samples into the
  telemetry-counter-derived `RawWindow` history it built. Fixed with a
  nearest-in-time join (`_nearest_latency_ms`); re-extracted all 8 runs
  directly from the VM's still-intact database (not a full 90-minute
  Mininet re-run) — verified byte-identical window/label counts to the
  original run, confirming the fix only fills in latency, changes
  nothing else.
- **Confirmed and corrected a real limitation:** `loss_rate`/`error_rate`
  are exactly 0.0 for all rows (confirmed on both the original 39,248
  -row and the later ramped 39,233-row dataset — OVS port counters don't
  reflect
  `tc netem`-injected loss on veth links — same root cause as Phase 7's
  `rising_errors` finding, now confirmed for `packet_loss` too; an
  earlier Phase 7 claim that `packet_loss` WAS visible via `rx_dropped`
  is corrected in `docs/limitations.md` — it was a misread of a
  cumulative counter). `train_model.py` detects and excludes these 22
  zero-variance features explicitly (reported in
  `dropped_zero_variance_features`, not silently dropped or hidden).
- **First result was near-chance (ROC-AUC 0.37-0.56, PR-AUC 0.016-0.024,
  0 true positives at threshold=0.75) — investigated rather than
  accepted:** root cause was that `failure_injector.py`'s faults were
  instantaneous step functions with no organic pre-fault ramp, so the
  label's anticipation window was statistically indistinguishable from
  any healthy window.
- **Fixed (2026-09-03):** `failure_injector.py` redesigned so every
  fault type ramps toward its target intensity over `ramp_in_s`
  (12s, 18-25s for `gradual_degradation`) BEFORE `fault_start` is
  logged. Verified the ramp produces real signal (queried
  `features.latency_ms` before an actual fault: climbed ~6-10ms →
  ~29-32ms, a real trend) before spending ~2 hours regenerating the
  full 8-seed dataset (39,233 windows, 640 positive, 1.63%) and
  retraining.
- **Gate result — real metrics, honestly reported:** **ROC-AUC 0.83-0.84,
  PR-AUC 0.37-0.38** (up from 0.02-0.024, a 15-18x improvement).
  RandomForest (chosen): precision 0.837 / recall 0.225 / F1 0.355 at
  threshold=0.75 (confusion matrix `[[9627,7],[124,36]]` — 36 true
  positives, only 7 false positives). HistGradientBoosting: more
  balanced at 0.426/0.362/0.392. LogisticRegression baseline stays weak
  (ROC-AUC 0.565) even with real signal present — a useful negative
  result showing the relationship isn't simply linear, justifying the
  tree-based candidates. Permutation feature importance confirms the
  model is using the engineered ramp trend
  (`latency_ms_slope_10`/`slope_5` are the top 2 features), not an
  unrelated artifact — a coherent, explainable result. Full before/after
  numbers and the ramp's methodological caveat (a designed precursor,
  not a claim about real-world failure dynamics) in
  `docs/limitations.md`. Real figures saved to `results/figures/ml_*.png`.

**Phase 10 — inference service + controller client + fallback:**
- `service/ml_rl_service.py` — FastAPI service (runs under a NEW `venv-ml`
  now also installed inside `sdn-vm`, see decisions.md — the controller
  needs `localhost` HTTP reachability and it runs in the VM, not the
  Mac) loading `models/degradation_model.joblib` + `metadata.json` once
  at startup. `POST /infer/degradation` returns a real probability from
  the trained model; `GET /health` reports load status. Returns 503
  (never a fabricated probability) if the model failed to load. Phase
  18's RL route endpoint will be added to this same process later, per
  the repo layout — one service, two trained artefacts.
- `controller/inference_client.py` — HTTP client under venv-ctrl (no
  scikit-learn there by design). The safety gate: any failure (timeout,
  connection refused, non-200, malformed body, out-of-range probability)
  returns `None`, never a substituted or last-known-good value; callers
  must treat `None` as "no prediction this round."
- `controller/telemetry_collector.py` now runs the real live prediction
  loop: each port-stats poll on a discovered link is converted to a
  `RawWindow` (via `common/features.py`, merging in the latest latency
  probe), appended to a bounded per-(link, switch-side) history matching
  `ml/dataset/generate_dataset.py`'s granularity exactly, turned into a
  feature vector, sent to the inference client, and (only on success)
  written to the `predictions` table.
- Fixed `network/topo_builder.py`'s Mininet imports (moved from
  module-level to function-local): `load_topology_spec()` is now
  importable from venv-ctrl (no mininet installed there) so
  `telemetry_collector.py` can use it to get link capacities. Verified
  `load_topology_spec` still works standalone and all other
  `network/*.py` scripts still import correctly.
- **Gate result — verified both halves in `sdn-vm` with the real
  service, real controller, real Mininet:**
  - Service running: **320 real predictions** written to `predictions`,
    all correctly low (0.0-0.037) for a healthy network with no faults
    injected — sane, not fabricated.
  - Service killed mid-run: **112 graceful fallback warnings** logged,
    prediction count stayed exactly at 320 (zero fabricated
    predictions), zero crashes/tracebacks, controller kept running
    normally throughout.

**Phase 11 — path computation:**
- `controller/path_computer.py` — pure Python + networkx (no ryu/mininet
  dependency; verified importing under both venv-ml and the real
  venv-ctrl). Deliberately two separate steps:
  `generate_candidate_paths()` returns up to `path.k_shortest` simple
  (loopless) paths via `nx.shortest_simple_paths`, shortest-hop-count
  first, independent of current traffic so candidates stay stable while
  telemetry is noisy; `compute_path_cost()` scores ONE candidate against
  current per-link `LinkMetrics` (max utilization and max loss along the
  path — a bottleneck link dominates, so summing would understate it —
  plus summed latency and hop count, weighted per `path.weights`).
  `rank_candidate_paths()` combines both, sorted ascending (best first).
  A link with no metrics yet defaults to zero contribution — an
  explicit, documented "assume healthy until proven otherwise" choice,
  not a silent one; Phase 12's persistence/stability logic is what
  actually gates any reroute decision, not this function.
- **Gate result:** 14/14 new unit tests green, including
  `test_candidates_exist_for_every_switch_pair` (all 30 ordered pairs
  across `scenario_a`'s 6 switches) and a direct re-verification that
  h1->h6 (s1->s6) really does have 4 distinct loop-free candidates, not
  just that the topology supports them in principle.

**Phase 12 — persistence tracker + stability manager:**
- `controller/persistence_tracker.py` — `PersistenceTracker` implements
  decision-logic items 1-3: a per-link sliding window (deque,
  maxlen=`persistence.window`) of recent P(degradation) readings.
  `evaluate()` checks the LATEST reading against
  `persistence.uncertainty_band` first (abstention overrides everything
  else), then counts how many of the window's readings are >= threshold,
  requiring both the latest reading and the count to qualify
  (`persistence.consecutive`) — "3 of the last 4" per the literal spec
  wording. Verified independent per-link tracking, window eviction, and
  the `consecutive > window` misconfiguration rejected at construction.
- `controller/stability_manager.py` — `StabilityManager` implements
  items 4-6: cooldown, minimum improvement (a fraction of current path
  cost), and route-hold, checked against a per-flow last-change
  timestamp. `evaluate()` returns a `Decision(change, reason)` for
  EVERY call, including negatives — never raises or skips silently, so
  a caller logging every returned Decision satisfies the spec's logging
  requirement. Both pure Python, no framework dependency; verified
  importing under both venv-ml and the real venv-ctrl.
- **Gate result — the oscillation test:** simulates a flow whose
  degradation signal repeatedly re-qualifies (persistent + real
  improvement available) every 5s for 30s straight — exactly what would
  flap a naive controller. Only the FIRST evaluation changes the route;
  all 5 subsequent ones are refused (cooldown, then route-hold), each
  with a real logged reason, not a silent no-op. Confirmed this is
  temporary suppression, not a permanent lockout: once `route_hold_s`
  genuinely elapses, a still-qualifying signal is allowed to reroute
  again. 19/19 new tests green (9 persistence, 10 stability/oscillation).

**Phase 13 — flow manager + remediation manager:**
- `controller/flow_manager.py` — `install_path()` sends a real OFPFlowMod
  + OFPBarrierRequest per hop (match: `in_port`, `eth_type=0x0800`,
  `ipv4_src`, `ipv4_dst`; priority `flow.priority_remediated`;
  `idle_timeout`/`hard_timeout` from config; cookie = `incident_cookie()`,
  a deterministic hash of the incident id), and logs every attempt —
  success or failure — to `flow_rules`. Deliberately takes fully-resolved
  `PathHop` (dpid + in_port + out_port) rather than a bare switch list:
  resolving ports is the caller's job, not this primitive's.
- `controller/remediation_manager.py` — `remediate()` ranks candidate
  paths (`path_computer.py`), excludes the currently-failing path, and
  installs the best alternative; resolves host-facing ports at the two
  path ends from a caller-supplied `FlowContext` and inter-switch ports
  from the topology graph. Every decision — including "no alternate
  path available" — is recorded in `route_changes`.
- **Found and fixed a serious real bug via live VM verification, not
  just unit tests:** `incident_cookie()`'s full unsigned-64-bit hash
  overflows SQLite's signed-64-bit `INTEGER` column about half the time;
  with no per-hop exception isolation, that `OverflowError` aborted
  `install_path()`'s entire loop after hop 1 — a 4-switch path silently
  ended up with a flow on only its FIRST switch, while every hop still
  reported "INSTALLED". Fixed by masking the cookie to 63 bits AND
  isolating each hop's install-and-log in its own try/except (defense
  in depth — the cascade-on-one-hop-failure fragility was the more
  dangerous half of the bug). Re-verified live after the fix: all 4 hops
  now correctly flowed. 2000-iteration regression test added for the
  cookie range, plus a test simulating the exact original failure mode.
- **Gate result:** real `ovs-ofctl dump-flows` output for a live
  h1->h6 remediation (forced away from a congested s1-s3-s6, choosing
  s1-s4-s5-s6): all 4 switches show correct, consistent flow entries —
  same cookie throughout, `priority=200`, `hard_timeout=300`, matching
  `nw_src=10.0.0.1,nw_dst=10.0.0.6`, and a coherent hop-by-hop action
  chain (s1 in:1→out:3, s4 in:2→out:3, s5 in:2→out:3, s6 in:3→out:1).
  8/8 new flow_manager tests + 5/5 new remediation_manager tests green.

**Phase 14 — verifier + retry and unresolved handling:**
- `controller/verifier.py` — `classify()` compares post-remediation
  latency/loss against a pre-remediation `Baseline` plus
  `verify.tolerance`, returning SUCCESS (both metrics within tolerance),
  PARTIAL (one metric only), or FAILED (neither, or no telemetry
  available at all — an explicit case, not silently treated as success).
  Zero-baseline metrics (e.g. a healthy link's 0% loss) fall back to
  treating the tolerance fraction as an absolute ceiling instead of a
  relative one, since "0 * 1.2 = 0" would impossibly demand exact
  perfection. `Verifier.verify_and_retry()` orchestrates
  remediate→settle→measure→classify, retrying with the next candidate
  path (excluding every path already tried this incident) up to
  `remediation.max_retries` times, then marks the incident UNRESOLVED
  and stops. `remediate_fn`/`measure_fn`/`sleep_fn` are injected, so the
  full retry loop is testable without a live network or real delays.
- Updated `remediation_manager.remediate()`'s signature from a single
  `current_path` to `excluded_paths: list[list[int]]`, since the
  verifier's retries need to exclude every previously-tried path, not
  just the original failing one.
- **Gate result — `test_forced_failure_ends_unresolved_not_a_loop`:**
  every verification attempt is forced to FAIL, and a fresh candidate
  path is always available (never runs out) — exactly the scenario that
  would spin a naive retry-forever controller indefinitely. Confirmed:
  `remediate_fn` is called EXACTLY `max_retries+1` (3) times, never
  more; the incident ends UNRESOLVED, not stuck OPEN or looping; every
  attempt is recorded in `verification_results`, not silently dropped.
  11/11 new tests green (5 `classify()`, 6 retry-orchestration
  including the gate test).

**Phase 15 — baselines B1 and B2:**
- `controller/baselines/shortest_path.py` (B1) — `ShortestPathBaseline`
  computes a single `nx.shortest_path` per flow and NEVER reroutes for
  degradation/congestion; only recomputes if the currently-installed
  path is no longer physically valid in the topology graph (a real
  failure), the same way ordinary L3 routing would react. No metrics
  input at all — represents "no self-healing" as the comparison study's
  floor.
- `controller/baselines/rule_based.py` (B2) — `RuleBasedBaseline`
  reroutes only when a link's CURRENT (not predicted, not persisted)
  utilization/loss exceeds a fixed threshold
  (`baselines.b2_utilization_threshold`/`b2_loss_threshold`, new config
  section — nothing hardcoded). Isolates simple reactive thresholding
  from prediction+persistence (P1), which is the whole point of having
  it as a separate baseline. Reuses `path_computer.py`'s candidate
  ranking for path selection once triggered; made
  `link_id_for_edge()` a public helper there since both this and
  `path_computer.py` itself need it.
- **Gate result —**`test_both_baselines_run_end_to_end_on_the_same_
  scenario`: runs B1 and B2 through the identical sequence of
  conditions (healthy→congested→healthy) on `scenario_a`, confirming
  they're not just non-crashing but behaving AS DESIGNED and
  DIFFERENTLY: B1's path is identical across all 3 steps (never reacts);
  B2 reroutes exactly when congestion crosses its threshold and stays on
  the new path afterward (reactive, not proactive — no oscillation back
  on its own). 12/12 new tests green.

**Phase 16 — RL env + simulator + calibration:**
- `rl/env/simulator.py` — `NetworkSimulator`, a fast, seeded, pure-numpy
  queueing model (M/M/1-style approximation) of `network/topologies/
  *.yaml`. Tracks active flows (fixed-rate, UDP-like offered load routed
  along a path) and per-link fault state; `step()` aggregates offered
  load per link across every flow sharing it, computing utilization
  -> latency (propagation + M/M/1 queueing delay) -> loss (droptail).
  Congested links split throughput by proportional fair share, not a
  full TCP model. Supports the same fault shapes as
  `failure_injector.py` (capacity multiplier, extra delay, loss rate,
  link-down) via `inject_fault()`/`clear_fault()`. 12/12 unit tests
  green, including determinism and multi-flow fair-sharing checks.
- `rl/calibrate.py` — runs 4 identical scenarios (same link
  capacity/delay, same offered UDP load) on both real Mininet and the
  simulator, writing `results/tables/simulator_calibration.json`.
- **Found and fixed two real bugs in the calibration script itself
  before trusting any result:** iperf3's UDP JSON reports the SENDER's
  offered rate as `bits_per_second`, not received throughput (fixed:
  achieved = offered × (1−loss)); `ping` measures round-trip time while
  the simulator's `latency_ms` is one-way (fixed: halved before
  comparing).
- **Gate result — real measured comparison, not fabricated:**
  throughput calibration is excellent under/near capacity (0.008%-0.08%
  relative error across 3 scenarios at 20-80% utilization); latency
  calibration surfaced a genuine sim-to-real gap — the M/M/1 queueing
  model overestimates real queueing delay under load (234% relative
  error at 80% utilization: model predicts +4.8ms queueing, real
  measured +0.04ms). Deliberately NOT patched with an uncalibrated
  scaling factor. Full discussion and Phase 17 consequence (a
  conservative congestion-averse training bias, not a dangerous one) in
  `docs/limitations.md`.

**Phase 17 — RL training + evaluation:**
- `rl/env/rewards.py` — `RewardWeights`/`RewardComponents` (with a
  `.total` property) + `compute_reward()`, the 8 weighted terms from
  `config.yaml`'s `rl.reward_weights`, all computed from real
  before/after `NetworkSimulator` deltas, never fabricated. Unit
  -normalization scales (`throughput_scale_bps`, `latency_scale_ms`)
  documented so raw unit magnitude can't silently dominate the
  configured weights.
- `rl/env/state.py` — fixed-size observation vector (47 dims for
  scenario_a: 12 src/dst one-hot + 4×8 per-candidate features +
  2 global-utilization summary + 1 time-since-reroute), zero-padded when
  fewer than K candidates exist. Built directly on
  `controller/path_computer.py`'s `LinkMetrics`/`link_id_for_edge` so
  link identity matches the heuristic path-cost function's convention.
- `rl/env/actions.py` — `Discrete(K+1)` (0=hold, 1..K=candidate i) with
  `get_action_mask()` for `sb3-contrib`'s MaskablePPO convention.
- `rl/env/network_env.py` — the `gymnasium.Env` (`SDNRoutingEnv`) tying
  simulator+state+actions+rewards together. One fixed h1->h6 (dpid 1->6)
  demand per episode; a seeded-random fault (matching
  `failure_injector.py`'s 6 real fault types) injected on a
  seeded-random link that's guaranteed to lie on the current path.
  **Two real bugs found and fixed via the first MaskablePPO smoke run,
  not assumed away:** (1) a `link_failure` fault makes
  `NetworkSimulator` report `latency_ms=inf`, which reached the policy's
  observation/reward unclipped and produced NaN action logits, crashing
  training outright — fixed by clipping latency to a finite sentinel
  (10,000ms) at the RL env boundary only, confirmed via a dedicated
  regression test (`test_link_failure_fault_never_produces_inf_or_nan`).
  (2) The state's "predicted degradation probability" feature does NOT
  call the real trained ML classifier inside simulated steps (would
  mix two unvalidated approximations — see docs/decisions.md and
  docs/limitations.md); uses a documented deterministic proxy instead.
- `rl/train_rl.py` — loads `config/config.yaml`, trains MaskablePPO for
  `config.rl.train_steps` (300,000) timesteps, saves
  `models/routing_policy.zip`, and writes both
  `results/tables/rl_training_summary.json` and
  `results/figures/rl_learning_curve.png` from stable_baselines3's own
  Monitor episode log — no estimated numbers.
- **Real training run executed:** 300,000 timesteps / 5,000 episodes in
  189.7s (~1580 fps on this Mac). Mean episode reward rose from
  wide-variance early exploration (episodes 0-500, several -1000/+1000
  swings) to a stable plateau around ~50-56 from episode ~500 onward
  through episode 5,000 (explained_variance 0.85-0.89 by the end).
- `rl/evaluate_rl.py` — evaluates the trained policy against 3 baselines
  (`random_valid`, `always_shortest_path`, `always_least_utilized`) over
  `config.rl.eval_episodes` (50) seeded held-out episodes, reporting
  mean reward, recovery success rate, mean post-reroute max utilization,
  and reroute count, each with a 95% CI computed from the actual
  episodes (`results/tables/rl_evaluation.json`).
- **Real evaluation result (50 seeded episodes, base_seed=100):**

  | policy | mean reward (95% CI) | recovery rate | mean reroutes/episode |
  |---|---|---|---|
  | learned (trained policy) | 51.28 ± 3.59 | **1.00** | 1.06 |
  | random_valid | -39.14 ± 72.56 | 0.68 | 35.78 |
  | always_shortest_path | -49.08 ± 1.43 | 0.00 | 0.00 |
  | always_least_utilized | -271.38 ± 97.11 | 0.00 | 60.00 |

  The learned policy recovers from every injected fault (100/100) while
  rerouting on average about once per episode — i.e. it reroutes once,
  correctly, and holds, rather than oscillating. Both non-learned
  reactive/static baselines never recover in this evaluation:
  `always_shortest_path` never reroutes at all by construction;
  `always_least_utilized`'s per-step no-memory greedy choice thrashes
  every single step (60/60) without ever settling onto a fault-avoiding
  path by episode end.
- 9 new `network_env.py` tests, 6 new `rewards.py` tests, 10 new
  `actions.py`/`state.py` tests, 6 new `evaluate_rl.py` tests — all
  green (187/187 total, see Test status).

**Phase 18 — RL integration into the controller:**
- `service/ml_rl_service.py` — added `POST /infer/route`, loaded
  alongside the degradation model at startup (`_load_rl_policy()` loads
  `models/routing_policy.zip` via `sb3_contrib.MaskablePPO.load`).
  Reuses `rl/env/state.py`'s `build_state()` and `rl/env/actions.py`'s
  `get_action_mask()` -- the SAME functions training used -- so a live
  decision's state representation is identical to what the policy was
  trained on (Phase 6's train/live-equality principle, applied to RL).
  Returns 503 (never a fabricated action) when the policy isn't loaded;
  422 if a malformed request produces a non-finite/wrong-shape state
  vector.
- `controller/rl_client.py` — `RLClient.select_route()`, mirroring
  `inference_client.py`'s exact safety-gate contract: connection error,
  timeout, non-200, malformed body, or an out-of-range action index all
  return `None`, never a fabricated route. Runs under venv-ctrl only
  (needs `requests`, which venv-ml deliberately doesn't install -- see
  docs/decisions.md 2026-09-03 on the resulting `TYPE_CHECKING` import
  guard needed in remediation_manager.py).
- `controller/remediation_manager.py` — `remediate()` now tries RL
  selection first when `rl_client` is configured (optional constructor
  arg, default `None` -- every pre-Phase-18 caller/test is unaffected).
  The weighted-cost heuristic ranking is ALWAYS computed regardless, so
  it's immediately ready as the fallback with no extra round-trip.
  Falls back to it, logging `rl_fallback=1` + a specific
  `rl_fallback_reason`, on: `rl_service_unavailable` (client returned
  None), `rl_selected_no_reroute` (RL said "hold" when a change was
  already mandated by the caller), `rl_selected_excluded_path` (RL's
  path is the currently-failing path or one already tried this
  incident), `rl_selected_invalid_path` (defense in depth -- RL's
  resolved path isn't even among the real candidates). `selector` on
  the `route_changes` row is `P2_ML_RL` only when an RL-selected path
  was actually used, `P1_ML_HEURISTIC` on any fallback -- matching
  `common/schema.py`'s `RouteSelector` enum (`P2_ML_RL` existed since
  Phase 1's schema but was unused until now).
- `database/schema.sql` — `route_changes` gained `rl_fallback INTEGER`
  and `rl_fallback_reason TEXT`, so fallback frequency/cause is
  queryable for the Phase 22 comparison study, not just log-visible.
  Safe schema edit (`CREATE TABLE IF NOT EXISTS`, no migration
  framework, no real deployment data yet predates this change).
- 12 new tests across `test_ml_rl_service.py` (real loaded policy, not
  mocked) and `test_remediation_manager.py` (a duck-typed `FakeRLClient`
  stand-in, since the real `RLClient` can't be imported under venv-ml --
  same `requests` constraint as above). Full RL-selects-path,
  service-down, hold-fallback, excluded-path-fallback, invalid-path
  -fallback, and RL-disabled-by-default cases all covered.

**Phase 19 — backend API + SSE:**
- `backend/api.py` — FastAPI app over the shared SQLite database
  (no ORM, matching the approved stack): `GET /api/health`,
  `/api/topology` (latest topology_snapshots row), `/api/links`,
  `/api/incidents` (optional `status` filter), `/api/incidents/{id}`
  (aggregates route_changes + flow_rules + verification_results +
  remediation_events for that incident), `/api/route_changes` (optional
  `incident_id` filter), `/api/telemetry/{link_id}` and
  `/api/predictions/{link_id}` (both `since`/`limit`-bounded, returned
  oldest-first for charting), and `GET /api/events/stream` (SSE).
- **SSE design, stated honestly:** no message queue/pubsub -- not in
  the approved stack. `/api/events/stream` is a plain polling loop
  (1s interval) over `remediation_events.id`, run off the event loop
  thread via `asyncio.to_thread` for each blocking sqlite3 read. A
  freshly-connected client sees only events inserted after it connects,
  not a history replay (verified both in a direct-call unit test and a
  live 3-second `curl -N` smoke test against a running uvicorn process).
- Endpoint functions use plain typed defaults rather than FastAPI's
  `Query(...)` wrapper specifically so they stay callable directly as
  plain Python functions in tests -- the same "no HTTP layer" testing
  pattern `test_ml_rl_service.py` established, needed here because
  starlette's `TestClient` requires `httpx`, which isn't in
  requirements-ml.txt (not added — no test actually needs it once
  endpoints are called directly).
- 14 new tests (`test_backend_api.py`), all real-SQLite-backed (a fresh
  tmp_path DB per test, real `INSERT`s, real queries) -- no mocking of
  the database layer itself.
- **Live-verified, not just unit-tested:** started a real
  `uvicorn backend.api:app` process and `curl`'d every REST endpoint
  (200s where data exists, a real 404 for `/api/topology` with an empty
  DB) plus a 3-second `curl -N` against `/api/events/stream`, observing
  real `: keepalive` frames at the configured 1s cadence.

**Phase 20 — dashboard:**
- `dashboard/index.html` + `style.css` + `app.js` — vanilla HTML/CSS/JS,
  no build step, cytoscape.js 3.28.1 + Chart.js 4.4.1 from cdnjs.
  Served by `backend/api.py` itself via a StaticFiles mount at `/`
  (registered last, since a `/` mount would otherwise shadow the
  `/api/*` routes).
- Written for a reader who has never seen the project: a plain-language
  status headline ("Handling a problem on s1-s3"), the closed loop shown
  as four labelled stages that light up as they engage, a stable
  left-to-right network map (H1 → switches → H6 with fixed node
  positions, not a force-directed layout that reshuffles on every
  reload), the currently-carrying-traffic path drawn thicker and blue,
  and the incident rendered as a chronological story rather than raw
  DB columns. Field names are translated (`utilization` → "How busy",
  `P2_ML_RL` → "chosen by the AI", `rl_fallback` → "AI unavailable: …").
- `backend/api.py` gained `GET /api/features/{link_id}` — the derived
  per-window metrics the dashboard needs. Deliberately added rather than
  re-deriving utilization from raw counters in JavaScript, which would
  have duplicated `common/features.py`'s formula in a second language.
- **Three real bugs found by actually rendering the page** (headless
  Chrome screenshots against the running server, not assumed-correct):
  1. Chart.js was pinned to version 4.4.4, which does not exist on
     cdnjs (404) — charts silently never rendered. Now 4.4.1, verified
     to return 200.
  2. `.chart-empty { display: flex }` overrode the UA stylesheet's
     `[hidden] { display: none }`, so the "no data" message could never
     be hidden and sat on top of a working chart. Added a global
     `[hidden] { display: none !important; }`.
  3. The empty-state itself: the link inspector previously showed four
     bare dashes with no explanation when no telemetry existed in the
     window. It now says so in words.
- Verification note: screenshotting needs Chrome killed manually after
  a fixed delay — an SSE connection never "finishes loading", so
  `--virtual-time-budget` alone hangs forever. Not a page defect.

**Phase 21 — the closed loop, assembled and running live:**
- `controller/healing_loop.py` — runs the chain Phases 11-18 built in
  isolation: prediction -> persistence -> stability -> incident ->
  remediation (RL, heuristic fallback) -> verification -> incident
  closed. A plain class with everything injected, so the whole loop is
  testable under venv-ml with no ryu, Mininet or real time.
- `controller/telemetry_collector.py` builds the live wiring and feeds it
  predictions, injecting `hub.sleep`/`hub.spawn` so a multi-second
  verification never stalls Ryu's event loop.
- `tests/e2e/run_closed_loop_e2e.py` — the acceptance gate: starts the
  service, controller and network, injects a real fault on the active
  path, and polls the DB for what the system did.
- `tests/e2e/measure_probe_overhead.py` — the A/B harness that diagnosed
  the inference-feedback pathology.
- `ml/threshold_sweep.py` + `results/tables/ml_threshold_sweep.json` —
  operating point chosen from held-out test data, not from a live run.

**Six real bugs found by assembling components that had only met in
tests** (all in docs/decisions.md):
  1. The min-improvement gate blocked EVERY reroute — a link predicted to
     fail still looked cheap by its current healthy counters. Predictions
     now feed the path cost, which is what makes the ML affect routing.
  2. Remediation could "fix" an incident by re-selecting the failing
     path, since the verifier passes an empty exclusion list first time.
  3. The controller's own per-link inference calls, made inside its event
     loop, inflated its latency-probe measurements ~20x (23 -> 464 ms),
     corrupting the latency-slope features the model most depends on.
     Fixed by batching + moving inference off the event path: 16.6 ms.
  4. The E2E injected its fault at startup, so the controller's settling
     transient inverted the latency trend the model reads.
  5. Predictions were produced per switch-side, so `persistence.window=4`
     spanned ~4.2 s instead of ~8 s, and let a link's two endpoints
     corroborate each other.
  6. Harness flaws: no `net.start()`, and block-buffered controller logs
     that discarded diagnostics on teardown.

**Gate result — real, and honestly qualified:**

    fault injected on s1-s3 (carrying traffic)
    +18s  incident opened, P=0.420, persistence confirmed
          rerouted s1,s3,s6 -> s1,s2,s3,s6 (selector=P2_ML_RL, no fallback)
    +27s  verification SUCCESS, 0 retries, closed SUCCESS

  **2 of 3 consecutive live runs passed**; the third opened no incident.
  The two passes picked *different* replacement paths (s1,s2,s3,s6 and
  s1,s4,s5,s6), both avoiding the degraded link — evidence the RL policy
  decides from live conditions. Three runs is far too small to state a
  detection rate; Phase 22 measures it across >=5 seeds.

## Pending components

Phase 22 onward: the 8 experiments (>=5 seeds each), figures, README/
architecture docs, technical paper + project report, presentation +
demo guide, patent documentation, and the final reproducibility review.

Phase 22 is now unblocked: it needs a whole working system to measure,
and as of Phase 21 there is one. It is also where the closed loop's
detection reliability gets a real number (see docs/limitations.md --
the 2-of-3 observed in Phase 21 is an observation, not a result).

## Known issues

None blocking. VM-side testing requires an explicit `multipass transfer -r`
re-sync after Mac-side edits (no live mount — see Environment above).

## Configuration

See `config/config.yaml` for the full set. Notable defaults: ML threshold
0.75, uncertainty band [0.45, 0.60], persistence 3-of-4 windows, cooldown
30s, route hold 60s, min improvement 15%, telemetry interval 2s, label
horizon 10s, verify settle 8s. `project.controller_backend: ryu`.

## Test status

243/243 unit tests passing (`test_config.py`, `test_db.py`, `test_logging.py`,
`test_of_compat.py`, `test_features.py`, `test_failure_injector.py`,
`test_ml_rl_service.py`, `test_path_computer.py`, `test_persistence_tracker.py`,
`test_stability_manager.py`, `test_flow_manager.py`, `test_remediation_manager.py`,
`test_baselines.py`, `test_simulator.py`, `test_rewards.py`, `test_actions.py`,
`test_state.py`, `test_network_env.py`, `test_evaluate_rl.py`, `test_backend_api.py`,
`test_verifier.py`, `test_baselines.py`) under
venv-ml (Python 3.11.9) on macOS. Phases 2-5's Mininet/Ryu gates all
passed with real measured results in `sdn-vm`, reproduced twice
independently for Phase 5. Phase 6's train/live feature-equality gate
passed; `common/features.py` also verified importing and running
correctly under the real venv-ctrl (Python 3.9, no pandas). Phase 7's
6-fault-type injection gate passed with real ping/iperf3/telemetry
evidence for every fault, including one honestly-documented limitation
(rising_errors not visible via OVS error counters on veth links). Phase
8's dataset generation gate passed: 8 real seeded runs, class balance
reported (not fabricated). Phase 9's ML gate passed in two stages: an
honestly-weak first result (ROC-AUC 0.37-0.56) was investigated, traced
to a real methodological gap in the fault injector, fixed, and the
dataset regenerated (39,233 windows, 1.63% positive) — retraining then
produced ROC-AUC 0.83-0.84, PR-AUC 0.37-0.38, with permutation
importance confirming the model uses the intended signal. Real metrics
in results/tables/ml_metrics.json + 5 figures in results/figures/; model
save/load reproducibility independently verified via
ml/evaluate_model.py. Phase 10's live-inference gate passed: 320 real
predictions logged with the service running, 112 graceful fallback
warnings and zero fabricated predictions with it killed mid-run, zero
crashes either way. Phase 17's RL gate passed: a real 300,000-timestep
MaskablePPO training run (189.7s wall clock) converged to a stable
episode-reward plateau, and a real 50-episode seeded evaluation showed
the learned policy at 100% recovery success vs 0% for both
`always_shortest_path` and `always_least_utilized` baselines — see
results/tables/rl_training_summary.json and rl_evaluation.json. Phase
18's RL-integration logic is fully unit-tested (all 6 selection/fallback
branches); `controller/rl_client.py` itself has no unit test file, by
the same necessity as `inference_client.py` (Phase 10) — both need
`requests`, which only exists in venv-ctrl inside the VM, so real
end-to-end HTTP behavior against a live `ml_rl_service.py` is a live-VM
verification, not yet performed (deferred to Phase 21's E2E test /
whenever main_app.py first wires RemediationManager in for real).

## Experiment status

Phase 22 in progress. Valid batches are selected explicitly by run id;
superseded ones stay in `results/experiments.db` and are listed with
their reason in `results/invalidated_runs.json`, which `analyze.py`
applies by default.

| | status | result |
|---|---|---|
| E1 | measured, ids 1-23 | B2 (reactive threshold) beats the full system on this fault. Reported, not hidden. |
| E2 | measured, ids 123-182 | Per fault: link failure 100%, gradual degradation 100%, congestion 80%, high latency 60%, packet loss 20%, rising errors 0%. Static routing 0/29. |
| E3 | measured, ids 183-202 | **Falsifies the lead-time claim.** P2 is not earlier than B2; it detects MORE (70% vs 40% in-fault). See docs/limitations.md. |
| E4 | not started | 30 trials |
| E5 | not started | 5 trials |
| E6 | RUNNING (started 2026-09-11 13:19) | 30 trials; the remaining justification for the RL half |
| E7 | not started | 10 trials |
| E8 | not started | 5 trials |

Three code defects were found BY these experiments, not by development:
the predictor's structural blindness to a hard link failure (fixed with
an LLDP link-down detection path, 0/5 -> 5/5), an unbounded iperf3 that
could take a measurement 18 minutes after its window, and a detection
metric that counted reactions to a fault's REMOVAL as detections.

## Documentation status

`docs/decisions.md` and `docs/limitations.md` initialized and current.
`docs/limitations.md` now also carries the four-instance record of this
project's one recurring root cause -- a measurement taken under
conditions that did not match the conditions being claimed -- which
belongs in the paper's threats-to-validity section as a pattern rather
than as four separate mishaps.
`docs/architecture.md` not yet started (later phases).

## Patent documentation status

Not pursued.
