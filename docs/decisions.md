# Architectural Decision Log

One line per decision, newest last. Decisions dictated directly by the
project owner (the approved tool stack, environment, two-venv split,
phase plan) are not re-logged here — only decisions made autonomously while
executing the plan.

- 2026-08-30: Repository root is `the project directory`
  itself (not a nested `ai-self-healing-sdn/` subdirectory) — the directory
  was already empty and dedicated to this project.
- 2026-08-30: Chose MIT license for the software implementation; patent
  rights (if pursued) are tracked separately per `docs/patent_notes.md` and
  are explicitly noted as not granted by the software license.
- 2026-08-30: `common/schema.py` holds shared string enums (LinkHealthState,
  IncidentStatus, VerificationClassification, RouteSelector, DataSource,
  FlowRuleResult) as the single source of truth for magic strings written
  into SQLite columns, importable unchanged from both venvs.
- 2026-08-30: `database/db.py` exposes only generic primitives
  (connect/init_schema/insert/query/execute) at Phase 1; table-specific
  convenience methods are added in the phase that first needs them (e.g.
  telemetry inserts arrive with the Phase 5 telemetry_collector) rather than
  stubbed now, per the no-placeholder rule.
- 2026-08-30: Dev machine for this project is macOS 14.5 (8GB RAM / 8
  cores, sudo-capable), which cannot run Mininet/OVS natively (no Linux
  network namespaces). Decision: build everything that doesn't need a Linux
  kernel (Phase 1: config/logging/DB) directly on macOS under venv-ml
  (Python 3.11 already present at
  `/Library/Frameworks/Python.framework/Versions/3.11`); Phases 2+ (Mininet,
  OVS, the Ryu controller's smoke test) require a Linux VM. Recommended:
  Multipass or UTM running Ubuntu 22.04 LTS, 4GB RAM / 4 vCPU. This is
  disclosed to the user directly (not silently substituted) and is not a
  pure-simulation substitution — actual Mininet/OVS still runs, just inside
  a VM rather than bare metal.
- 2026-08-30: Linux VM tooling for Phases 2+ is Multipass (`brew install
  --cask multipass`), Ubuntu 22.04 LTS, 4GB RAM / 4 vCPU — scriptable
  end-to-end (launch/mount/shell all via CLI), unlike UTM's GUI ISO install.
- 2026-08-31: Multipass's default "classic" (SSHFS-based) live mount of the
  project directory into the VM proved unreliable for this workflow: it
  produced broken bin/python venv symlinks that not even `rm -rf` (with
  sudo, from either side) could remove, and later the whole mount started
  rejecting reads ("Operation not permitted") after a VM stop/start cycle.
  The alternative "native" (virtiofs) mount type failed outright because
  qemu couldn't get macOS TCC permission to read `~/Desktop` (a
  privacy-protected folder) without a manual Full Disk Access grant.
  Decision: stop relying on a live mount. `multipass transfer -r` pushes a
  point-in-time snapshot of the repo into the VM's native filesystem
  (`/home/ubuntu/ai-self-healing-sdn`) instead; re-run it after edits that
  need to be tested VM-side. This is slower to iterate than a live mount
  but has proven completely reliable, and the VM-side copy is disposable
  (the Mac-side working tree with its `.git` history remains authoritative).
- 2026-08-31: `network/topologies/scenario_a.yaml`'s partial mesh has real
  L2 cycles by design (h1->h6 needs >=3 loop-free *routed* paths, which
  requires redundant links to exist). Confirmed empirically that OVS
  fail-mode='standalone' (used so topology sanity checks don't need an SDN
  controller running) floods and storms on that topology: pingall was
  100% dropped. Fix: `topo_builder.enable_rstp_and_wait()` enables RSTP
  (`ovs-vsctl set Bridge <sw> rstp_enable=true`) on every switch and waits
  15s for convergence whenever fail_mode='standalone', both in
  topo_builder.py and traffic_generator.py. Not needed once a real SDN
  controller runs in fail-mode='secure' (Phase 4 onward): there is no
  flooding to loop when forwarding is exact-match flows chosen by the
  controller instead of learning-switch broadcast.
