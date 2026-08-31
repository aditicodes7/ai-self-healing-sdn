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
