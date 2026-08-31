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
Mininet, on `network/topologies/scenario_a.yaml`:

| link | configured one-way delay | probe estimate (avg of 8 samples) | independent ping RTT (h1<->h2 / h1<->h6) |
|---|---|---|---|
| s1-s2 | 2ms | ~21ms (10-40ms range) | 4.19-5.39ms (h1->h2, direct) |
| s1-s3 | 1ms | ~21ms (10-39ms range) | -- |
| s3-s6 | 2ms | ~18ms (13-27ms range) | 6.20-7.45ms (h1->h6, via s1-s3-s6) |
| s2-s5 | 3ms | ~18ms (14-23ms range) | -- |

**Finding:** the probe estimate runs several times higher than the real
link delay, AND is roughly the same magnitude (~15-20ms) across all four
links regardless of their different configured propagation delays
(1-3ms). This indicates the estimate is currently dominated by
controller-side eventlet greenthread scheduling/queueing overhead (the
probe loop, echo loop, telemetry poll loop, and OpenFlow event dispatch
all share one cooperatively-scheduled reactor thread under Ryu), not by
the physical link propagation delay the technique is meant to measure.

**Why this is reported, not hidden or "fixed" by adjusting numbers:** the
subtraction of half the OFPEchoRequest RTT (see telemetry_collector.py)
already accounts for one control-channel hop as designed; the residual
overhead is real, reproducible measurement error inherent to a
single-threaded green-thread control plane under concurrent load (LLDP,
telemetry polling, echo, and probe traffic all contending for the same
event loop), not a bug being papered over. Two runs (2026-08-31) produced
consistent results.

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