- 2026-08-31: `network/topo_builder.py`, `traffic_generator.py`, and
  (later) `failure_injector.py` run under the VM's **system** Python 3.10,
  not venv-ctrl or venv-ml. The apt `mininet` package installs into
  `/usr/lib/python3/dist-packages` against system Python, and Mininet
  itself needs root to create network namespaces — it is architecturally
  separate from the Ryu controller process, which only talks to Mininet's
  switches over the OpenFlow wire protocol, never via Python import. system
  Python needed `python3-yaml` (apt) added for topology-spec parsing;
  `iperf3` (apt) provides the CLI the approved stack specifies for
  throughput measurement.
- 2026-09-03: `rl/calibrate.py`'s real-vs-simulator comparison surfaced
  two real bugs in the calibration script itself (not the simulator's
  physics), fixed before trusting any result: iperf3's UDP JSON reports
  the sender's offered rate as `bits_per_second`, not received
  throughput (achieved = offered * (1-loss)); and `ping` measures
  round-trip time while the simulator's `latency_ms` is one-way (halved
  before comparing). After fixing both, throughput calibration is
  excellent (<0.1% error) under/near capacity; latency calibration
  showed the M/M/1 queueing model genuinely overestimates real
  queueing delay under load (234% error at 80% utilization) --
  deliberately NOT patched with an uncalibrated scaling factor, since
  that would trade one unverified number for another. Documented as a
  real sim-to-real gap in docs/limitations.md, with its consequence for
  Phase 17's reward signal (a conservative congestion-averse bias, not a
  dangerous one) stated explicitly rather than glossed over.
- 2026-09-03: `controller/flow_manager.py`'s `incident_cookie()` produced a
  full unsigned 64-bit hash, but SQLite's `INTEGER` column is signed
  64-bit -- confirmed on a live VM run: an `OverflowError` while logging
  hop 1's FlowMod aborted `install_path()`'s loop entirely (no per-hop
  exception isolation existed), leaving a 4-switch path with a flow
  installed on only its FIRST switch despite every hop reporting
  "INSTALLED". Fixed two ways: (1) `incident_cookie()` masks to 63 bits
  (`& 0x7FFFFFFFFFFFFFFF`), trivial entropy loss, guaranteed to fit;
  (2) `install_path()` now isolates each hop's install-and-log in its
  own try/except, so a failure on one hop can never prevent the rest --
  defense in depth, since the specific overflow is fixed but the "one
  hop's failure cascades to all others" fragility was the more dangerous
  half of the bug. Re-verified live: all 4 hops of a real remediated
  path ([1,4,5,6]) now show correct `ovs-ofctl dump-flows` entries with
  the same cookie, confirming the fix end-to-end, not just in unit
  tests. Regression tests added (2000-iteration cookie-range sweep, plus
  a flaky-logger test simulating the exact original failure).
