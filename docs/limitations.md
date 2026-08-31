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

## rising_errors fault: not observable via OVS rx_errors/tx_errors (Phase 7)

`network/failure_injector.py`'s `rising_errors` fault applies a real
`tc qdisc ... netem corrupt <pct>%` to the link's interfaces -- genuine
bit-level corruption, not a fabricated value. Verified this is really
applied: during a 6s, corrupt=8% fault on s1-s2 (2026-08-31), the
switch-side port's `rx_bytes` counter grew only ~2.8KB over the whole
window (828020 -> 830798) versus hundreds of KB/s normally under
comparable traffic -- a real, large effect on delivered traffic.

However, `telemetry.rx_errors`/`tx_errors` (from `OFPPortStatsRequest`)
stayed at exactly 0 throughout, and across the entire test run (638
telemetry rows, `SUM(rx_errors)=0`, `SUM(tx_errors)=0`). By contrast the
`packet_loss` fault's effect IS visible this way (`SUM(rx_dropped)=232`
over the same run). Root cause: Mininet's links are Linux `veth` pairs,
which pass `sk_buff`s directly in kernel memory and do not perform the
hardware-level FCS/CRC validation a real NIC does -- there is no physical
frame-check step to fail, so nothing increments a hardware-style error
counter the way it would on real hardware. This is a genuine property of
software-emulated links, not a bug in the injector or the telemetry
collector.

**Consequence for later phases:** `error_rate` (derived in
`common/features.py` from `rx_errors`/`tx_errors` deltas) will be ~0 for
`rising_errors`-labeled windows in the Phase 8 dataset, same as for
healthy windows. The ML model (Phase 9) will not be able to learn this
fault type from `error_rate`; whatever signal exists will have to come
indirectly, from the drop in `utilization`/throughput and any latency
effect, exactly like a mild `packet_loss` or `congestion` case. This
should be stated explicitly in the technical paper's dataset/feature
discussion, and `rising_errors` classification accuracy (if reported
separately per fault type) should be expected to be no better than what
those indirect signals allow -- not artificially inflated by adding a
fabricated error-count feature to compensate.

Further entries (RL simulator calibration, queue-occupancy availability,
etc.) are added as those phases are reached.
