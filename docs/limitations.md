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

## ML lead-time prediction: near-chance performance, root-caused and fixed (Phase 9)

**Resolved 2026-09-03 -- see the update at the end of this entry for the
fix and the real post-fix numbers.** The original finding is kept below
in full because the diagnosis is what led to the fix; deleting it would
hide the reasoning that produced a genuinely better model.

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

### Update (2026-09-03): option (1) implemented, dataset regenerated, real improvement

Option (1) above was implemented: `network/failure_injector.py` now ramps
every fault type toward its target intensity over `ramp_in_s` (12s for
most types, 18-25s for `gradual_degradation`) BEFORE logging
`fault_start`, instead of jumping there instantly (see
`docs/decisions.md`, 2026-09-02/03). The full 8-seed, ~600s-per-run
dataset was regenerated with this change (`results/tables/
dataset_summary.json`: 39,233 windows, 640 positive, 38,593 negative,
1.63%) and `ml/train_model.py` re-run on it.

**Verified the ramp produces real signal before retraining** (not
assumed): queried `features.latency_ms` for a `high_latency` fault on
s1-s4 (seed 1) in the 16s before its `fault_start` -- values climb from
~6-10ms baseline to ~29-32ms right at onset, a clear, real upward trend,
not noise.

**New results, real and computed the same way as before (train seeds
1-6, test seeds 7-8, same 22 zero-variance features excluded):**

| model | ROC-AUC | PR-AUC | precision/recall/F1 @ threshold=0.75 |
|---|---|---|---|
| histgradientboosting | 0.836 | 0.370 | 0.426 / 0.362 / 0.392 |
| randomforest (chosen) | 0.828 | 0.381 | 0.837 / 0.225 / 0.355 |
| logisticregression_baseline | 0.565 | 0.063 | 0.184 / 0.113 / 0.140 |

Test set: 9,794 rows, 160 positive (1.63%). RandomForest's confusion
matrix at threshold=0.75: `[[9627, 7], [124, 36]]` -- 36 true positives,
only 7 false positives (83.7% precision when it does predict an
upcoming fault; still misses ~78% of them at this fixed threshold,
recall 22.5%). HistGradientBoosting trades some precision for more
balanced recall (36.2%).

ROC-AUC roughly 0.52-0.56 -> 0.83-0.84; PR-AUC roughly 0.02-0.024 -> 0.37-0.38
(15-18x). The permutation feature importance for the chosen model
(`results/figures/ml_feature_importance.png`) confirms WHY: the top
three features are `latency_ms_slope_10`, `latency_ms_slope_5`, and
`latency_ms_mean_3` -- the model is learning the ramp trend the fix
introduced, not some unrelated artifact. This is a coherent result:
predictive power came specifically from giving the anticipation window
genuine precursor signal, exactly as diagnosed above.

**Still worth stating plainly:** this dataset's positive-class precursor
signal is, by construction, a designed ramp -- a real methodological
choice recorded here, not a claim that real-world link failures always
ramp this cleanly or this fast. `logisticregression_baseline` staying
weak (ROC-AUC 0.565) even with the ramp signal present is a useful
negative result too: the relationship between the engineered features
and the label is not simply linear, which is part of why
HistGradientBoosting/RandomForest were included as candidates rather
than shipping only a linear baseline.

## RL simulator calibration: throughput excellent, latency queueing model overestimates real delay (Phase 16)

`rl/env/simulator.py` is a pure-numpy M/M/1-style queueing approximation
standing in for real Mininet during RL training (Phase 17), because
PPO's step counts (200k-500k) are infeasible against real Mininet -- each
step would need real wall-clock network I/O. This substitution is stated
openly here and in the technical paper, not hidden, per the project's own
requirement. `rl/calibrate.py` compares the simulator against real
Mininet for 4 identical scenarios (same link capacity/delay, same
offered UDP load) — full results in
`results/tables/simulator_calibration.json`.

**Two real bugs in the calibration script itself, found and fixed before
trusting any result:** (1) iperf3's UDP JSON reports `bits_per_second` as
the SENDER's offered rate, not what was actually received — achieved
throughput needed `offered * (1 - loss_fraction)`, confirmed by an
initial run showing ~15Mbit "received" on a 10Mbit-capped link;
(2) `ping` measures round-trip time, but the simulator's `latency_ms` is
one-way (matches the topology YAML's per-link `delay`) — halved before
comparing. Both fixed, then re-measured for real.

**Throughput calibration is excellent** for under/near-capacity traffic:
relative error 0.008%-0.08% across three scenarios at 20-80% of link
capacity. The over-capacity scenario (150% offered) is far less accurate
(45% relative error) — real Mininet delivered more throughput
(12.03 Mbit) than the simulator's simple droptail approximation predicts
(6.67 Mbit) when significantly oversubscribed, likely because TCLink's
real token-bucket rate limiting allows some burst tolerance the model
doesn't capture.

**Latency calibration reveals a genuine, real finding, not a bug:** at
low utilization (20%), the M/M/1 queueing model is reasonably close
(12% relative error). At 80% utilization, it overestimates badly (234%
relative error) — the model predicts +4.8ms of queueing delay on top of
the 2ms propagation delay, but real measured latency was 2.04ms,
essentially just the propagation delay with almost no queueing
contribution at all. Real Mininet/TCLink links do not build up
M/M/1-predicted queueing delay the way the idealized formula assumes,
at least not within a 5-second test window at this load level — plausible
explanations include TCLink's actual queue discipline (Linux HTB/TBF)
having enough buffering to absorb short bursts without delay build-up,
or `ping`'s own small packets experiencing different treatment than the
bulk UDP flow being measured. This was NOT worked around with an
uncalibrated fudge factor on the simulator's queueing term -- that would
trade one unverified number for another. It is reported as-is.

**Consequence for Phase 17 RL training, stated plainly:** the reward
signal's latency component will be based on a queueing model that
systematically overstates the latency penalty of routing through a
congested link relative to real Mininet behavior. This is a conservative
bias (the trained policy will likely be MORE averse to congestion than
strictly optimal, not less), not a dangerous one, but it means absolute
latency-related reward magnitudes from simulator-trained behavior should
not be presented as calibrated real-world latency predictions -- only the
*throughput* side of the simulator has been calibrated to that standard.
This should be stated explicitly in the technical paper's RL methodology
section, exactly where the sim-to-real gap is normally addressed in the
RL literature.

## Phase 17: RL state's degradation-probability feature is a proxy, not the real classifier

The RL training environment (rl/env/network_env.py) includes a
"max predicted degradation probability" feature per candidate path in
its state vector, matching the live controller's design (Phase 10's
InferenceClient feeds the same kind of signal into real remediation
decisions). During RL training, however, this value is NOT produced by
calling the actual trained ML classifier (service/ml_rl_service.py,
Phase 9). That model was trained on real Mininet-derived telemetry
feature distributions (45 features over rolling windows of real
utilization/loss/latency/error-rate samples). NetworkSimulator's
per-step outputs are a fast M/M/1-style approximation, not that same
distribution, and Phase 16's calibration effort covered throughput/
latency accuracy only -- it never validated that the trained classifier
would produce meaningful probabilities on simulator-shaped inputs.
Calling it anyway would silently combine two unvalidated approximations.

