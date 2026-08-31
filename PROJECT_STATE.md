# PROJECT_STATE

Authoritative status. Updated every phase boundary, same commit as the code.
Never re-ask anything recorded here.

## Environment (asked once, 2026-08-30 — do not re-ask)

- **OS:** macOS 14.5 (Darwin 23.5.0), Apple Silicon-class Homebrew at
  `/opt/homebrew`. Not Linux — cannot run Mininet/OVS natively.
- **Sudo:** yes, user `the development account` is in the `admin` group (sudo-capable).
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
- **venv-ml:** Python 3.11.9, `<repo>/venv-ml` on macOS. Built by
  `scripts/setup_env.sh ml`.
- `project.controller_backend: ryu` in `config/config.yaml` (decided
  Phase 2 GO/NO-GO smoke test — no os-ken fallback needed).

## Current phase

**Phase 6 — COMPLETE.** Shared feature pipeline, train/live equality test.
Next: Phase 7 (failure injector, 6 fault types, scheduled).

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
  `.gitignore`, `README.md`, `LICENSE` (MIT), `docs/decisions.md`,
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

## Pending components

Phase 7 onward: failure injector (6 fault types, scheduled), then dataset
generation.

## Known issues

None blocking. VM-side testing requires an explicit `multipass transfer -r`
re-sync after Mac-side edits (no live mount — see Environment above).

## Configuration

See `config/config.yaml` for the full set. Notable defaults: ML threshold
0.75, uncertainty band [0.45, 0.60], persistence 3-of-4 windows, cooldown
30s, route hold 60s, min improvement 15%, telemetry interval 2s, label
horizon 10s, verify settle 8s. `project.controller_backend: ryu`.

## Test status

46/46 unit tests passing (`test_config.py`, `test_db.py`, `test_logging.py`,
`test_of_compat.py`, `test_features.py`) under venv-ml (Python 3.11.9) on
macOS, 96% coverage on `common/` + `database/`. Phases 2-5's Mininet/Ryu
gates all passed with real measured results in `sdn-vm`, reproduced twice
independently for Phase 5. Phase 6's train/live feature-equality gate
passed; `common/features.py` also verified importing and running
correctly under the real venv-ctrl (Python 3.9, no pandas).

## Experiment status

Not started (Phase 22).

## Documentation status

`docs/decisions.md` and `docs/limitations.md` initialized and current.
`docs/architecture.md`, `technical_paper.md`, `project_report.md`,
`presentation.md`, `demo_guide.md` not yet started (later phases).

## Patent documentation status

Not started (Phase 27). `docs/patent_notes.md` and
`docs/invention_disclosure.md` do not exist yet.
