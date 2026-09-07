"""The Phase 22 experiment set.

Each entry says what claim it supports, so nothing gets run (and no
figure gets drawn) without a stated reason. Every experiment runs
>= seeds_required distinct seeds per system, and every number that
reaches a table or figure comes from an executed trial recorded in the
`experiment_runs` table -- never from an estimate.

SYSTEMS UNDER TEST
------------------
  B1_SHORTEST_PATH   static shortest path, installed once, never changed.
                     The do-nothing control: shows what the fault costs
                     when nobody reacts.
  B2_RULE_BASED      reactive thresholding on CURRENT utilization/loss
                     (controller/baselines/rule_based.py). No prediction,
                     no persistence, no learned policy. Isolates "did
                     prediction help, or would simple thresholds do?"
  P1_ML_HEURISTIC    ML prediction + persistence/stability + weighted-cost
                     path choice. Isolates the ML half.
  P2_ML_RL           ML prediction + persistence/stability + the trained
                     RL policy choosing the path. The full system.

The B1 -> B2 -> P1 -> P2 ladder is deliberate: each step adds exactly one
mechanism, so a difference between adjacent rungs is attributable.
"""
from __future__ import annotations

import dataclasses

ALL_SYSTEMS = ("B1_SHORTEST_PATH", "B2_RULE_BASED", "P1_ML_HEURISTIC", "P2_ML_RL")
LEARNED_SYSTEMS = ("P1_ML_HEURISTIC", "P2_ML_RL")

# The six fault types network/failure_injector.py can inject.
ALL_FAULTS = (
    "congestion",
    "gradual_degradation",
    "packet_loss",
    "high_latency",
    "rising_errors",
    "link_failure",
)


@dataclasses.dataclass(frozen=True)
class Experiment:
    experiment_id: str
    title: str
    claim: str
    systems: tuple[str, ...]
    faults: tuple[str, ...]
    seeds_required: int
    notes: str = ""

    @property
    def trial_count(self) -> int:
        return len(self.systems) * len(self.faults) * self.seeds_required


EXPERIMENTS: tuple[Experiment, ...] = (
    Experiment(
        experiment_id="E1",
        title="End-to-end comparison on the primary fault",
        claim=(
            "The full system recovers a degrading link faster and with less "
            "throughput lost than static routing or reactive thresholding."
        ),
        systems=ALL_SYSTEMS,
        faults=("gradual_degradation",),
        seeds_required=5,
        notes="The headline table. One fault type, all four systems.",
    ),
    Experiment(
        experiment_id="E2",
        title="Coverage across all six fault types",
        claim=(
            "The approach generalises beyond one fault shape -- and, equally "
            "important, shows WHICH faults it handles poorly."
        ),
        systems=("B1_SHORTEST_PATH", "P2_ML_RL"),
        faults=ALL_FAULTS,
        seeds_required=5,
        notes=(
            "Expect weak results on packet_loss and rising_errors: OVS "
            "counters cannot observe tc-netem loss on veth links, so those "
            "features are identically zero (docs/limitations.md). Reporting "
            "that honestly is the point of this experiment."
        ),
    ),
    Experiment(
        experiment_id="E3",
        title="Prediction lead time",
        claim=(
            "Detection happens BEFORE the link has fully degraded -- the "
            "difference between predictive and reactive operation."
        ),
        systems=("B2_RULE_BASED", "P2_ML_RL"),
        faults=("gradual_degradation", "congestion"),
        seeds_required=5,
        notes=(
            "Measures fault-onset -> detection. B2 can only fire once "
            "thresholds are already breached, so a positive lead-time gap "
            "is the core predictive claim. A non-positive gap would "
            "falsify it and must be reported as such."
        ),
    ),
    Experiment(
        experiment_id="E4",
        title="Route stability and oscillation",
        claim=(
            "Persistence + hysteresis keep reroute counts low; the system "
            "does not flap."
        ),
        systems=("B2_RULE_BASED", "P1_ML_HEURISTIC", "P2_ML_RL"),
        faults=("gradual_degradation", "congestion"),
        seeds_required=5,
        notes="Primary metric is reroutes per run, not recovery time.",
    ),
    Experiment(
        experiment_id="E5",
        title="Ablation: persistence and stability gates removed",
        claim=(
            "The persistence/hysteresis layer is load-bearing -- without it "
            "the same predictions cause visibly worse behaviour."
        ),
        systems=("P2_ML_RL",),
        faults=("gradual_degradation",),
        seeds_required=5,
        notes=(
            "Run twice: gates enabled vs persistence.consecutive=1 and "
            "stability cooldown/hold/min-improvement zeroed. This is what "
            "justifies claiming those elements as part of the contribution."
        ),
    ),
    Experiment(
        experiment_id="E6",
        title="Ablation: RL policy vs weighted-cost heuristic",
        claim=(
            "The learned policy chooses better paths than the hand-tuned "
            "cost function -- or, if it does not, that is reported."
        ),
        systems=LEARNED_SYSTEMS,
        faults=("gradual_degradation", "congestion", "high_latency"),
        seeds_required=5,
        notes=(
            "P1 vs P2 differ ONLY in path selection, so any difference is "
            "attributable to the RL policy. This is the experiment that "
            "justifies the RL half of the project."
        ),
    ),
    Experiment(
        experiment_id="E7",
        title="Sensitivity to background load",
        claim=(
            "Behaviour is characterised across offered load, not just at one "
            "convenient operating point."
        ),
        systems=("B1_SHORTEST_PATH", "P2_ML_RL"),
        faults=("congestion",),
        seeds_required=5,
        notes="Sweeps background traffic; the load levels are a runner parameter.",
    ),
    Experiment(
        experiment_id="E8",
        title="Safety gates under real service failure",
        claim=(
            "When the ML/RL service is unavailable the system degrades "
            "safely to the heuristic and never fabricates a decision."
        ),
        systems=("P2_ML_RL",),
        faults=("gradual_degradation",),
        seeds_required=5,
        notes=(
            "The inference service is killed mid-run. Success is measured "
            "as: zero fabricated predictions, rl_fallback=1 recorded with a "
            "reason, and the network still forwarding. Phase 10 verified "
            "this for the ML client in isolation; this verifies it for the "
            "assembled loop."
        ),
    ),
)


def by_id(experiment_id: str) -> Experiment:
    for exp in EXPERIMENTS:
        if exp.experiment_id == experiment_id:
            return exp
    raise KeyError(f"unknown experiment {experiment_id!r}")


def total_trials() -> int:
    return sum(e.trial_count for e in EXPERIMENTS)


if __name__ == "__main__":
    print(f"{'id':<5}{'systems':>9}{'faults':>8}{'seeds':>7}{'trials':>8}  title")
    for e in EXPERIMENTS:
        print(f"{e.experiment_id:<5}{len(e.systems):>9}{len(e.faults):>8}"
              f"{e.seeds_required:>7}{e.trial_count:>8}  {e.title}")
    print(f"\ntotal trials: {total_trials()}")