Instead, `_proxy_degradation_prob()` computes a deterministic function
directly from the simulator's own ground-truth state for that step:
`0.5*utilization + 0.3*loss_rate + 0.2*(1 if any fault active else 0)`,
clipped to [0, 1]. This is honestly a heuristic stand-in: it moves in
the same direction a real predictor's output would (higher under real
stress, near-zero when healthy) and gives the policy a consistent
training signal to condition on, but it is not a claim that these
values are calibrated probabilities, and the trained RL policy's
learned sensitivity to this feature should not be presented as evidence
about the real ML classifier's behavior. This is a second, independent
sim-to-real gap on top of the queueing-delay one documented above, and
should be stated in the technical paper's RL methodology section
alongside it.

## E8 RESOLVED: both safety criteria now demonstrated directly

Re-run 2026-09-12 with the evidence fields in place (ids 344-348). The
entry below described an experiment that passed while measuring nothing;
this is what it measures now.

| trial | service killed | predictions after the kill | fallbacks after the kill |
|---|---|---:|---|
| seed1 | yes | **0** | -- |
| seed2 | yes | **0** | -- |
| seed3 | yes | **0** | -- |
| seed4 | yes | **0** | -- |
| seed5 | yes | **0** | `rl_service_unavailable` |

**Zero predictions after the model became unreachable, in every trial.**
That is the central claim -- an unavailable model yields no prediction
rather than a guessed one -- and it is now measured directly rather than
inferred. One trial additionally exercised the routing fallback after the
kill and recorded its reason, which is the second stated criterion.

The detection rate for E8 (20%) is not a defect and should not be read as
one: the inference service is deliberately killed 20s into the fault, so
for most of each trial there is no model to detect anything with. E8 is a
safety experiment, not a detection experiment, and its detection column
measures the cost of the failure being injected.

## E8 passed, but the experiment was not recording its own evidence

E8 kills the inference service mid-run to check that the system degrades
safely. Its result JSON reported **zero** RL fallbacks across all five
trials, which reads as a null result.

The database said otherwise. In the trial DB:

* `route_changes` carried `rl_fallback = 1` with reason
  `rl_service_unavailable`, so the safety gate did fire and did record a
  reason;
* `predictions` stop **dead at 10:26:42**, the moment the service was
  terminated. Not one prediction afterwards. That is the central claim --
  an unreachable model yields no prediction rather than a guessed one --
  and it held.

Two separate problems produced the misleading summary:

1. **The runner counted fallbacks only from fault onset onward.** The
   fallback it missed happened at controller startup, before the fault,
   when the service had not finished coming up. Correct behaviour, and
   correctly filtered out of "what happened during this fault" -- but it
   meant E8's stated success criterion ("rl_fallback=1 recorded with a
   reason") could never be satisfied by the data it looked at.
2. **The kill lands after the interesting work is done.** It fires 20s
   after fault injection, and in these trials detection and remediation
   completed 14-16s in. So no RL call happened after the kill, which is
   why there was no post-kill fallback to count. The experiment was
   measuring a window in which nothing was supposed to happen.

`_timeline_from_db` now records `service_killed_at`,
`n_predictions_after_service_kill` and `rl_fallbacks_after_service_kill`,
so the claim is measured directly instead of inferred from a count that
excludes it. E8 will be re-run against those fields.

**The pattern again, in its cheapest form.** The evidence for the safety
property existed in the database the whole time; the experiment simply
never looked at it and reported a number that meant something else. An
experiment has to capture the thing it claims, not a quantity that
correlates with it on a good day.

## E6 ANSWERED: the policy prefers higher-capacity paths than the formula

Resolved 2026-09-12, from the record rather than a reconstruction. The
retraction below stands -- the earlier "the RL routes worse" conclusion
was wrong -- and this is what the evidence actually shows.

With `route_changes.decision_inputs_json` in place, every routing
decision in the valid E6 batch (ids 309-338) can be replayed against the
exact inputs the selector was shown, and the weighted-cost formula run on
those same inputs for comparison. Only decisions offering two or more
candidates count; a single candidate is not a choice.

| arm | decisions | agreed with formula | differed | chose **wider** | chose narrower | same capacity |
|---|---:|---:|---:|---:|---:|---:|
| P1 hand-tuned formula | 31 | **31** | 0 | -- | -- | -- |
| P2 learned policy | 44 | 11 | 33 | **24** | 3 | 6 |

