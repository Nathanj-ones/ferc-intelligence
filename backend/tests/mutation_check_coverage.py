#!/usr/bin/env python3
"""Reintroduce each defect and prove the tests catch it.

A test that keeps passing under its own mutation is not a test.
"""
import pathlib, sys, unittest, io, contextlib
HERE = pathlib.Path(__file__).resolve().parent
# Adopted into tests/ from work/w4-coverage/ (W4-R8), which is one level
# shallower, so the root is the parent rather than the grandparent. Only the
# path bootstrap changed; not one assertion was touched.
ROOT = HERE.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE))
from ferclib import coverage as cov
import build_field_status as bfs
import test_coverage as T   # renamed on adoption into tests/ (W4-R8)

MUTATIONS = {}

def mut(name):
    def deco(fn): MUTATIONS[name] = fn; return fn
    return deco

@mut("A06a shipped ranking: filed before present")
def _m1():
    from ferclib.status import Validation
    orig = cov._rank
    cov._rank = lambda o: (o.get("method") != "filed", o.get("validation") != Validation.PASS)
    return lambda: setattr(cov, "_rank", orig)

@mut("A06b no admissibility gates at all")
def _m2():
    orig = cov.admissible
    cov.admissible = lambda slot, o, **kw: (True, "")
    return lambda: setattr(cov, "admissible", orig)

@mut("A06c unit rule ignored (every family admissible)")
def _m3():
    orig = cov._slot_unit_contract
    cov._slot_unit_contract = lambda slot: (frozenset(), True)
    return lambda: setattr(cov, "_slot_unit_contract", orig)

@mut("A05 denominator rebuilt only from healthy sources")
def _m4():
    from ferclib.applicability import SourceHealth
    orig = cov.calendar_slots
    def broken(obligations, metrics, **kw):
        healthy = [o for o in obligations if o.source_health == SourceHealth.OK]
        return orig(healthy, metrics, **kw)
    cov.calendar_slots = broken
    return lambda: setattr(cov, "calendar_slots", orig)

@mut("A05b reconciliation drops shipped-only slots")
def _m5():
    orig = cov.reconcile_expected
    cov.reconcile_expected = lambda cal, ship: (list(cal), [])
    return lambda: setattr(cov, "reconcile_expected", orig)

@mut("A07 readiness from cross-template counts")
def _m6():
    orig = bfs.classify
    def broken(m, template, counts, n_entities, req, blockers):
        # the shipped behaviour: any present/pass ANYWHERE marks the field ready
        return ("implemented_retrieved_validated", "implemented",
                "cross-template count", "")
    bfs.classify = broken
    return lambda: setattr(bfs, "classify", orig)

def run():
    loader = unittest.TestLoader()
    rows = []
    for name, apply_mut in MUTATIONS.items():
        undo = apply_mut()
        suite = loader.loadTestsFromModule(T)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            res = unittest.TextTestRunner(stream=buf, verbosity=0).run(suite)
        undo()
        caught = sorted({t.id().rsplit(".", 1)[-1]
                         for t, _ in list(res.failures) + list(res.errors)})
        rows.append((name, len(caught), caught))
    print(f"{'mutation':52s} tests failing")
    ok = True
    for name, n, caught in rows:
        print(f"  {name:50s} {n}")
        for c in caught[:6]:
            print(f"      - {c}")
        if len(caught) > 6:
            print(f"      ... and {len(caught)-6} more")
        if n == 0:
            ok = False
            print("      !! NOTHING CAUGHT THIS MUTATION -- the test is not a test")
    print("\nALL MUTATIONS CAUGHT" if ok else "\nUNCAUGHT MUTATION PRESENT")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(run())
