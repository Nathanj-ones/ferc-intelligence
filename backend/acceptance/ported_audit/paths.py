"""
Path porting layer for the auditor's own harness.

The scripts in `audit_2026-09-08/.../evidence/scripts/` are written against the
auditor's sandbox:

    R = pathlib.Path('/mnt/data/ferc_audit')
    P = R/'evidence_full/operating_assets_all_regimes_FULL'   # the audited tree
    W = R/'work/integrated'                                   # the code under test

None of those paths exist here. This module maps each one onto this machine so
the auditor's checks can be run again as an INDEPENDENT verification, without
editing the substance of a single assertion and without touching the original
audit evidence, which stays read-only where it was delivered.

Mapping:

    R  ->  outputs/repair_all_regimes/work/w6-acceptance/ported_audit/
    P  ->  FERC_ACCEPTANCE_HISTORICAL_TREE                 (explicit, read-only)
    W  ->  outputs/repair_all_regimes/                    (the code under test)

The one substantive difference is deliberate and is the point of re-running them:
`W` is the REPAIRED code rather than the delivered code, so a control that the
auditor recorded as PASS-when-it-should-REJECT is expected to change verdict. Any
control whose verdict does NOT change is a fix that did not land.
"""

from __future__ import annotations

import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
TREE = HERE.parent.parent                       # candidate release root

#: the auditor's `W`: the code under test. Repaired, not delivered.
CODE = TREE
#: the auditor's `P`: an explicitly supplied historical package, read-only.  No
#: sibling path is inferred: in a standalone release the old expression could
#: alias W back to itself and turn historical-negative controls into repaired-
#: state controls without saying so.
_AUDITED_RAW = os.environ.get("FERC_ACCEPTANCE_HISTORICAL_TREE", "")
AUDITED = pathlib.Path(_AUDITED_RAW) if _AUDITED_RAW else None
_AUDITED_DB_RAW = os.environ.get("FERC_ACCEPTANCE_HISTORICAL_DB", "")
AUDITED_DB = (pathlib.Path(_AUDITED_DB_RAW) if _AUDITED_DB_RAW else
              (AUDITED / "staging" / "operating_assets.sqlite" if AUDITED else None))
#: the auditor's `R`: scratch. Ours, disposable, never their evidence.
ROOT = TREE / "work" / "w6-acceptance" / "ported_audit"
REPORTS = ROOT / "reports"
WORK = ROOT / "work"

#: The ORIGINAL audit evidence, opened read-only for comparison. Never searched
#: for outside the release; callers must name it explicitly when they need the
#: full original report rather than a compact packaged fixture.
_AUDIT_EVIDENCE_RAW = os.environ.get("FERC_ORIGINAL_AUDIT_EVIDENCE", "")
AUDIT_EVIDENCE = (pathlib.Path(_AUDIT_EVIDENCE_RAW)
                  if _AUDIT_EVIDENCE_RAW else None)


def prepare() -> None:
    for d in (ROOT, REPORTS, WORK):
        d.mkdir(parents=True, exist_ok=True)


def audited_db_uri() -> str:
    """Read-only AND immutable, exactly as the auditor opened it."""
    if AUDITED_DB is None:
        raise RuntimeError(
            "historical database not configured: set "
            "FERC_ACCEPTANCE_HISTORICAL_DB (or FERC_ACCEPTANCE_HISTORICAL_TREE); "
            "the port refuses to infer a sibling baseline")
    if not AUDITED_DB.is_file() or AUDITED_DB.is_symlink():
        raise RuntimeError(
            f"historical database is absent or aliased: {AUDITED_DB}")
    return f"file:{AUDITED_DB}?mode=ro&immutable=1"


def original_report(name: str) -> dict:
    """One of the auditor's own recorded results, for verdict comparison."""
    import json
    if AUDIT_EVIDENCE is None:
        raise RuntimeError(
            "original audit evidence not configured: set "
            "FERC_ORIGINAL_AUDIT_EVIDENCE; no personal-folder fallback is allowed")
    if AUDIT_EVIDENCE.is_symlink():
        raise RuntimeError(f"original audit evidence directory is aliased: {AUDIT_EVIDENCE}")
    path = AUDIT_EVIDENCE / "reports" / name
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"required original audit report is absent or aliased: {path}")
    return json.loads(path.read_bytes())