**P1 agreeing 31 of 31 is the control for the method.** P1 *is* the
formula, so anything other than perfect agreement would mean the replay
was misreading the record. It does not.

**Where the policy differs from the formula, it picks the
higher-capacity path 8 times as often as the lower one** (24 vs 3). That
is a real, measured behavioural difference, and because P1 and P2 differ
in nothing but path selection, it is attributable to the learned policy
and nothing else.

Worked example from one trial, showing why the earlier conclusion
inverted:

```
candidates offered            formula cost     chosen
[1, 3, 6]        5 Mbit           0.50         <- formula would pick this
[1, 2, 3, 6]    10 Mbit           0.75
[1, 2, 5, 6]     8 Mbit           0.75
[1, 4, 5, 6]    10 Mbit           0.75         <- the policy picked this
```

The *formula* is the one biased toward the narrow 2-hop path here: its
cost function weights summed latency and hop count, and `hop_count` at
0.25 does not offset a short path that happens to be the thinnest. The
policy went the other way.

**What this does NOT establish.** That wider is always better, or that
P2's end-to-end outcomes beat P1's. E6's aggregate throughput figures do
not show that (P1 97.6 +/- 0.5%, P2 86.0 +/- 16.8%), and on this topology
that metric is confounded anyway -- the shortest-hop path is also the
lowest-capacity one, so any reroute at all gains throughput, as recorded
elsewhere in this file. The defensible claim is about decision quality,
which is exactly what E6 was written to isolate: **the learned policy
makes systematically different and more capacity-aware path choices than
the hand-tuned cost function.** Whether that converts into better
end-to-end performance on a topology where the metric is not confounded
is future work, not a result.

**The methodological point, which is the transferable one.** This
question was open for two days and produced one confidently wrong
conclusion, and the thing that settled it was not a better experiment --
it was recording what the system was looking at when it decided. The
aggregate outcome metrics could not distinguish a bad decision from a
different input; the decision record could, immediately.

## RETRACTED: the E6 conclusion below is not supported by the evidence

**The section that follows is kept for the record but its conclusion is
withdrawn (2026-09-12).** It states that the learned policy routes worse
than the hand-tuned formula. That may be true, but this project cannot
currently demonstrate it, and the claim should not appear in the paper
in either direction until it can.

**What broke the argument.** The E6 conclusion was built from each
trial's `final_path` and its aggregate throughput. Two checks undermined
it:

1. **Replaying a real decision offline inverted the result.** Taking one
   recorded reroute (E6_P2_ML_RL_gradual_degradation_seed3, the move onto
   the 5 Mbit path) and feeding the metrics measured at that moment to
   both selectors: the *heuristic* picked the 5 Mbit path (cost 18.8,
   its lowest) and the *RL* picked the 10 Mbit path. That is the exact
   opposite of what each did live.
2. **`final_path` is not a decision.** That trial made FOUR reroutes --
   10 Mbit, then 8, then 5, then back to 10 -- and `final_path` recorded
   only where it happened to stop when the measurement was taken. An
   aggregate over stopping points is not a measure of decision quality.
   (The oscillation itself is a real finding, and E4 is the experiment
   for it.)

Either the live selector saw different inputs from the ones
reconstructable from the database, or the offline replay misstates the
state it builds. **Both explanations are consistent with the record, and
they imply opposite fixes**, so no conclusion can be drawn.

Also corrected here: an intermediate diagnosis that the training load was
at fault (a fixed 4 Mbit demand, below every path capacity, so wider
paths earned no reward) was **tested and disproved**. Retraining with the
demand randomised over 2-12 Mbit produced a policy with a LOWER recovery
rate on the same evaluation (0.28 vs 0.84), and a direct probe showed
both the old and the new policy choosing the 10 Mbit path in 60 of 60
simulated reroutes. The training load was never the problem. That change
was reverted and the original policy restored.

**What was done about it.** `route_changes` now carries
`decision_inputs_json`: the candidate set, the per-link metrics, the
degradation probabilities, the degraded link and the current path --
recorded at the moment of every decision, next to what was chosen. E6
will be re-run once that data exists, and the question answered from the
record instead of from a reconstruction. A schema column is a small
price for the difference between "the policy chose badly" and "the
policy was shown something different", which is otherwise invisible.

**Sixth instance of the recurring root cause, with a twist.** The first
five were measurements taken under conditions that did not match what
was claimed. This one is a measurement that could not be *interpreted*
at all, because the system did not record what it was reacting to. The
lesson for the paper is the same in both cases: an experiment must
capture its own inputs, not just its outputs.

## E6 result: the learned policy is WORSE than the hand-tuned formula

With the hold action masked (previous entry), the RL made all 9 of P2's
selections -- zero fallbacks, against 7 of 11 before. E6 is now a valid
test of its claim, and the claim does not survive.

| | chose 10 Mbit path | chose 5 Mbit path | throughput after / before |
|---|---:|---:|---|
| P1 hand-tuned formula | 8 of 9 | 1 | 1.00-2.63, **never below 1.0** |
| P2 learned policy | 1 of 9 | 5 | 0.54-1.93, **7 of 15 below 1.0** |

Aggregate throughput retained: P1 **97.5 +/- 0.4%**, P2 **89.8 +/- 10.4%**
-- a worse mean and 26x the variance. Recovery time is indistinguishable
(99.8 +/- 33.7s vs 95.8 +/- 45.5s), so the difference is in path choice,
which is exactly what E6 isolates.

Per-trial, 6 of the policy's 9 reroutes moved traffic to a
LOWER-capacity path than it was already on; the formula did that once.
Three of the policy's moves made throughput worse by 25-46%. Rerouting
onto a worse path is not a repair.

