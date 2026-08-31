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

## Current phase

**Phase 2 — COMPLETE.** Env setup, both venvs, GO/NO-GO smoke test, of_compat.

## Completed components

**Phase 1 (repo skeleton, config, logging, DB):**
- Repository root initialized at `the project directory`
  (git repo, no nested `ai-self-healing-sdn/` subdirectory — see
  decisions.md).
- `config/config.yaml` — full tunable set (telemetry, features, label, ml,
  persistence, stability, path, rl, flow, verify, remediation, traffic,
  database, api, seeds).
- `common/config_loader.py` — frozen dataclasses, `load_config()`,
  `ConfigError` on missing/malformed sections, validates
  `uncertainty_band` ordering.
- `common/logging_setup.py` — stdlib logging, one-JSON-object-per-line
  formatter, `configure_logging(name)`, no duplicate handlers.
- `common/schema.py` — shared enums (LinkHealthState, IncidentStatus,
  VerificationClassification, RouteSelector, DataSource, FlowRuleResult).
- `database/schema.sql` — all 11 tables (telemetry, features, predictions,
  links, topology_snapshots, incidents, route_changes, flow_rules,
  remediation_events, verification_results, experiment_runs) + indexes on
  (timestamp), (link_id, timestamp), (incident_id).
- `database/db.py` — WAL-mode sqlite3 connection helper, `init_schema()`,
  `Database` context manager with generic `insert`/`query`/`execute`.
- `venv-ml` created on macOS with Python 3.11.9; `pyyaml`, `pytest`,
  `pytest-cov` installed.
- `pytest.ini`, `Makefile`, `requirements-ctrl.txt`, `requirements-ml.txt`,
  `.gitignore`, `README.md`, `LICENSE` (MIT), `docs/decisions.md`,
  `docs/limitations.md`.

**Phase 2 (env setup, GO/NO-GO smoke test, of_compat):**
- **Linux VM:** Multipass `sdn-vm`, Ubuntu 22.04.5 LTS, 4 vCPU / 4GB RAM
  allocation (3.8GB usable), 20GB disk. Created via `multipass launch`.
- **Repo delivery to the VM:** NOT a live mount. Multipass's classic
  (SSHFS) mount corrupted venv symlinks and later started rejecting all
  reads; the native (virtiofs) mount failed outright on macOS TCC
  permissions for `~/Desktop`. Using `multipass transfer -r` to push
  point-in-time snapshots into `/home/ubuntu/ai-self-healing-sdn` instead
  — re-run after any edit that needs VM-side testing. Full reasoning in
  `docs/decisions.md` (2026-08-31).
- **venv-ctrl:** Python 3.9.25 (deadsnakes PPA), lives at
  `$HOME/venv-ctrl-ai-self-healing-sdn` inside the VM (outside the repo
  directory, deliberately — see decisions.md). Built by
  `scripts/setup_env.sh ctrl`, which works around a real Ryu 4.34 packaging
  bug (pbr reports version 0.0.0 outside a git checkout, so a plain
  `pip install ryu==4.34` fails both with and without build isolation) by
  building the wheel manually with `PBR_VERSION=4.34` set and a pinned
  `setuptools<60`. `dnspython` is pinned to `1.16.0`, not the originally
  specified `2.2.1`, because `eventlet==0.30.2` itself declares
  `dnspython<2.0.0,>=1.15.0` — installing 2.2.1 would conflict with
  eventlet's own requirement, not just pip's resolver being strict.
- **GO/NO-GO smoke test result: PASS with `ryu` backend** (no os-ken
  fallback needed). `from ryu.base import app_manager` imports cleanly;
  `ryu-manager ryu.app.simple_switch_13` plus
  `sudo mn --controller=remote,ip=127.0.0.1,port=6653 --topo=single,3 --test pingall`
  returned **0% dropped (6/6 received)**. Re-verified end-to-end from a
  fully clean `scripts/setup_env.sh ctrl` run (second, independent run) —
  same result, confirming reproducibility.
- `config/config.yaml`: `project.controller_backend` now set to `ryu`
  (was `null` pending this test).
- `controller/of_compat.py` (the shim itself) and
  `tests/unit/test_of_compat.py` (AST-based import-hygiene check, needs no
  ryu/os_ken installed, runs under venv-ml) both pass. Additionally
  verified under the real venv-ctrl: `of_compat backend: ryu`.
- Mininet + Open vSwitch installed in the VM via apt
  (`mininet`, `openvswitch-switch`).
- `requirements-ctrl.txt` updated to reflect the actual working pin set
  (dnspython 1.16.0, plus ryu's full declared runtime deps: msgpack,
  netaddr, oslo.config, ovs, routes, six, tinyrpc, webob) and now documents
  that `scripts/setup_env.sh ctrl` — not a plain `pip install -r` — is the
  supported setup path.
- `scripts/setup_env.sh` — reproducible, idempotent setup for both venvs;
  `ml` and `ctrl` subcommands, `all` runs both.
- `Makefile` targets `setup-ml`/`setup-ctrl` now call `scripts/setup_env.sh`;
  `test-ctrl` checks `of_compat` resolves against the real ryu install
  (pytest is not part of the approved venv-ctrl stack, so it isn't used
  here).

## Pending components

Phase 3 onward: topology builder + iperf3 traffic generator, then the Ryu
L2-forwarding/topology-discovery app.

## Known issues

None blocking. Noted for awareness: VM-side testing requires an explicit
`multipass transfer -r` re-sync after Mac-side edits (no live mount).

## Configuration

See `config/config.yaml` for the full set. Notable defaults: ML threshold
0.75, uncertainty band [0.45, 0.60], persistence 3-of-4 windows, cooldown
30s, route hold 60s, min improvement 15%, telemetry interval 2s, label
horizon 10s, verify settle 8s. `project.controller_backend: ryu` (decided
Phase 2).

## Test status

27/27 unit tests passing (`tests/unit/test_config.py`, `tests/unit/test_db.py`,
`tests/unit/test_logging.py`, `tests/unit/test_of_compat.py`) under
`venv-ml` (Python 3.11.9) on macOS, 97% coverage on `common/` + `database/`.
Phase 2's GO/NO-GO smoke test (real Mininet + real Ryu, in `sdn-vm`) passed
twice independently: 0% dropped (6/6 received) both times.

## Experiment status

Not started (Phase 22).

## Documentation status

`docs/decisions.md` and `docs/limitations.md` initialized and current.
`docs/architecture.md`, `technical_paper.md`, `project_report.md`,
`presentation.md`, `demo_guide.md` not yet started (later phases).

## Patent documentation status

Not started (Phase 27). `docs/patent_notes.md` and
`docs/invention_disclosure.md` do not exist yet.
