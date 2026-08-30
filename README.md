# AI-Driven Self-Healing Software-Defined Network

A closed-loop self-healing SDN: an ML model predicts link
degradation ahead of time, a trained RL policy selects a remediation path,
the controller installs OpenFlow rules automatically, recovery is measured
and verified, and the whole loop is shown live on a dashboard.

**Current status:** see [PROJECT_STATE.md](PROJECT_STATE.md) for the
authoritative phase-by-phase status, environment, decisions, and test
results. This README gives orientation; PROJECT_STATE.md is never stale by
more than one commit.

## Data honesty

Every number in this repository's docs, figures, and papers traces to a
JSON/CSV artefact under `results/` produced by an executed run. If a
measurement has not been taken yet, the docs say `NOT YET MEASURED` — never
a placeholder value. Every dataset is labelled `data_source:
emulated_mininet` or `data_source: synthetic_simulator`; neither is ever
described as real-world traffic. See [docs/limitations.md](docs/limitations.md)
for every place emulation stands in for something that couldn't be measured
directly, and why.

## Repository layout

Two Python interpreters are used deliberately and never import each other:

- `venv-ctrl` (Python 3.9): the Ryu/os-ken controller — needs a real Linux
  kernel host (Mininet + Open vSwitch) to drive the data plane.
- `venv-ml` (Python 3.11): dataset generation, ML training, RL training,
  the inference service, the FastAPI backend, and the test suite.

They communicate over localhost HTTP and a shared SQLite file
(`database/network.db`, WAL mode). `common/` is pure Python + numpy so both
venvs import it unchanged.

See the top of [PROJECT_STATE.md](PROJECT_STATE.md) for the full directory
tree as it exists today (it grows one phase at a time; this README does not
duplicate a tree that will immediately go stale).

## Setup

```
make setup-ml      # Python 3.11 venv: ML/RL/backend/tests
make setup-ctrl    # Python 3.9 venv: Ryu controller (Linux host only)
make test          # runs tests/unit + tests/integration under venv-ml
```

Mininet/OVS require a real Linux kernel and root. If your development
machine is not Linux, run the data-plane phases inside a Linux VM (Ubuntu
22.04 LTS, 4GB RAM / 4 vCPU is enough) — see PROJECT_STATE.md's Environment
section for the specific setup used on this project.

## Documentation

- [docs/architecture.md](docs/architecture.md) — system design, every
  telemetry/feature formula
- [docs/decisions.md](docs/decisions.md) — architectural decision log
- [docs/limitations.md](docs/limitations.md) — honest limitations and why
- [docs/technical_paper.md](docs/technical_paper.md) — paper draft (measured
  numbers only)
- [docs/project_report.md](docs/project_report.md) — university evaluation
  report
- [docs/demo_guide.md](docs/demo_guide.md) — exact commands for a live demo
- [docs/patent_notes.md](docs/patent_notes.md) — technical documentation for
  provisional patent review (not a legal patentability opinion)
- [docs/invention_disclosure.md](docs/invention_disclosure.md) — dated
  factual record of conception/reduction to practice

## License

MIT for the software (see [LICENSE](LICENSE)); patent rights, if pursued,
are tracked separately in `docs/patent_notes.md`.