**This is not an unfair comparison, and it was checked.** Both selectors
receive identical inputs: `compute_path_cost` and the RL state both see
per-link utilization, latency, loss and hop count, and NEITHER is given
link capacity. Capacity enters both indirectly and identically, through
utilization -- the same traffic on a 5 Mbit link reads a higher ratio
than on a 10 Mbit one. The information needed to avoid the bad path was
present in the policy's observation; it did not use it.

**Most likely cause, stated as a hypothesis because this experiment does
not isolate it.** The policy is trained in the numpy simulator, whose
latency model is already documented above as overestimating real delay
-- 12% error at moderate load but **234% at 80% utilization**. A policy
trained against a distorted latency signal and then deployed on
Mininet is optimising a cost surface it will not meet. The hand-tuned
weights, by contrast, were set against the real system
(utilization 1.0, latency 1.0, loss 2.0, hop_count 0.25 -- note how
heavily hop count is discounted, which is precisely the term the policy
appears to over-weight). Confirming this would need a retrained policy
against a corrected latency model, which is future work, not a result.

**Consequence for the paper. The RL must not be presented as an
improvement.** Two of the project's headline claims are now falsified by
its own experiments: E3 removed "prediction detects earlier", E6 removes
"the learned policy routes better". What survives is real and worth
stating plainly:

* a closed detection-to-verified-repair loop that works end to end;
* ML-based detection with measured per-fault coverage, including the two
  fault types it cannot see and why;
* a second detection path for hard link failures that took that fault
  from 0/5 to 5/5;
* and a measurement methodology that caught five separate instances of
  results being produced under conditions that did not match what was
  claimed -- including this one, where the experiment ran perfectly and
  measured the wrong thing.

A project that reports two falsified hypotheses with the evidence that
falsified them is more defensible than one that reports four successes
and cannot show its working.

## E6, attempt 1: the RL was credited for decisions the heuristic made

E6 exists to justify the RL half of the project: P1 and P2 are identical
except for path selection, so any difference between them is
attributable to the learned policy. The first run showed no difference
-- P1 and P2 statistically indistinguishable on every metric (recovery
68.3 +/- 17.3s vs 68.5 +/- 29.9s, throughput retained 97.4 +/- 0.4% vs
97.9 +/- 0.4%, overlapping intervals).

That tie is not a result about the RL. Checking which component actually
chose each path:

| | selections | made by |
|---|---:|---|
| P2, policy chose | 4 | the RL |
| P2, policy declined | **7** | the weighted-cost heuristic |

In 7 of 11 selections the policy returned action 0 ("do not reroute"),
`remediation_manager` recorded `rl_selected_no_reroute`, discarded the
answer and fell back to the heuristic. So roughly two thirds of P2's
decisions were made by P1's algorithm, and E6 was substantially
comparing the heuristic against itself. A tie was the expected outcome
of the setup, and says nothing about path quality.

**Why the policy kept declining, and why masking is the fix.** The
policy is consulted only after the healing loop has opened an incident
and persistence is satisfied -- the decision to act has already been
made. "Hold" in that context contradicts the call itself. The action
mask is the mechanism built for exactly this: `get_action_mask` now
takes `allow_hold`, and remediation passes False, so MaskablePPO
renormalises over the reroute actions and the policy returns its best
REROUTE rather than an answer the caller will throw away. Hold remains
valid when there are no candidates, since a mask with no valid action is
undefined.

This is a production fix, not an experiment-only switch. The previous
behaviour did not respect the policy's veto either -- it overrode it with
the heuristic. The change is about which component decides when the
policy declines, and the honest answer is the one the system is built to
use.

**The first E6 batch (ids 203-232) is therefore not a valid test of its
own claim** and is excluded in `results/invalidated_runs.json`. Note it
is not wrong data -- the trials ran cleanly, no duration anomalies, no
errors -- it simply does not measure what E6 says it measures.

**Fifth instance of the same root cause**: a measurement taken under
conditions that did not match the conditions being claimed. The first
four were the controller's inference inflating its own latency, dataset
generation under a load the model would never see, the harness
under-loading the network, and the observer starving the VM of CPU. This
one is the subtlest -- the experiment ran perfectly and measured the
wrong thing -- and it is the strongest argument for the practice that
caught it: checking WHICH COMPONENT produced each decision, not just what
the aggregate numbers say.

## E3 FALSIFIES the lead-time claim, and exposed an inflated detection metric

E3 was written to test the project's central claim -- that prediction
detects a fault BEFORE a threshold-based system can -- and its own
definition states that "a non-positive gap would falsify it and must be
reported as such". The gap is non-positive. Reported here as such.

**First, a measurement bug the experiment surfaced.** B2's raw numbers
looked excellent: 10/10 detection. But six of those ten detections
carried latencies of 166.2-168.6s, clustered within 2.4s of each other,
against a fault window of 162s (12s ramp + 150s hold, confirmed against
the injector's own ground truth in
`results/raw/experiments/*/faults.jsonl`, which records fault_end at
150.0-150.1s after full intensity). Those "detections" landed 4-6s AFTER
the fault had been removed -- one telemetry cycle later. They are not
detections of the fault; they are reactions to the recovery transient,
when bandwidth and delay snap back and queues flush.

Counting them was inflating a reactive system's detection rate AND its
mean latency, the latter with values that are really just "the length of
the fault window". `analyze.py` now classifies every detection against
the fault window and reports `detection_rate_in_fault` and
`detection_latency_in_fault_s`, with late detections counted separately
in a `late` column rather than dropped silently -- they are real events
the system logged, just not detections. E1 and E2 were re-checked under
the same rule and have **zero** late detections, so only E3's numbers
move.

