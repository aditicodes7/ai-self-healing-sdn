# Architecture

How the system is put together, and why each piece is where it is. Design
rationale for individual decisions lives in `docs/decisions.md`; what the
system cannot do lives in `docs/limitations.md`. This document is the map
between them.

---

## 1. The loop, in one paragraph

Switches report port counters to the controller. The controller derives
per-link metrics from consecutive counter samples, turns those into a
window of features, and asks a model whether each link is about to
degrade. A prediction that persists across several windows opens an
**incident**. The incident triggers path selection — either a learned
policy or a weighted-cost formula — which installs new flow rules. The
system then **verifies** that the reroute actually helped, and closes the
incident as SUCCESS, PARTIAL or FAILED. Everything at every stage is
written to SQLite, which is what makes the experiments and the dashboard
possible.

```
 switches ──counters──▶ telemetry_collector ──features──▶ ml_rl_service (predict)
                              │                                   │
                              │                            probability
                              ▼                                   ▼
                        link metrics ──────────────────▶  persistence_tracker
                              │                                   │
                              │                          incident opened
                              │                                   ▼
                              └────────────────────────▶  remediation_manager
                                                            │         │
                                              rl_client ◀───┘         └──▶ path_computer
                                                            │                 (formula)
                                                            ▼
                                                      flow_manager ──FlowMod──▶ switches
                                                            │
                                                            ▼
                                                        verifier ──▶ incident closed
```

A second, independent path exists for hard failures: LLDP link-removal
events go straight from `topology_manager` to `healing_loop.on_link_down`,
bypassing prediction entirely. See §6.

---

## 2. Where the code runs

Three execution contexts, and the split is forced by dependencies:

| context | Python | holds | why separate |
|---|---|---|---|
| **VM, `venv-ctrl`** | 3.9 | Ryu controller, `requests` | Ryu pins old dependencies and needs Linux network namespaces |
| **VM, `venv-ml`** | 3.11 | inference service, experiment runner | scikit-learn and torch will not install under 3.9 |
| **macOS, `venv-ml`** | 3.11 | training, analysis, figures, dashboard backend | no Mininet needed; iterate without the VM |

The controller and the model therefore cannot share a process. They
communicate over HTTP to `service/ml_rl_service.py`, which is the only
component that loads either model.

`controller/of_compat.py` exists so that controller modules import
cleanly under `venv-ml` (where `ryu` is absent) for unit testing, using
a `TYPE_CHECKING` guard rather than stubbing Ryu.

---

## 3. Module responsibilities

**`common/`** — shared vocabulary. `schema.py` holds the enums every
layer agrees on (`LinkHealthState`, `RouteSelector`, `DataSource`).
`features.py` derives metrics from raw counters and builds feature
windows; it is imported by both the live controller and the offline
dataset generator, which is what keeps training and serving on the same
code path. `config_loader.py` parses `config/config.yaml` into frozen
dataclasses so a typo fails at startup, not mid-experiment.

**`controller/`** — the closed loop.

- `main_app.py` — the Ryu application; L2 forwarding and switch lifecycle.
- `telemetry_collector.py` — polls port stats, estimates per-link latency,
  derives metrics, batches inference calls, and wires the loop together.
- `topology_manager.py` — LLDP discovery, the live graph, the flood-safe
  spanning tree, and link-removal events.
- `persistence_tracker.py` — requires a prediction to repeat before it
  counts. This is the gate between "the model twitched" and "act".
- `healing_loop.py` — the assembled loop. Deliberately a plain class, not
  a RyuApp, so it can be unit-tested with injected `sleep_fn`, `spawn_fn`,
  `graph_provider`, `datapaths_provider` and `link_metrics_provider`.
- `remediation_manager.py` — chooses a path (RL or formula), installs it,
  and records the decision *and its inputs*.
- `path_computer.py` — k-shortest candidates and the weighted-cost
  ranking used by the formula.
- `rl_client.py` / `inference_client.py` — HTTP to the service, with the
  safety property that an unreachable service returns `None` rather than
  raising or guessing.
- `flow_manager.py` — FlowMod construction and installation.
- `verifier.py` — re-measures after a reroute and decides SUCCESS /
  PARTIAL / FAILED.
- `stability_manager.py` — cooldown, hold-down and minimum-improvement
  gates, to stop the loop from flapping.
- `baselines/` — the comparison systems (B1 static, B2 reactive).

