# Limitations

Honest record of every place this project's implementation stands in for
something that couldn't be done directly, and why. Updated as each phase
surfaces a new one — this file is never emptied out to look cleaner.

## Development environment (Phase 1)

- **Dev machine cannot run Mininet natively.** The primary development
  machine is macOS 14.5, which has no Linux network-namespace support that
  Mininet/Open vSwitch require. All data-plane phases (topology emulation,
  traffic generation, failure injection, and therefore every dataset used
  for ML/RL training and every experiment result) run inside a Linux VM
  (Ubuntu 22.04 LTS), not on bare metal. This is disclosed rather than
  substituted away: the VM runs real Mininet and real OVS, not a simulation
  of them. See `docs/decisions.md` (2026-08-30 entries).

## Latency probe accuracy (Phase 5)

`controller/telemetry_collector.py`'s per-link latency probe is explicitly
an ESTIMATE, not ground truth (see its module docstring for the mechanism).
Phase 5 validated it against real `ping` measured independently by
Mininet, on `network/topologies/scenario_a.yaml`. Reproduced twice from a
clean controller restart + wiped DB (2026-08-31); the second run's fuller
sample (n=28/link over the run, vs. n=8 in the first pass) is reported
below:

| link | configured one-way delay | probe estimate: n, min/avg/max (ms) | independent ping RTT |
|---|---|---|---|
| s1-s2 | 2ms | n=28, 8.7/29.1/64.6 | 4.18-5.02ms (h1->h2, direct; avg 4.42ms) |
| s1-s3 | 1ms | n=28, 11.2/31.8/67.5 | -- |
| s3-s6 | 2ms | n=28, 7.2/27.2/70.0 | 6.18-6.80ms (h1->h6, via s1-s3-s6; avg 6.34ms) |
| s2-s5 | 3ms | n=28, 9.8/26.1/57.5 | -- |

(First run, n=8/link, for comparison: s1-s2 avg 21.1ms, s1-s3 avg
21.1ms, s3-s6 avg 18.2ms, s2-s5 avg 17.6ms, against ping averages
4.68ms/6.54ms — same pattern, smaller sample.)

**Finding:** the probe estimate runs roughly 4-7x higher than the real
ping RTT (not just one-way delay — RTT, an even more generous
comparison), AND is roughly the same magnitude across all four links
regardless of their different configured propagation delays (1-3ms),
with wide run-to-run variance (min-max spans 55-63ms per link). This
indicates the estimate is dominated by controller-side eventlet
greenthread scheduling/queueing overhead (the probe loop, echo loop,
telemetry poll loop, and OpenFlow event dispatch all share one
cooperatively-scheduled reactor thread under Ryu), not by the physical
link propagation delay the technique is meant to measure.

**Why this is reported, not hidden or "fixed" by adjusting numbers:** the
subtraction of half the OFPEchoRequest RTT (see telemetry_collector.py)
already accounts for one control-channel hop as designed; the residual
overhead is real, reproducible measurement error inherent to a
single-threaded green-thread control plane under concurrent load (LLDP,
telemetry polling, echo, and probe traffic all contending for the same
event loop), not a bug being papered over. Two independent runs
(2026-08-31), the second from a fully clean restart at the user's
request, produced consistent results.

**Practical consequence for later phases:** absolute latency_ms values
from this probe should not be treated as accurate wall-clock link delay.
Relative changes (before vs. after a remediation) are likely more
reliable than absolute magnitude, since the same overhead applies to
every measurement. This should be stated plainly in the technical paper
and not used to claim sub-millisecond measurement precision anywhere.
Revisiting this (e.g. moving probing to a less contended thread, or
weighting by echo-RTT jitter) is deferred; not required for the closed
loop to function, since persistence/stability logic (Phase 12) operates
on trends and thresholds, not absolute latency accuracy.

## rising_errors AND packet_loss: not observable via OVS rx_errors/rx_dropped (Phase 7, corrected Phase 9)

`network/failure_injector.py`'s `rising_errors` fault applies a real
`tc qdisc ... netem corrupt <pct>%` to the link's interfaces -- genuine
bit-level corruption, not a fabricated value. Verified this is really
applied: during a 6s, corrupt=8% fault on s1-s2 (2026-08-31), the
switch-side port's `rx_bytes` counter grew only ~2.8KB over the whole
window (828020 -> 830798) versus hundreds of KB/s normally under
comparable traffic -- a real, large effect on delivered traffic.

However, `telemetry.rx_errors`/`tx_errors` (from `OFPPortStatsRequest`)
stayed at exactly 0 throughout, and across the entire test run (638
telemetry rows, `SUM(rx_errors)=0`, `SUM(tx_errors)=0`).

**Correction (2026-09-02):** the original version of this entry claimed
`packet_loss`'s effect WAS visible via `rx_dropped` (`SUM(rx_dropped)=232`
in that same Phase 7 run). That was a misreading: `rx_dropped` is a
monotonically increasing cumulative OVS counter, and summing it across
many polling snapshots double/triple-counts the same standing value --
it does not mean 232 packets were actually dropped. Phase 9's ML training
crashed outright on an entirely-zero-variance `loss_rate` column, which
prompted a proper check: `MAX(rx_dropped) - MIN(rx_dropped)` per
(switch_id, port_no) across the full 8-run, ~90-minute dataset-generation
run is **0 for every single port** (54,572 telemetry rows checked).
`rx_dropped` is set once near port startup (almost always to exactly 1)
and never increments again, regardless of any `packet_loss` fault applied
during the run.