**The result, on genuine in-fault detections:**

| | detects | when it does | late |
|---|---:|---:|---:|
| B2 reactive, gradual degradation | 20% (1/5) | 49.8s (n=1) | 4 |
| P2 full system, gradual degradation | **80%** (4/5) | 83.1s | 0 |
| B2 reactive, congestion | 60% (3/5) | 63.0s | 2 |
| P2 full system, congestion | 60% (3/5) | 59.2s | 0 |

P2 is **not** earlier. On congestion the two are statistically
indistinguishable (59.2s vs 63.0s, overlapping intervals). On gradual
degradation P2 is 33s *slower* than B2's single in-fault detection --
and that comparison rests on n=1, so it is weak in both directions.

**What P2 is better at is coverage: 70% vs 40% overall**, and every one
of its detections landed while the fault was still present, where 6 of
B2's 10 did not. That is a real and defensible advantage -- a system
that catches 7 of 10 faults during the fault beats one that catches 4 and
then twitches at the recovery -- but it is a different claim from the one
E3 was written to test, and substituting it silently would be exactly the
kind of retrofitted hypothesis this file exists to prevent.

**Consequence for the paper.** The "predictive means earlier" framing is
not supported by this evidence and must not be used. The defensible
claim is coverage and precision of detection, not lead time. E6 (RL vs
heuristic path selection) now carries more weight as the justification
for the learned components, since E3 has removed one of the two pillars
the project was resting on.

## Host sleep suspends the VM, and the clock step afterwards is the real hazard

This machine is configured to sleep after 1 minute idle, and had slept
and woken 16 times during E2's third attempt. Each sleep suspends the
qemu process, freezing the VM.

The freeze itself is probably benign for correctness: the entire
emulated network -- hosts, switches, links, controller, inference
service -- lives inside that VM, so everything stops and resumes
together, and guest-relative durations stay coherent. Nothing outside
the VM participates in a trial.

The clock step afterwards is not benign. Measured directly: the guest
clock was 76 minutes behind the host (VM 17:51 vs host 19:07), and
minutes later chrony had stepped it forward to match. Recovery and
detection latencies are computed from wall-clock timestamps written to
the database, so a trial spanning that step has its timings inflated by
the size of the step. Unlike the freeze, this corrupts numbers rather
than merely delaying them.

**Mitigations, in order of reliability.** `caffeinate` is held for the
duration of a run so the host cannot sleep and no step occurs -- this
is the actual fix, and it treats the cause. As a backstop, every trial
records `duration_s`, and a trial whose duration is wildly out of family
(~5 minutes here) is flagged and reported rather than averaged in; a
76-minute step is not subtle in that column. The same check catches the
unbounded-iperf3 stall described above, which is why it is worth running
over any batch rather than only when a problem is suspected.

**Not fixed by switching to a monotonic clock.** That would be the
textbook answer for durations, but the timings here are derived from
wall-clock timestamps that several tables record independently and
correlate across (incidents, route_changes, verification_results), and
those need a shared absolute time base. Changing it is a real refactor
with its own risk of skew, not a one-line fix, and is not worth doing
mid-experiment. Recorded here as a known weakness rather than presented
as solved.

## An unbounded iperf3 could take a measurement long after its window

Found while watching E2's third attempt: one trial took 21 minutes
instead of 5. The trial's own record showed nothing wrong -- same
fields, same shape as every other trial. Only the log timestamps showed
that `_measure_flow`'s post-fault sample had sat for **18 minutes**
between the fault window closing and the measurement returning.

`_measure_flow` runs iperf3 through Mininet's `cmd()`, which blocks
until the command exits and has no deadline of its own. A stalled
iperf3 client therefore hangs the trial indefinitely -- and the stall is
most likely exactly when the path is lossy, which is what several of the
injected fault types produce.

**Why this matters beyond wasted time.** The sample is supposed to
describe the network shortly after the fault window. Taken 18 minutes
late, it describes a network where the fault has long since expired and
the system has long since settled -- but it is still recorded in the
`after` field and still feeds `throughput_retained_frac`. A late number
looks exactly like a valid one. That is worse than a missing number,
which the analysis already handles: `_measure_flow` returns None for
anything it could not measure, and the aggregator reports n and marks
underpowered cells rather than filling them in.

Both commands are now wrapped in `timeout`, with `--connect-timeout` on
iperf3 to bound the setup phase specifically, and each measurement
records `measure_elapsed_s` and `measure_timed_out` so an over-long
sample is visible in the trial data rather than only in log timestamps.

**The E2 run in progress was deliberately NOT restarted for this.** Its
60 trials all execute under the already-loaded code, so that run stays
internally consistent, which is what its within-run comparison needs.
The fix applies from E3 onward, and that difference is disclosed here
rather than being smoothed over. Trials from this E2 run whose recorded
duration is anomalous are identified after the fact and reported
separately.

## E2, attempt 2: the measuring apparatus starved the machine under test

Discarded 30 completed baseline trials (2026-09-10). Kept as
`results/E2_runner_DISCARDED_cpu_starved.log`; no number from it appears
anywhere in this project's results.

The runner was launched detached and correctly, and three host-side
watchers were set up to follow it: a status poll, a `tail -f` on the
runner log, and a loop waiting for the run to end. Each held a
`multipass exec` client. All three wedged -- and a wedged multipass
client does not idle, it spins at ~100% CPU indefinitely, after the
remote command has already finished. Measured 3 hours in: three clients
at ~175 minutes of CPU each, taking 3 of this Mac's 8 cores. qemu's CPU
share was 34.6%; killing the three watchers took it to **246%**. The VM
had been running the experiment on roughly a seventh of the processor it
should have had.

