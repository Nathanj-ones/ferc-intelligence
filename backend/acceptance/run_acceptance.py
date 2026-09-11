#!/usr/bin/env python3
"""
Run the adversarial acceptance suite.

    python3 acceptance/run_acceptance.py                 # everything
    python3 acceptance/run_acceptance.py --tier worker   # one tier
    python3 acceptance/run_acceptance.py --issue A12.3   # one case

Results are reported per TIER, because the tiers prove different things and
merging them produces exactly the kind of green total A12 was raised about:

    worker            runs against hash-pinned historical evidence, the current
                      read-only reference where required, and disposable
                      fixtures. Proves a mutation is detected.
    integrated        needs the canonical repaired database the integrator
                      builds. SKIPPED until it exists -- never counted as passed.
    external_optional needs an optional external reference package.

Exit status is 0 only when there are no FAIL and no ERROR results. SKIPPED and
XFAIL do not make the run green by accident: they are printed in full, listed in
the JSON report, and summarised separately at the end.
"""

from __future__ import annotations

import argparse
import importlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
TREE = HERE.parent
if str(TREE) not in sys.path:
    sys.path.insert(0, str(TREE))

from acceptance import harness                                          # noqa: E402
from acceptance.harness import REGISTRY, Env, Status, Tier              # noqa: E402

#: every module holding registered cases
CASE_MODULES = (
    "acceptance.test_a12_suite_integrity",
    "acceptance.test_a13_release_contract",
    "acceptance.test_a21_release_identity",
    "acceptance.test_matrix_runtime",
    "acceptance.test_matrix_identity",
    "acceptance.test_matrix_values",
    # `test_matrix_documents` was planned and then folded into
    # `test_matrix_identity` (matrix point 7, the M7.x cases). It stayed listed
    # here as a module that never existed, and because a failed import was only a
    # printed warning, every run of this suite has been quietly reporting it. The
    # integrity guard in `load_cases()` caught it the first time it ran.
    "acceptance.test_matrix_coverage",
    "acceptance.test_positive_controls",
)


def load_cases() -> tuple[list, list[str]]:
    """Import every case module. An import failure is FATAL, not a warning.

    This used to `print("!! could not import ...")` and carry on, which is the
    A12.1b defect reproduced in this very runner: a module whose import failed
    contributed zero cases, every remaining case passed, and the run exited 0
    having silently dropped six checks. During the 15:14 restore that was not
    hypothetical -- `acceptance/contract.py` was briefly unreadable, and any
    module importing it would have vanished from the suite with one line of log
    to say so.

    A module that imports but registers nothing is caught too: a decorator that
    stops running is indistinguishable, in the totals, from a file that stops
    existing.
    """
    problems: list[str] = []
    for mod in CASE_MODULES:
        before = len(REGISTRY)
        try:
            importlib.import_module(mod)
        except Exception as exc:                                        # noqa: BLE001
            problems.append(f"{mod}: import FAILED -- {type(exc).__name__}: {exc}")
            continue
        if len(REGISTRY) == before:
            problems.append(f"{mod}: imported but registered NO cases")
    return list(REGISTRY), problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=[Tier.WORKER, Tier.INTEGRATED, Tier.EXTERNAL])
    ap.add_argument("--issue")
    ap.add_argument("--out", default=str(TREE / "verification" / "acceptance_results.json"))
    args = ap.parse_args()

    cases, problems = load_cases()
    if problems:
        print("SUITE INTEGRITY FAILURE -- the suite is not complete, so no result "
              "from it can be trusted:")
        for problem in problems:
            print(f"  !! {problem}")
        print("\nRefusing to run. A partial suite that reports PASS is worse than no "
              "suite at all: it is the false-pass shape this workstream exists to close.")
        return 3
    env = Env()
    print(f"acceptance suite: {len(cases)} registered case(s)")
    print(f"scratch: {env.scratch}")
    print(f"current reference: {harness.BASELINE_DB}")
    print(f"repaired: {harness.REPAIR_DB} "
          f"({'present' if harness.REPAIR_DB.is_file() else 'NOT BUILT YET'})\n")

    summaries = {}
    all_results = []
    for tier in (Tier.WORKER, Tier.INTEGRATED, Tier.EXTERNAL):
        if args.tier and tier != args.tier:
            continue
        tier_cases = [c for c in cases if c.tier == tier]
        if args.issue:
            tier_cases = [c for c in tier_cases if c.issue == args.issue]
        if not tier_cases:
            continue
        print(f"=== tier: {tier} ({len(tier_cases)} case(s)) ===")
        s = harness.run(tier_cases, env, only_tier=tier, only_issue=args.issue)
        summaries[tier] = s
        all_results.extend(s["results"])
        print()

    env.close()

    combined = harness.summarise(all_results)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"combined": combined, "by_tier_report": summaries},
                              indent=1, default=str), encoding="utf-8")

    t = combined["totals"]
    ran = sum(t[k] for k in (Status.PASS, Status.FAIL, Status.ERROR,
                             Status.SKIPPED, Status.XFAIL, Status.XPASS))
    print("=" * 72)
    for tier, s in summaries.items():
        st = s["totals"]
        print(f"{tier:<18} {st[Status.PASS]} pass / {st[Status.FAIL]} fail / "
              f"{st[Status.ERROR]} error / {st[Status.SKIPPED]} skipped / "
              f"{st[Status.XFAIL]} xfail / {st[Status.XPASS]} xpass")
    print("-" * 72)
    print(f"{'TOTAL':<18} {t[Status.PASS]} pass / {t[Status.FAIL]} fail / "
          f"{t[Status.ERROR]} error / {t[Status.SKIPPED]} skipped / "
          f"{t[Status.XFAIL]} xfail / {t[Status.XPASS]} xpass")

    if combined["failures"]:
        print("\nFAILURES (every one listed, none hidden):")
        for r in combined["failures"]:
            print(f"  [{r['status']}] {r['issue']} {r['check']}  (owner: {r['owner']})")
            for line in str(r["detail"]).splitlines()[:8]:
                print(f"        {line}")
    if combined["expected_failing_targets"]:
        print("\nEXPECTED-FAILING ACCEPTANCE TARGETS (fix not landed; NOT weakened):")
        for r in combined["expected_failing_targets"]:
            print(f"  [XFAIL] {r['issue']} {r['check']}  (owner: {r['owner']})")
            print(f"        {r['detail']}")
            print(f"        actual: {str(r.get('failure', ''))[:200]}")
    if combined["targets_now_met"]:
        print("\nTARGETS NOW MET -- remove the xfail marker:")
        for r in combined["targets_now_met"]:
            print(f"  [XPASS] {r['issue']} {r['check']}  (owner: {r['owner']})")
    if combined["skipped"]:
        print("\nSKIPPED (never counted as a pass):")
        for r in combined["skipped"]:
            print(f"  [skip] {r['issue']} {r['check']}: {str(r['detail'])[:140]}")

    print(f"\n-> {out}")

    # A run that executed NOTHING is not a pass. Without this, an empty selection
    # -- a mistyped --issue, a tier with no cases, or a tree being rewritten
    # underneath the import -- scores 0 failures and 0 errors and exits 0.
    # `python3 -m unittest discover` exits 5 on zero tests for the same reason;
    # this is that guard, in this runner.
    if ran == 0:
        print("NO CASES RAN. This is NOT a pass -- the selection matched nothing, so "
              "the suite proved nothing. Check --tier / --issue.")
        return 2
    return 0 if (t[Status.FAIL] == 0 and t[Status.ERROR] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