**`ml/`** — dataset generation, training, threshold sweep, evaluation.
Offline only.

**`rl/`** — the routing policy: a pure-numpy `env/simulator.py`, the
Gymnasium environment, state and action encodings, training and
evaluation. Also offline; the trained policy is loaded by the service.

**`service/ml_rl_service.py`** — FastAPI. Loads both models, exposes
`/infer` (degradation probability) and `/infer/route` (path choice).
Holds the action mask.

**`network/`** — the emulated network: `topo_builder.py` builds Mininet
topologies from YAML, `failure_injector.py` applies real `tc netem`
faults with a pre-fault ramp, `traffic_generator.py` drives load.

**`experiments/`** — `definitions.py` (what each experiment claims),
`runner.py` (executes a trial end to end), `analyze.py` (aggregates,
honestly), `make_figures.py`.

**`database/`**, **`backend/`**, **`dashboard/`** — storage, the REST/SSE
API, and the single-page UI.

---

## 4. Data model

Eleven tables, one per stage, so every claim is traceable to a row.

| table | one row per | answers |
|---|---|---|
| `telemetry` | port stats sample | what the switches reported |
| `features` | derived window, and latency probe | what the model was given |
| `predictions` | scored link | what the model said |
| `links` | link | current health state |
| `topology_snapshots` | topology change | what the graph looked like |
| `incidents` | incident | what the system decided to fix |
| `route_changes` | routing decision | what was chosen, **and what it was shown** |
| `flow_rules` | FlowMod | what was installed |
| `remediation_events` | step | the audit trail |
| `verification_results` | verification | whether it actually helped |
| `experiment_runs` | trial | the measured outcome |

`route_changes.decision_inputs_json` was added late and for a specific
reason: without it, a decision that looks wrong cannot be distinguished
from a decision made on different information. See `docs/limitations.md`.

SQLite in WAL mode, stdlib `sqlite3`, no ORM. Schema is idempotent
(`CREATE TABLE IF NOT EXISTS`) with a small explicit column migration in
`database/db.py` for columns added after databases already existed.

---

## 5. The four systems under test

They exist so that a difference between two of them is attributable to
exactly one mechanism. Each row adds one thing to the row above it.

| | detection | path selection |
|---|---|---|
| **B1** static shortest path | none | none — never reroutes |
| **B2** reactive threshold | threshold breach | weighted-cost formula |
| **P1** ML + heuristic | ML prediction + persistence | weighted-cost formula |
| **P2** ML + RL | ML prediction + persistence | learned policy |

Selected by `experiment.system` in config, so the same binary runs as any
of them. B1 is deliberately inert: it is the control that shows what the
fault does when nothing responds.

---

## 6. Two detection paths, not one

Gradual degradation is detected by the model, with persistence required
before acting. A hard link failure is **not** — measured: with a link
cut, the model scored it 0.034, lower than before the cut, because a dead
link carries no traffic and returns no probes, and the labels only cover
degradation that has yet to happen.

So `topology_manager` emits link-removal events and `healing_loop`
remediates on them directly, bypassing persistence: that gate filters
uncertain *predictions*, and a link's disappearance is an observed fact.
This capability is given to B2/P1/P2 but withheld from B1, since
reacting to a link-down event is a control-plane capability rather than a
learned one.

Reported separately rather than averaged into one "detection rate",
because the two mechanisms work in completely different ways.

---

## 7. Safety properties

The system is built so that failure degrades rather than fabricates.

- An unreachable inference service means **no prediction**, not a
  guessed one. `inference_client` returns `None`; the loop does nothing.
- An unreachable or nonsensical RL answer falls back to the formula, and
  records `rl_fallback_reason` for *every* call, not just noticed ones.
- A reroute is **verified**; a failed verification reopens the incident
  and excludes the failed path rather than declaring success.
- Persistence, cooldown, hold-down and minimum-improvement gates all sit
  between a prediction and a FlowMod.
- Anything unmeasurable is stored as `NULL`, never as `0`.

E8 tests the first two by killing the service mid-run.

---

## 8. What the numbers rest on

Every reported figure traces to a JSON artefact under `results/` produced
by an executed run. Superseded runs are never deleted — they stay in
`results/experiments.db` and are listed, with the reason, in
`results/invalidated_runs.json`, which `analyze.py` applies by default.

Data is labelled `emulated_mininet` or `synthetic_simulator` at the point
of capture. Neither is real-world traffic and the distinction is carried
through to the tables and figures.