**Why the trials were discarded rather than kept with a caveat.** The
slowdown alone would have been tolerable and disclosable. What is not is
*when it ended*: the starvation covered the whole B1 baseline arm and
none of the P2 arm, because the arms run sequentially and the watchers
were killed in between. Host CPU availability would then have been
perfectly confounded with the system under test -- a B1-vs-P2 comparison
would partly be measuring the monitoring bug. Trial wall-times were
stable throughout (~5:05-5:20, dominated by fixed sleeps), so the fault
was invisible in the run's own output; only the host process table
showed it.

**Fixed structurally, not by remembering to be careful.**
`scripts/vm_exec.sh` wraps every VM command in a host-side watchdog that
kills the client unconditionally at a deadline (macOS ships no
`timeout`), and no long-lived `multipass exec` is held any more -- no
`tail -f`, no poll loop wrapped around one. Progress is now sampled
infrequently through that wrapper.

**This is the fourth instance of one recurring root cause in this
project: a measurement taken under conditions that did not match the
conditions being claimed.** The first three were the controller's own
inference inflating the latency it measured, dataset generation running
under a load the model would never see, and the experiment harness
under-loading the network relative to training. This one is the same
mistake made from the outside -- the observer competing with the
observed for CPU. Worth stating in the paper's threats-to-validity
section as a pattern, not four separate mishaps.

## The ML predictor structurally cannot detect a hard link failure

E2 scored 0/5 on `link_failure`, and adding the missing background
traffic did NOT fix it. The per-link data shows why:

| | predictions | max P(degradation) |
|---|---:|---:|
| before the link was cut | 57 | 0.050 |
| after the link was cut | 87 | **0.034** |

The model rated the dead link as *healthier than before it died*. Two
independent reasons, both structural rather than tunable:

1. **A dead link is indistinguishable from an idle one in this feature
   set.** It carries no traffic (utilization ~ 0) and returns no latency
   probes, and `loss_rate`/`error_rate` are identically zero on this
   platform anyway (see the veth/netem entry above). Low utilization and
   absent latency is exactly what a quiet healthy link looks like.
2. **The labels do not cover it.** The model is trained to predict
   degradation ~10 s BEFORE onset (`label.horizon_s`). A link that has
   already gone is not about to degrade; there is no future event left
   to predict.

**Resolved by adding a second detection path, not by retuning.**
`HealingLoop.on_link_down` is driven by LLDP's own link-removal event,
which `TopologyManager` already received and previously discarded.
Persistence is deliberately bypassed there: that gate exists to filter
uncertain *predictions* across several windows, and a link's
disappearance is an observed fact -- making a certainty wait for
corroboration only adds latency. Verified live on the same fault that
scored 0/5: **2/2 detected, recovery in 20.2 s and 20.1 s.**

Two design points worth stating in the paper:

* The capability is given to B2/P1/P2 but NOT to B1. Reacting to a
  link-down event is a control-plane capability, not a learned one;
  attributing it to the ML arm would make P2 beat B2 on `link_failure`
  for a reason that has nothing to do with prediction. B1 remains the
  inert control.
* So the system now has two detection mechanisms for two fault classes:
  gradual degradation via the predictor (with persistence), hard failure
  via topology events (immediate). That division should be described
  honestly rather than presenting a single "detection rate" that
  averages across mechanisms which work entirely differently.

A related state-machine bug surfaced while implementing this: the link
was marked FAILED, then the remediation path overwrote it -- first with
DEGRADED as the incident opened, then AT_RISK once the reroute verified
-- so a physically dead link was displayed as merely "starting to look
risky". Rerouting around a link is not repairing it. FAILED is now
sticky, cleared only by discovery seeing the link return or by the
stale-state expiry.

## Phase 22 E2: the experiment harness under-loaded the network

E2 ran all six fault types (60 trials, B1 control vs P2 full system) and
produced detection rates of 2-3 out of 5 for five fault types and
**0 out of 5 for link_failure**.

That last number is what exposed the problem: the live demo detects the
SAME fault, with the SAME model, in 6 seconds with 3 of 3 readings above
threshold. Same code, opposite result.

The difference is traffic. `demo/run_live_demo.py` and
`ml/dataset/generate_dataset.py` both run continuous 2 Mbit UDP between
three host pairs; `experiments/runner.py` ran only three 10-second
iperf3 bursts, leaving links idle for most of each trial. On an idle
link, cutting it changes nothing observable -- utilization was already
zero -- so a total failure is invisible while a congestion fault (which
still perturbs the little traffic there is) remains partly detectable.

**Consequence: every E2 detection rate measured before this fix is
depressed by the harness, not only by the detector, and none of them
should be quoted.** The B1 control at 0/26 is unaffected and remains
valid -- it confirms the fault injection is real and that detections are
not a harness artefact.

The runner now runs the same continuous load as training and the demo,
with `pingAll` first so the iperf3 clients do not stall on unresolved
hosts. E2 must be re-run before any per-fault claim is made.

This is the third instance of the same root cause in this project: a
measurement path whose conditions did not match the conditions the model
was trained under (see the Phase 21 inference-overhead entry and the
Phase 22 train/serve skew entry above). The recurring lesson is that the
model is far more sensitive to operating regime than to its own
hyperparameters, and any new harness has to reproduce the training
regime before its numbers mean anything.

## Phase 22 E1: the simple reactive baseline outperforms the full system

First complete experiment, 4 systems x 5 seeds x gradual_degradation,
all 20 trials executed live (results/tables/experiment_results.json,
results/figures/e1_comparison.png):

| system | detected | recovery | time to recovery | reroutes | throughput kept |
|---|---:|---:|---:|---:|---:|
| B1 static shortest path | 0% | 0% | n/a | 0.0 | 13% |
| **B2 reactive thresholds** | **100%** | **100%** | **54.0 s** | 2.8 | **137%** |
| P1 ML + heuristic path | 80% | 80% | 70.5 s | 1.0 | 24% |
| P2 ML + RL (full system) | 40% | 40% | 74.1 s | 0.4 | 48% |

