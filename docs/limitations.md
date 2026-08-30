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

Further entries (RL simulator calibration, latency-as-estimate,
queue-occupancy availability, etc.) are added as those phases are reached.
