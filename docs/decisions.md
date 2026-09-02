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
- 2026-08-30: Because Ryu's controller role only needs a TCP listener that
  OpenFlow-speaking switches can reach (it does not itself need to create
  network namespaces), venv-ctrl can potentially run on the macOS host while
  Mininet/OVS run inside the Linux VM, with the VM's OVS instances
  configured with `--controller=remote,ip=<host-ip>,port=6653`. This will be
  evaluated for real at the Phase 2 GO/NO-GO smoke test; if it adds friction
  the fallback is to run both venv-ctrl and Mininet inside the VM.