**B2 beats the full system on every headline metric on this fault type.**
It detects more often (100% vs 40%), recovers sooner (54.0 s vs 74.1 s)
and retains more throughput. This must be reported as the primary E1
finding; it directly challenges the project's central claim, and
presenting P2 as superior on the strength of any other cut of this data
would be misrepresentation.

Reading it honestly:

* **Why B2 wins here.** `gradual_degradation` drops bandwidth to 15% and
  adds 40 ms -- a large, sustained change that trips a fixed threshold
  almost immediately. Prediction buys lead time only when a fault is
  subtle enough that thresholds do NOT trip early; against an obvious
  fault, the 3-of-4 persistence requirement is pure added latency. The
  project's hypothesis is therefore untested by E1, not merely
  unsupported: E1 uses the fault least able to discriminate the two
  approaches. E2 (all six fault types) and E3 (lead time vs B2) are the
  experiments that can actually support or refute it.
* **P1 80% vs P2 40% is not evidence about the RL policy.** P1 and P2
  share the entire detection path and differ only in path SELECTION, so
  their detection rates should not differ. With n=5 (4/5 vs 2/5) this is
  within noise (Fisher exact p ~ 0.52). It is reported, not explained
  away, but no conclusion about RL should be drawn from it. E6 is the
  ablation designed to isolate that.
* **The >100% throughput figure is real, and it makes the metric
  confounded.** `s1-s3` is provisioned at 5 Mbit while every alternative
  path uses 10 Mbit links, so the shortest-HOP path is the lowest-
  CAPACITY one. Any system that reroutes off it gains throughput
  regardless of whether it correctly identified a fault. "Throughput
  retained" therefore rewards rerouting per se on this topology and
  should not be read as remediation quality; B2's 137% partly measures
  its willingness to reroute 2.8 times. This is a topology design
  problem, and either the topology or the metric needs revisiting before
  throughput is used as a headline comparison.
* **The one dimension where P2 leads is stability**: 0.4 reroutes per
  trial vs B2's 2.8. That is the behaviour persistence and hysteresis
  exist to produce. But it is not a clean win either, since fewer
  detections mechanically mean fewer reroutes; E4 is where stability can
  be measured against systems that detected comparably.

Status: E1 complete and reported as-is. No claim of superiority for the
learned systems is supported by it.

## Phase 22: offline model metrics disagreed with live behaviour

Two models were trained on the same topology, traffic, fault schedule,
seeds and label rules, differing only in the conditions their telemetry
was collected under and in the feature set:

| | Phase 8 model | deployment-matched model |
|---|---:|---:|
| PR-AUC (own held-out test seeds) | 0.381 | 0.262 |
| ROC-AUC | 0.828 | 0.714 |
| detects the fault live, under experiment load | **no** | **yes** |

The Phase 8 model scores better and does not work. Under identical live
conditions it gave a max probability of 0.290 on a link whose latency
had gone 40.9 -> 342 ms while scoring a HEALTHY link at 0.720; the
matched model detected and remediated the same fault (recovery 77.1 s).

Two things this does NOT establish, and which must not be claimed:
  * that the matched model is "better" in a general sense. The two
    PR-AUC figures come from different test distributions and are not
    directly comparable. What is established is only that one detects
    under deployment conditions and the other does not.
  * that one live trial is a detection rate. It is one trial.

What it does establish is methodological: for this system, held-out
offline metrics collected under non-deployment conditions were an
actively misleading guide to live behaviour, and the operating decision
had to be made on the live objective. Any model-selection claim in the
paper should be stated in those terms rather than as a straight
accuracy comparison.

## Phase 21: the controller's own inference calls corrupt the latency it measures

The closed loop was assembled and runs end-to-end against a live
network, but on its first full runs it never fired: 982 real predictions
were written and the maximum probability on the deliberately-degraded
link was 0.193, far below the 0.75 threshold. Investigating rather than
retuning the threshold produced a measured root cause.

**The measurement.** `tests/e2e/measure_probe_overhead.py` runs the
identical controller and topology twice, changing exactly one thing --
whether `service/ml_rl_service.py` is reachable -- and compares the
latency the controller's own probe reports (60s collection each,
480 samples per condition):

| condition | median | mean | p95 | max |
|---|---:|---:|---:|---:|
| inference OFF (0 predictions) | 23.3 ms | 26.4 ms | 53.6 ms | 82.9 ms |
| inference ON (464 predictions) | 464.2 ms | 472.4 ms | 822.5 ms | 1047.4 ms |
| Phase 8 training data | 2.6 ms | — | 32.8 ms | 69.3 ms |

Enabling inference makes the controller's latency measurements ~20x
worse. The mechanism: the latency probe times a controller-emitted frame
from PacketOut to the matching PacketIn, so it measures the controller's
own event-loop responsiveness as much as the link. Every telemetry cycle
the controller issues one blocking HTTP inference call per link inside
that same single-threaded event loop, so probe PacketIns queue behind
that work and are timestamped late.

**This is a feedback pathology, not a property of the network.** The
model's most important features are latency slopes (Phase 9's
permutation importance ranked `latency_ms_slope_10` and `slope_5`
first). Running the model therefore degrades the very signal the model
depends on, and the live feature distribution lands ~200x outside
anything present in training -- so the classifier emits near-zero
probabilities on inputs it has never seen. Both halves are real: a
genuine train/serve skew, and a self-inflicted one.

**Not the cause:** CPU contention. Raising the VM from 4 to 6 vCPUs
moved live mean latency only 517 ms -> 437 ms and left the maximum
probability unchanged at 0.193. That hypothesis was tested and rejected
rather than assumed.