**Root cause (covers both `rising_errors` and `packet_loss`):** Mininet's
links are Linux `veth` pairs, and `tc netem` (both `loss` and `corrupt`)
is applied to the veth interface itself, which sits OUTSIDE OVS's own
per-port accounting -- packets netem drops or corrupts on the wire never
reach OVS's datapath at all, so OVS's `OFPPortStatsRequest` counters
(which only reflect what OVS itself processed) never see them. This is a
genuine property of software-emulated links, not a bug in the injector or
the telemetry collector -- confirmed by the SAME real, physical effect
being independently visible in other ways every time (ping RTT/loss,
iperf3 throughput, `rx_bytes` deltas -- see Phase 7's verification and
`utilization`, which IS a usable feature, below).

**Consequence for later phases:** `error_rate` AND `loss_rate` (derived
in `common/features.py` from `rx_errors`/`rx_dropped` deltas) are exactly
0.0 for every row in the Phase 8 dataset -- confirmed zero-variance
across all 39,248 windows, not just for fault-labeled ones. Phase 9's
`ml/train_model.py` detects and explicitly excludes zero-variance
features before training (reported in `results/tables/ml_metrics.json`'s
`dropped_zero_variance_features`, not silently dropped). The model's
only learnable signal for `packet_loss`/`rising_errors`/`congestion` has
to come from `utilization` (which DOES respond -- confirmed: reduced
`rx_bytes` deltas on the receiving side) and `latency_ms`. This
should be stated explicitly in the technical paper's dataset/feature
discussion, and `rising_errors` classification accuracy (if reported
separately per fault type) should be expected to be no better than what
those indirect signals allow -- not artificially inflated by adding a
fabricated error-count feature to compensate.

## ML lead-time prediction: near-chance performance, and why (Phase 9)

`ml/train_model.py` trained all three approved candidates
(HistGradientBoosting, RandomForest, LogisticRegression) on the real
Phase 8 dataset (train seeds 1-6, test seeds 7-8, 22 zero-variance
features excluded per the entry above). **Real, computed results, not
fabricated or adjusted:**

| model | ROC-AUC | PR-AUC | precision/recall/F1 @ threshold=0.75 |
|---|---|---|---|
| histgradientboosting | 0.524 | 0.0214 | 0.000 / 0.000 / 0.000 |
| randomforest (chosen) | 0.558 | 0.0239 | 0.000 / 0.000 / 0.000 |
| logisticregression_baseline | 0.368 | 0.0161 | 0.000 / 0.000 / 0.000 |

(Test set: 9,730 rows, 200 positive, i.e. 2.06% -- PR-AUC's own no-skill
baseline is ~0.021, so these PR-AUC values are barely above chance.
ROC-AUC near 0.5, one model below it. At threshold=0.75, no model ever
predicts positive for a true positive window: 0 true positives across
all three, full confusion matrices in
`results/tables/ml_metrics.json`.)

**Root cause, investigated rather than accepted at face value:** the
label (per docs/architecture.md and `ml/dataset/label_rules.py`) is 1
only for windows in the `label.horizon_s` (10s) BEFORE a fault's onset --
deliberately an anticipation/lead-time label, not a "currently degraded"
label (a model that only detects an already-dead link was explicitly
called out as not the point of this project). But
`network/failure_injector.py`'s fault types are applied as instantaneous
step functions -- `congestion`, `packet_loss`, `high_latency`,
`rising_errors`, and `link_failure` all flip from fully healthy to fully
faulted in one `tc`/OVS call, with no organic ramp-up before `start_ts`.
Even `gradual_degradation`'s 5-step ramp happens AFTER `start_ts`, not
before it. Telemetry polling (2s interval) is on an independent timer
from fault scheduling, so the ~5 samples labeled positive before each
fault are, by construction, statistically indistinguishable from any
other healthy window -- there is nothing in the network that has changed
yet. This is not a bug in feature computation, labeling, or model
training (all independently verified working correctly elsewhere in this
document); it is a property of the fault-injection methodology: **real
link degradation often has organic precursors a model could learn to
recognize; this synthetic step-function injection does not create any**.

**Consequence, stated plainly for the technical paper:** this dataset and
this training run do not demonstrate genuine lead-time prediction
capability, because the experimental design as currently implemented
gives the model no precursor signal to find, regardless of model choice
or feature engineering quality. This is a limitation of the emulation
methodology, not evidence the modeling approach doesn't work. Honest
options going forward (not implemented here -- out of scope for closing
the loop, which needs a working baseline more than a highly accurate
one): (1) add a genuine pre-fault ramp period to more fault types,
modeled after `gradual_degradation`'s existing step ramp, so real
precursor signal exists to learn from; (2) reduce `label.horizon_s`
toward 0 to evaluate detection (not anticipation) as a separate, easier,
honestly-labeled task; (3) collect substantially more runs so rarer
borderline cases (e.g. windows just as `gradual_degradation`'s ramp
begins) contribute enough training examples to matter. None of these are
implemented in Phase 9 -- doing so would change the dataset and require
regenerating it (another ~90 minutes of Mininet runtime), which is
recorded here as follow-up work rather than undertaken silently to
produce a better-looking number.

Further entries (RL simulator calibration, queue-occupancy availability,
etc.) are added as those phases are reached.
