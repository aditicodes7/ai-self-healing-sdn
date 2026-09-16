#!/usr/bin/env python3
"""Phase 28: check that this project's claims survive re-derivation.

Four checks, in increasing order of how much they would embarrass us:

  1. The unit tests pass.
  2. The analysis re-derives from experiments.db to the same numbers that
     are currently committed in results/tables/.
  3. Every figure regenerates.
  4. Every headline number quoted in docs/ and README.md matches the
     artefact it is supposed to come from.

Check 4 is the point of this script. Six documents quote figures that were
transcribed by hand from JSON, and a transcription error is invisible --
it reads as a perfectly plausible number. Nothing else in the pipeline
would ever catch one.

Exit status is non-zero if any check fails, so this is usable in CI.

    python3 scripts/verify_reproducibility.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLES = ROOT / "results" / "tables"
VENV = ROOT / "venv-ml" / "bin" / "python"

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    mark = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn "}[status]
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def run(cmd: list[str], cwd: Path = ROOT) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# --------------------------------------------------------------------------
# 1. Tests
# --------------------------------------------------------------------------
def check_tests() -> None:
    code, out = run([str(VENV), "-m", "pytest", "tests/unit", "-q"])
    m = re.search(r"(\d+) passed", out)
    n = m.group(1) if m else "?"
    record("unit tests", PASS if code == 0 else FAIL, f"{n} passed")


# --------------------------------------------------------------------------
# 2. The analysis re-derives to the committed numbers
# --------------------------------------------------------------------------
def check_analysis_is_reproducible() -> None:
    committed = TABLES / "experiment_results.json"
    if not committed.exists():
        record("analysis re-derives", FAIL, "no experiment_results.json")
        return
    before = json.loads(committed.read_text())

    backup = Path(tempfile.mkdtemp()) / "experiment_results.json"
    shutil.copy(committed, backup)
    try:
        code, out = run([str(VENV), "experiments/analyze.py"])
        if code != 0:
            record("analysis re-derives", FAIL, "analyze.py exited non-zero")
            return
        after = json.loads(committed.read_text())
    finally:
        pass  # the regenerated file is the one we want to keep

    # generated_at always differs; everything else must not.
    drift = []
    for key in ("results", "by_fault"):
        a, b = before.get(key, {}), after.get(key, {})
        if set(a) != set(b):
            drift.append(f"{key}: arm set changed")
            continue
        for arm in a:
            if json.dumps(a[arm], sort_keys=True) != json.dumps(b[arm], sort_keys=True):
                drift.append(f"{key}/{arm}")
    if drift:
        record("analysis re-derives", FAIL, f"{len(drift)} differ: {drift[:3]}")
    else:
        shutil.copy(backup, committed)  # restore the original generated_at
        record("analysis re-derives", PASS,
               f"{len(before.get('results', {}))} arms identical")


# --------------------------------------------------------------------------
# 3. Figures regenerate
# --------------------------------------------------------------------------
def check_figures() -> None:
    figs = ROOT / "results" / "figures"
    before = {p.name for p in figs.glob("*.png")}
    code, out = run([str(VENV), "experiments/make_figures.py"])
    after = {p.name for p in figs.glob("*.png")}
    if code != 0:
        record("figures regenerate", FAIL, "make_figures.py exited non-zero")
    elif missing := (before - after):
        record("figures regenerate", FAIL, f"disappeared: {sorted(missing)}")
    else:
        record("figures regenerate", PASS, f"{len(after)} figures")


# --------------------------------------------------------------------------
# 4. Documented numbers match their artefacts
# --------------------------------------------------------------------------
def _pct(entry) -> int | None:
    return None if entry is None else round(entry * 100)


def claims() -> list[tuple[str, object, str]]:
    """(description, expected value as it should be written, context regex
    with ONE capture group).

    The regex finds every place the docs state this quantity, and EVERY
    match must equal the expected value. An earlier version of this check
    merely asked whether the correct value appeared somewhere in the
    corpus -- which passes even when a document states the wrong number,
    as long as some other document states the right one. A negative test
    confirmed it: changing E5's ablated reroute count from 4.2 to 4.9
    across the docs did not fail the check, because one line the edit
    missed still carried 4.2. A check that cannot fail is not a check.
    """
    res = json.loads((TABLES / "experiment_results.json").read_text())
    arms, by_fault = res["results"], res["by_fault"]
    out: list[tuple[str, object, str]] = []

    e2 = {k.split("/")[2]: v for k, v in by_fault.items() if k.startswith("E2/P2_ML_RL/")}

    lf = (e2.get("link_failure", {}).get("recovery_time_s") or {}).get("mean")
    if lf:
        out.append(("E2 link-failure recovery", f"{lf:.1f}",
                    r"repairs? a hard link failure in \*\*(\d+\.\d)\s*seconds|"
                    r"\*\*(\d+\.\d) s\*\* to repair"))

    b1 = arms.get("E2/B1_SHORTEST_PATH", {})
    if b1:
        out.append(("E2 control trial count", str(b1["trials_usable"]),
                    r"0 of (\d+)(?: injected)? faults?|detected \*\*0 of (\d+)\*\*"))

    on = (arms.get("E5/P2_ML_RL", {}).get("reroute_count") or {}).get("mean")
    off = (arms.get("E5/P2_ML_RL_nogates", {}).get("reroute_count") or {}).get("mean")
    if on is not None and off is not None:
        # Every "X to/vs/and Y reroutes" pair in the docs must be this pair.
        out.append(("E5 ablated reroutes", f"{off:.1f}",
                    rf"{on:.1f}\s*(?:to|vs|and)\s*\*?\*?(\d+\.\d)"))

    b2 = (arms.get("E3/B2_RULE_BASED", {}).get("detection_latency_in_fault_s") or {}).get("mean")
    p2 = (arms.get("E3/P2_ML_RL", {}).get("detection_latency_in_fault_s") or {}).get("mean")
    if b2 and p2:
        # Stated in both orders across the documents ("59.7 s vs 72.9" and
        # "72.9 s vs 59.7"), so a single capture cannot say which side is
        # which. Both numbers are captured and the PAIR must match.
        # Anchored on "faster"/"averaged", because the documents state this
        # pair in both orders and an unanchored "X vs Y" matches every other
        # comparison in the corpus -- recovery times, throughput, all of it.
        out.append(("E3 detection latencies", {f"{b2:.1f}", f"{p2:.1f}"},
                    r"(?:faster|averaged)[^.]{0,40}?(\d+\.\d)\s*s"
                    r"(?:econds)?[^.]{0,30}?(\d+\.\d)"))

    dq = TABLES / "e6_decision_quality.json"
    if dq.exists():
        a = json.loads(dq.read_text())["arms"]
        out.append(("E6 wider-path count", str(a["P2_ML_RL"]["chose_wider"]),
                    r"wider\*?\*? path \*\*(\d+)\*?\*?(?:\u00d7| times)"))
        out.append(("E6 formula self-agreement", str(a["P1_ML_HEURISTIC"]["agreed_with_formula"]),
                    r"agreed with itself \*\*(\d+)"))

    total = sum(v["trials_usable"] for v in arms.values())
    out.append(("total valid trials", str(total),
                r"\*\*(\d+) trials in eight controlled|(\d+) valid trials|"
                r"(\d+) trials \u00b7 8 experiments"))
    return out


def check_documented_numbers() -> None:
    docs = sorted((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md"]
    corpus = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                       for p in docs if p.exists())

    bad, checked = [], 0
    for desc, expected, pattern in claims():
        found = [g for m in re.finditer(pattern, corpus) for g in m.groups() if g]
        if not found:
            bad.append(f"{desc}: no statement of it found in docs")
            continue
        checked += len(found)
        allowed = expected if isinstance(expected, set) else {expected}
        wrong = sorted({f for f in found if f not in allowed})
        if wrong:
            bad.append(f"{desc}: docs say {wrong}, artefact says "
                       f"{sorted(allowed) if isinstance(expected, set) else expected}")

    if bad:
        record("documented numbers match artefacts", FAIL, "; ".join(bad[:3]))
    else:
        record("documented numbers match artefacts", PASS,
               f"{checked} statements across {len(docs)} documents")


def main() -> int:
    print("Phase 28 -- reproducibility review\n" + "=" * 52)
    check_tests()
    check_analysis_is_reproducible()
    check_figures()
    check_documented_numbers()

    print("=" * 52)
    failed = [r for r in results if r[1] == FAIL]
    if failed:
        print(f"{len(failed)} of {len(results)} checks FAILED")
        return 1
    print(f"all {len(results)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
