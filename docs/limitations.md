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

Further entries (RL simulator calibration, queue-occupancy availability,
etc.) are added as those phases are reached.
