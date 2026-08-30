# PROJECT_STATE

Authoritative status. Updated every phase boundary, same commit as the code.
Never re-ask anything recorded here.

## Environment (asked once, 2026-08-30 — do not re-ask)

- **OS:** macOS 14.5 (Darwin 23.5.0), Apple Silicon-class Homebrew at
  `/opt/homebrew`. Not Linux — cannot run Mininet/OVS natively.
- **Sudo:** yes, user `the development account` is in the `admin` group (sudo-capable).
- **RAM / cores:** 8 GB RAM / 8 cores.
- **Consequence:** Phases requiring Mininet/OVS (2 onward for the data
  plane) run inside a Linux VM (Ubuntu 22.04 LTS recommended, 4GB RAM / 4
  vCPU allocation — Multipass or UTM). See `docs/decisions.md`.
- **Python already available on the dev machine:** `python3.11` at
  `/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11`
  (3.11.9) — used directly for `venv-ml`. `python3.9` for `venv-ctrl` is not
  yet installed; will be provisioned at the Phase 2 GO/NO-GO smoke test
  (on the Linux VM, or via deadsnakes-equivalent on macOS if the
  controller-runs-on-host approach from `docs/decisions.md` is used).

## Current phase

**Phase 1 — COMPLETE.** Repo skeleton, config loader, logging, DB schema.

## Completed components

- Repository root initialized at `the project directory`
  (git repo, no nested `ai-self-healing-sdn/` subdirectory — see
  decisions.md).
- `config/config.yaml` — full tunable set (telemetry, features, label,
  ml, persistence, stability, path, rl, flow, verify, remediation, traffic,
  database, api, seeds). `project.controller_backend` intentionally `null`
  until Phase 2's smoke test determines ryu vs os-ken.
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
- `venv-ml` created on macOS with Python 3.11.9 (system-available, no
  install needed); `pyyaml`, `pytest`, `pytest-cov` installed.
- Tests: `tests/unit/test_config.py` (15 tests), `tests/unit/test_db.py`
  (9 tests), `tests/unit/test_logging.py` (3 tests) — **27/27 passing**.
- `pytest.ini`, `Makefile` (setup-ml, setup-ctrl, test, test-ctrl, clean),
  `requirements-ctrl.txt`, `requirements-ml.txt`, `.gitignore`, `README.md`,
  `LICENSE` (MIT), `docs/decisions.md`, `docs/limitations.md`.

## Pending components

Everything from Phase 2 onward per the phase plan (env setup + GO/NO-GO
smoke test is next).

## Known issues

- `venv-ctrl` (Python 3.9 + Ryu) not yet created — blocked on the Linux VM
  being available, or on evaluating the controller-on-host approach logged
  in `docs/decisions.md`. This is the next concrete step in Phase 2.

## Configuration

See `config/config.yaml` for the full set. Notable defaults: ML threshold
0.75, uncertainty band [0.45, 0.60], persistence 3-of-4 windows, cooldown
30s, route hold 60s, min improvement 15%, telemetry interval 2s, label
horizon 10s, verify settle 8s.

## Test status

27/27 unit tests passing (`tests/unit/test_config.py`,
`tests/unit/test_db.py`, `tests/unit/test_logging.py`) under `venv-ml`
(Python 3.11.9) on macOS. Coverage not yet measured formally (will be
reported honestly in the README per the testing rules once more modules
exist to cover).

## Experiment status

Not started (Phase 22).

## Documentation status

`docs/decisions.md` and `docs/limitations.md` initialized and current.
`docs/architecture.md`, `technical_paper.md`, `project_report.md`,
`presentation.md`, `demo_guide.md` not yet started (later phases).

## Patent documentation status

Not started (Phase 27). `docs/patent_notes.md` and
`docs/invention_disclosure.md` do not exist yet.