**Residual gap.** Even with inference off, live median (23.3 ms) is ~9x
the training median (2.6 ms), so inference overhead is the dominant
term but not the only one. That remainder is not yet explained and is
NOT claimed to be.

**Resolved 2026-09-05 (the inference half).** Inference was moved off
the OpenFlow event path onto its own greenthread and collapsed from one
HTTP call per link into a single batched call per cycle
(`POST /infer/degradation/batch`). Re-measured with the same A/B
harness: median probe latency with inference ON went 464.2 ms ->
16.6 ms, statistically indistinguishable from inference OFF (17.2 ms),
while still producing 448 predictions per 60 s. A unit test asserts the
batched path returns the same probability as the single-item path, so
this changed round trips, not answers.

**A second, separate harness flaw, also fixed.** The E2E test injected
its fault immediately after the initial path install, so the fault ramp
coincided with the controller's startup transient: probe latency was
*falling* on every link (s1-s3 41.8 -> 33.9 ms, healthy s2-s3
19.7 -> 13.6 ms) exactly while the fault ramped in. Since the model's
strongest features are latency SLOPES (Phase 9 permutation importance),
a settling-induced downward slope is the inverse of the trained pattern.
Phase 8's training runs always injected into an already-settled network,
so the harness was not a fair test. It now settles for 90 s first.

**With both fixed, the detector demonstrably works -- and is one reading
short of the configured gate.** During the fault window the model ranks
the genuinely degraded link first, by a wide margin:

| link | max P(degradation) | |
|---|---:|---|
| s1-s3 | **0.853** | the injected fault |
| s2-s5 | 0.463 | healthy |
| s1-s2 | 0.280 | healthy |
| (5 others) | <= 0.243 | healthy |

and the underlying signal is real: s1-s3 latency went 20.5 ms before the
fault to 39.1 ms during it.

**RESOLVED 2026-09-05.** Two further changes closed it, both justified
independently of whether they made the test pass:

1. `ml.threshold` moved 0.75 -> 0.40, chosen by a sweep on the HELD-OUT
   test seeds (`ml/threshold_sweep.py`,
   `results/tables/ml_threshold_sweep.json`), where F1 peaks (0.416 at
   precision 0.558 / recall 0.331, vs 0.355 at 0.75). The original 0.75
   was fixed in Phase 1 before any model existed. The abstention band
   moved to [0.25, 0.40] so the rule is monotonic (healthy -> uncertain
   -> degraded); a new config test enforces that ordering.
2. Predictions are now aggregated to ONE reading per link per telemetry
   cycle (max across the two switch-sides). Previously each link was
   scored twice per cycle, so `persistence.window = 4` spanned ~4.2 s
   rather than the 4 telemetry windows (~8 s) the spec means -- measured
   live at 1.9 readings/cycle. It also let a link's two endpoints count
   as independent corroboration of each other, which is precisely what
   the persistence gate exists to prevent.

`persistence.consecutive` was deliberately NOT relaxed. With the loop
running end to end, the live result is:

    fault injected on s1-s3 (carrying traffic)
    +18 s  incident opened, P=0.420, persistence confirmed
           rerouted s1,s3,s6 -> s1,s2,s3,s6 (selector=P2_ML_RL, no fallback)
    +27 s  verification SUCCESS, 0 retries, incident closed SUCCESS

**Reliability, measured honestly: 2 of 3 consecutive live runs detected
and remediated the fault; the third opened no incident at all.** The two
successes also selected *different* replacement paths (s1,s2,s3,s6 and
s1,s4,s5,s6), both correctly avoiding the degraded link, which is
evidence the RL policy is deciding from live conditions rather than
replaying a fixed answer.

A ~2/3 detection rate is consistent with the model's measured per-reading
recall (0.331) combined with a 3-of-4 persistence requirement: whether
the loop fires depends on whether that run's scattered detections happen
to cluster inside a 4-window span. Three runs is far too small a sample
to put a number on this -- the 95% interval on 2/3 spans roughly
0.09-0.99 -- so NO detection-rate claim should be made from it. Phase 22
is where this gets measured properly, across >=5 seeds per configuration,
and that is the figure the paper must use.

What IS established: the closed loop is functionally complete and
verified end to end against a live network. What is NOT established is
how often it succeeds.

The historical record below is retained because the diagnosis is what
produced the fix.

**Original status (open, precisely characterised).** Of 258 readings during the
fault, exactly 2 crossed the 0.75 threshold, and they were consecutive.
The best persistence window achieved was 2-of-4; `persistence.consecutive`
requires 3. The distribution falls off a cliff after the top two
readings (0.853, 0.837, then 0.533, 0.520, 0.517, 0.480). So the loop
does not fire because the model's signal is strong but brief, not
because any component is broken.

Closing this requires a deliberate decision that is NOT a bug fix, and
must be reported as a tuning choice rather than made silently:
  * relax `persistence.consecutive` from 3-of-4 to 2-of-4 (still rejects
    single-sample blips, and would fire on this run), and/or
  * lower `ml.threshold` -- constrained by `persistence.uncertainty_band`
    [0.45, 0.60], since a threshold inside that band collides with the
    abstention rule, and/or
  * retrain so the signal is sustained rather than a 1-cycle spike.

Note also that predictions are currently produced once per (link,
switch-side), i.e. twice per telemetry cycle per link, so a persistence
window of 4 spans only ~2 real telemetry cycles rather than the 4 the
spec's "3 of the last 4 windows" wording implies. Any retune should
settle that semantics question at the same time.

Until one of those is chosen and re-measured, no experimental claim
about end-to-end detection performance may rest on the live closed
loop.

Further entries (queue-occupancy availability, etc.) are added as those
phases are reached.