- 2026-09-03: `service/ml_rl_service.py` (Phase 10's ML inference
  service, and later Phase 18's RL policy service) must be reachable via
  `localhost` HTTP from the Ryu controller per the approved architecture
  ("communicate over localhost HTTP"). Since the controller runs inside
  `sdn-vm` (not on the Mac host, per Phase 2's decision), the service
  must also run inside `sdn-vm` -- so a second interpreter, `venv-ml`
  (Python 3.11.15, installed via deadsnakes, same PPA already added in
  Phase 2), now exists in the VM alongside `venv-ctrl`, built the same
  way as the Mac's `venv-ml` via `scripts/setup_env.sh ml`. The Mac's
  own `venv-ml` remains the one used for training (Phases 8-9) and stays
  the faster/more convenient place to iterate; the VM's `venv-ml` exists
  specifically to serve the already-trained model live.
- 2026-09-02/03: Phase 9's near-chance ML result (ROC-AUC ~0.52-0.56) was
  traced to `failure_injector.py` faults being instantaneous step
  functions with no pre-fault precursor -- the anticipation window (the
  label's positive class) was statistically identical to any healthy
  window. Fixed by giving every fault type a genuine ramp toward its
  target intensity (`ramp_in_s`, default 12s; 18-25s for
  `gradual_degradation`) BEFORE `fault_start` is logged, via a new
  `_ramp_to_target()` helper shared across all 6 fault types (replacing
  the old fault-type-specific `_apply_instant_fault`/
  `_apply_gradual_degradation` split with one unified ramp-then-hold
  design). `fault_start` still marks arrival at full intensity, so
  `ml/dataset/label_rules.py`'s labeling semantics are unchanged. The
  full 8-seed dataset was regenerated (~2 hours wall-clock this time,
  including an unplanned ~10.5-hour gap in seed 4 from the host laptop
  sleeping overnight -- confirmed harmless: only `run_start_ts`/
  `run_end_ts` timestamps were affected, not the fault schedule or
  labeled counts, which stayed consistent with other seeds). Retraining
  on the ramped dataset raised ROC-AUC to 0.83-0.84 and PR-AUC 15-18x;
  permutation importance confirmed the model is using the engineered
  ramp trend (`latency_ms_slope_10`/`slope_5`), not an unrelated
  artifact. Full before/after numbers in `docs/limitations.md`.
- 2026-09-02: Phase 9 ML training crashed on `HistGradientBoostingClassifier`
  ("window shape cannot be larger than input array shape") because
  `latency_ms` was 100% NaN across the whole Phase 8 dataset -- a real bug
  in `ml/dataset/generate_dataset.py`: it never joined the probe-latency
  samples telemetry_collector.py writes to `features`
  (`latency_is_estimate=1`) into the `RawWindow` history it built purely
  from `telemetry` counter rows (which has no `latency_ms` column at
  all). Fixed with `_nearest_latency_ms()` (nearest-in-time join,
  tolerance `2*telemetry.interval_s`, since the two are polled on
  independent timers). Rather than re-running the full ~90-minute, 8-seed
  Mininet generation, re-extracted all 8 runs directly from the VM's
  still-intact `database/network.db` (the underlying telemetry was never
  wrong, only the post-hoc extraction) -- verified byte-identical
  windows/positive/negative counts to the original run, confirming the
  fix only fills in previously-missing latency values and changes nothing
  else.
- 2026-09-02: `loss_rate`/`error_rate` are exactly 0.0 for every one of
  39,248 dataset rows -- confirmed via `MAX(rx_dropped)-MIN(rx_dropped)`
  per port across the whole dataset run being 0 everywhere. This
  generalizes the 2026-08-31 `rising_errors` finding to `packet_loss` too
  (both rely on OVS port counters that `tc netem` bypasses on veth
  links), and corrects an earlier claim in `docs/limitations.md` that
  `packet_loss` WAS visible via `rx_dropped` (that was a misreading of a
  monotonically-increasing cumulative counter summed across many polls).
  `ml/train_model.py` now detects and excludes zero-variance features
  explicitly (reported, not silently dropped) rather than letting
  `HistGradientBoostingClassifier` crash on them.
- 2026-08-31: Ryu's `packet.Packet.serialize()` pads a bare Ethernet frame
  with no upper-layer protocol up to the 60-byte minimum frame size before
  returning `pkt.data`. Confirmed by debugging Phase 5's latency prober,
  which appends a raw trailer (probe id + timestamp) after `pkt.data` --
  reading that trailer back at a fixed offset after the 14-byte Ethernet
  header landed inside the padding (always read as zero) instead. Fix:
  any code appending a custom trailer to a Ryu-serialized frame must read
  it back from the END of the received frame (`msg.data[-N:]`), never from
  a fixed offset after the header. Applies anywhere else this pattern gets
  reused (e.g. if RL-selected-path telemetry probing does something
  similar later).
- 2026-08-31: `venv-ctrl` lives at `$HOME/venv-ctrl-ai-self-healing-sdn`
  inside the VM (outside the project directory) rather than at
  `<repo>/venv-ctrl`, even now that the project directory is a native-VM
  copy rather than a live mount — keeping venv location decoupled from
  however the repo got onto the VM avoids re-hitting this class of problem
  if a live mount is revisited later. See `scripts/setup_env.sh`.
- 2026-09-03: Phase 18's `controller/rl_client.py` imports `requests`,
  which requirements-ml.txt deliberately does not install (it's
  ml_rl_service.py's own HTTP client dependency, never needed by the
  FastAPI/uvicorn *server* side). Importing it at module level in
  `controller/remediation_manager.py` (which HAS venv-ml-run unit tests,
  unlike inference_client.py which has none) broke those tests outright.
  Fixed by moving the import under `if TYPE_CHECKING:` -- safe because
  `from __future__ import annotations` (already present) makes every
  type hint in that file lazy, so the import was never actually needed
  at runtime, only for type checkers. Any future controller/ module that
  both (a) needs `requests` and (b) wants venv-ml-run unit tests must use
  this same pattern.
- 2026-09-03: Phase 17 RL env (rl/env/network_env.py) does not call the
  real trained ML classifier (service/ml_rl_service.py) inside simulated
  training steps to produce the state's degradation-probability feature.
  That model was trained on real Mininet telemetry feature distributions
  (Phase 9); feeding it NetworkSimulator's approximated per-step metrics
  would introduce an unvalidated distributional mismatch outside
  anything Phase 16's calibration covered. Used a documented deterministic
  proxy instead (`_proxy_degradation_prob`: weighted function of
  utilization/loss/fault-active, clipped to [0,1]) -- moves the same
  direction a real predictor would, never claimed to BE calibrated. See
  docs/limitations.md.
- 2026-09-03: NetworkSimulator reports `latency_ms = inf` for a link with
  `fault_down=True` (link_aggregate_metrics's own documented behavior --
  correct as "queueing delay undefined"). Confirmed via the MaskablePPO
  smoke test that this inf reaches the policy's observation/reward
  unchanged and produces NaN action logits, crashing training outright.
  Fixed by clipping latency_ms to a finite sentinel (10,000 ms) at the RL
  env boundary only (rl/env/network_env.py's `_link_metrics_from_step`
  and `_clip_flow_latency`) -- the simulator itself keeps reporting real
  inf, unmodified; only the RL env's observation/reward construction
  clips it, since a neural net input must be finite but nothing else
  downstream needs that constraint.
- 2026-08-30: Because Ryu's controller role only needs a TCP listener that
  OpenFlow-speaking switches can reach (it does not itself need to create
  network namespaces), venv-ctrl can potentially run on the macOS host while
  Mininet/OVS run inside the Linux VM, with the VM's OVS instances
  configured with `--controller=remote,ip=<host-ip>,port=6653`. This will be
  evaluated for real at the Phase 2 GO/NO-GO smoke test; if it adds friction
  the fallback is to run both venv-ctrl and Mininet inside the VM.
- 2026-09-05: Phase 21 assembles the closed loop in
  `controller/healing_loop.py` as a PLAIN class, not a `ryu.RyuApp`.
  Everything it needs (graph, datapaths, link metrics, sleep, spawn) is
  injected, so the full prediction -> persistence -> stability ->
  remediation -> verification chain is exercisable under venv-ml with
  fakes and no ryu/Mininet/real time. `controller/telemetry_collector.py`
  builds the live wiring, because only that module is guaranteed to run
  under venv-ctrl where `requests` (needed by inference_client and
  rl_client) exists. `hub.sleep`/`hub.spawn` are injected there: Ryu is a
  cooperative-greenthread world, and a multi-second verification run
  inline inside an OpenFlow event handler would stall every other
  switch's telemetry.
- 2026-09-05: The healing loop folds the ML prediction into the path
  COST before comparing routes (`_metrics_with_prediction`). Found by
  assembling the loop: each component was individually correct, yet the
  system never rerouted, because a link predicted to fail still looked
  cheap by its current (still-healthy) counters, so the min-improvement
  gate rejected every alternative as "worse". The degrading link is
  re-costed with the model's own probability as an expected loss_rate
  and utilization floor (never lowering a measured value). This is a
  ranking-only adjustment; nothing adjusted is written to the DB or
  reported as a measurement.
- 2026-09-05: The healing loop seeds the verifier's exclusion list with
  the currently-failing path. `Verifier.verify_and_retry` passes `[]` on
  its first attempt, so without this the first remediation could
  "resolve" an incident by re-selecting the very path that was failing.
  Every candidate still crossing the degrading link is excluded too.
- 2026-09-05: Phase 21's live loop never fired despite 982 real
  predictions (max probability 0.193 vs a 0.75 threshold). Rather than
  lowering the threshold to make it fire -- which would have manufactured
  a result -- the cause was measured. `tests/e2e/measure_probe_overhead.py`
  A/B-tests the identical controller and topology with inference the only
  variable: median probe latency is 23.3 ms with inference off and
  464.2 ms with it on, against 2.6 ms in the Phase 8 training data. The
  controller's own blocking per-link HTTP inference calls run inside the
  same single-threaded event loop that timestamps the latency probes, so
  running the model degrades the latency-slope features the model relies
  on most. Documented in docs/limitations.md as an open issue; no
  detection-performance claim may rest on the live loop until resolved.
  A CPU-contention explanation was tested first and rejected (4 -> 6
  vCPUs moved mean latency only 517 -> 437 ms, max probability unchanged).
- 2026-09-05: Fixed Phase 21's inference-feedback pathology two ways,
  both required: (1) inference moved OFF the OpenFlow event path into
  its own greenthread (`_run_inference_cycle`), so probe PacketIns are
  no longer timestamped behind inference work; (2) per-link inference
  calls replaced by one batched call per cycle
  (`POST /infer/degradation/batch`, `predict_degradation_batch`),
  collapsing ~16 HTTP round trips into one and letting sklearn score a
  matrix instead of N single rows. Re-measured with the same A/B
  harness: median probe latency with inference ON went 464.2 ms ->
  16.6 ms, now indistinguishable from inference OFF (17.2 ms), while
  still producing 448 predictions per 60s window. The batch path is
  covered by a test asserting it returns the same probability as the
  single-item path, so the fix changes only how many round trips are
  needed, not the model's answer.
- 2026-09-05: `ml.threshold` moved 0.75 -> 0.40 and
  `persistence.uncertainty_band` [0.45, 0.60] -> [0.25, 0.40]. The
  threshold was selected by `ml/threshold_sweep.py` on the HELD-OUT test
  seeds (F1 peaks at 0.40: 0.416, vs 0.355 at 0.75), never against a
  live closed-loop run -- choosing an operating point by what makes a
  demo fire would be fitting the demo. The original 0.75 was fixed in
  Phase 1 before any model existed. Trading per-reading precision
  (0.837 -> 0.558) for recall (0.225 -> 0.331) is right for THIS
  architecture: a missed degradation is unrecoverable, while a false
  positive is filtered twice more by persistence (3-of-4) and stability
  (cooldown/route-hold/min-improvement), so per-reading precision is not
  the rate at which spurious reroutes occur. The band moved below the
  threshold to keep the rule monotonic (healthy -> uncertain ->
  degraded); `test_decision_rule_is_monotonic` now enforces that
  ordering, and two config tests that hardcoded the old band literals
  were made value-independent (one had silently become a no-op).
- 2026-09-05: Predictions are aggregated to one reading per link per
  telemetry cycle (max across the two switch-sides) before reaching the
  persistence tracker. Each link had been scored once per endpoint, so
  `persistence.window = 4` spanned ~4.2 s instead of the 4 telemetry
  windows (~8 s) the spec's "3 of the last 4 windows" means -- measured
  live at 1.9 readings/cycle. It also allowed a link's two endpoints to
  corroborate each other, which is exactly the independence the
  persistence gate is supposed to require. `persistence.consecutive` was
  deliberately NOT relaxed to compensate; weakening it would have
  hollowed out a mechanism the project claims as a distinguishing
  element.
- 2026-09-07: Phase 22's eight experiments were not recorded anywhere in
  the repo (the original enumeration was lost when the working
  conversation compacted), so they were re-derived from the claims the
  project actually has to support and written down in
  `experiments/definitions.py` -- each with the claim it exists to test,
  so nothing is run and no figure is drawn without a stated reason. The
  systems form a ladder (B1 static -> B2 reactive thresholds -> P1
  ML+heuristic -> P2 ML+RL) that adds exactly one mechanism per rung, so
  a difference between adjacent rungs is attributable to that mechanism.
  180 trials at >=5 seeds each.
- 2026-09-07: `load_config()` now honours an `AI_SDN_CONFIG` environment
  variable. The Ryu controller is launched as a subprocess and calls
  `load_config()` with no arguments, so the experiment harness had no
  other way to run the same controller as B1/B2/P1/P2 without mutating
  the tracked config file. Each trial writes its own complete config into
  its results directory, so any recorded number can be traced back to the
  exact settings that produced it.
- 2026-09-07: System selection lives in `healing_loop.py` for B1/B2 only.
  P1 vs P2 is expressed purely as "was an rl_client supplied", reusing
  the safety-gate fallback path that already exists, so the E6 ablation
  differs in path SELECTION and nothing else. B1 returns immediately from
  on_prediction (the do-nothing control); B2 ignores the probability
  entirely and calls `evaluate_current_conditions` on measured metrics,
  bypassing persistence and stability -- which is what makes E4 and E5
  able to measure what those gates are worth.
- 2026-09-07: The inference service is started for EVERY trial including
  B1, which never queries it. Leaving it out would give the B1 arm a
  quieter machine, and Phase 21 established that host load measurably
  moves this controller's own latency measurements -- so an unloaded
  control would have confounded every comparison against it.
- 2026-09-07: The Phase 22 pilot (B1 vs P2, one seed) did its job and
  blocked the full 180-trial run. It measured a real, severe fault
  (throughput 4.72 -> 0.56 Mbps, RTT 6 -> 140 ms) but P2 behaved
  identically to the do-nothing control: max probability on the badly
  degraded link was 0.290 (below the 0.40 threshold) while a HEALTHY
  link scored 0.720, despite the degraded link's latency going
  40.9 -> 342.0 ms. Root cause is train/serve skew again, from a
  different source than Phase 21's: `generate_dataset.py` assumes an
  externally-running controller, and in Phase 8 that controller ran with
  no inference service and no measurement load (median probe latency
  2.6 ms). The experiment harness runs the service plus iperf3/ping, and
  measures 40.9 ms before any fault -- ~16x outside the training range
  on the feature family the model most depends on. Decision: regenerate
  the dataset under deployment-matched conditions
  (`ml/dataset/regenerate_matched.py`: inference service running for load
  parity, controller as B1_SHORTEST_PATH so it predicts but never
  remediates and cannot confound the fault labels) and retrain, rather
  than retune thresholds against a distribution the model never learned.
  Topology, traffic, fault schedule, seeds and label rules are unchanged
  so the comparison with the Phase 8 model is like-for-like.
- 2026-09-07: Derived per-link metrics are now persisted to the
  `features` table by the telemetry collector. They had lived only in
  memory, so `features.utilization` was NULL in every row (0 of 2000 in
  the pilot): there was no record of what the system actually saw when it
  made a decision, which made post-hoc analysis of an experiment
  impossible and left the dashboard's link inspector permanently blank.
- 2026-09-07: The experiment runner now joins the fault-injector thread
  before tearing the network down. The pilot tore down while an injector
  was still sleeping; it woke into a dead Mininet and crashed inside
  recover() ("'NoneType' object has no attribute 'node'"), which also
  meant the previous trial's fault was never formally recovered before
  the next trial began.
