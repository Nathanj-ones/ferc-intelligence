"""Bind the production acceptance runner to ordinary ``unittest`` evidence.

The acceptance matrix deliberately has its own runner and machine-readable
result format.  The release test manifest, however, accepts only complete
verbose ``unittest`` logs.  This bridge does not reproduce any acceptance
answer: it invokes the shipped ``acceptance/run_acceptance.py`` exactly once,
parses that runner's JSON, and exposes one stable unittest method for each
registered case.

The runner's normal scratch directory is below the source tree.  For this test
it is redirected to a private temporary directory before the real runner is
entered.  Its result file, HOME and TMPDIR are private too.  The Build-A
database remains an explicit read-only input supplied through
``FERC_STAGING_DB``; there is intentionally no repository fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "acceptance" / "run_acceptance.py"
EXPECTED_TOTAL = 63
EXPECTED_BY_TIER = {"worker": 59, "integrated": 3, "external_optional": 1}
EXTERNAL_OPTIONAL = frozenset(
    {
        (
            "A12.4",
            "acceptance.test_a12_suite_integrity",
            "t_absent_external_reference_is_skipped",
        ),
    }
)


def _declared_cases():
    """Load the production registry without executing any acceptance case."""
    from acceptance import harness
    from acceptance import run_acceptance

    expected_modules = set(run_acceptance.CASE_MODULES)
    if harness.REGISTRY:
        present_modules = {case.fn.__module__ for case in harness.REGISTRY}
        if present_modules != expected_modules:
            raise RuntimeError(
                "acceptance registry was partially populated before bridge import: "
                f"expected {sorted(expected_modules)}, got {sorted(present_modules)}"
            )
        cases, problems = list(harness.REGISTRY), []
    else:
        cases, problems = run_acceptance.load_cases()
    if problems:
        raise RuntimeError("production acceptance registry is incomplete: " + "; ".join(problems))
    if len(cases) != EXPECTED_TOTAL:
        raise RuntimeError(
            f"production acceptance registry has {len(cases)} cases, expected {EXPECTED_TOTAL}"
        )
    tiers = {tier: sum(case.tier == tier for case in cases) for tier in EXPECTED_BY_TIER}
    if tiers != EXPECTED_BY_TIER:
        raise RuntimeError(
            f"production acceptance tier population changed: expected "
            f"{EXPECTED_BY_TIER}, got {tiers}"
        )
    external = [
        (case.issue, case.fn.__module__, case.fn.__name__)
        for case in cases
        if case.tier == "external_optional"
    ]
    if set(external) != EXTERNAL_OPTIONAL or len(external) != len(EXTERNAL_OPTIONAL):
        raise RuntimeError(
            f"external-optional acceptance population changed: {external}"
        )
    return cases


CASES = _declared_cases()


def _result_key(row):
    return (
        row.get("issue"),
        row.get("tier"),
        row.get("group"),
        row.get("owner"),
        row.get("check"),
        row.get("mutation_rejected"),
    )


def _case_key(case):
    return (
        case.issue,
        case.tier,
        case.group,
        case.owner,
        case.name,
        case.mutation,
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


class AcceptanceSuiteBridgeTests(unittest.TestCase):
    """One real runner invocation, projected into 63 per-case results."""

    _invocations = 0
    _temporary = None
    _by_key = None
    _returncode = None
    _stdout = ""
    _stderr = ""

    @classmethod
    def _cleanup_temporary(cls):
        if cls._temporary is not None:
            cls._temporary.cleanup()
            cls._temporary = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        db_text = os.environ.get("FERC_STAGING_DB", "").strip()
        if not db_text:
            raise AssertionError(
                "FERC_STAGING_DB is required; the acceptance bridge has no repository fallback"
            )
        db = pathlib.Path(db_text).expanduser().resolve()
        if not db.is_file() or db.stat().st_size == 0:
            raise AssertionError(f"FERC_STAGING_DB is absent or empty: {db}")
        wal = pathlib.Path(str(db) + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise AssertionError(
                f"refusing acceptance evidence while a non-empty SQLite WAL exists: {wal}"
            )
        if RUNNER.is_symlink() or not RUNNER.is_file():
            raise AssertionError(f"production acceptance runner is absent or aliased: {RUNNER}")

        cls._invocations += 1
        if cls._invocations != 1:
            raise AssertionError("production acceptance runner was invoked more than once")

        cls._temporary = tempfile.TemporaryDirectory(prefix="ferc-acceptance-bridge-")
        cls.addClassCleanup(cls._cleanup_temporary)
        private = pathlib.Path(cls._temporary.name)
        work = private / "work"
        home = private / "home"
        runtime_tmp = private / "tmp"
        result = private / "acceptance_results.json"
        for directory in (work, home, runtime_tmp):
            directory.mkdir()

        runner_before = RUNNER.read_bytes()
        runner_hash = hashlib.sha256(runner_before).hexdigest()
        bootstrap = textwrap.dedent(
            f"""
            import pathlib
            import runpy
            import sys

            root = pathlib.Path({str(ROOT)!r})
            runner = pathlib.Path({str(RUNNER)!r})
            work = pathlib.Path({str(work)!r})
            result = pathlib.Path({str(result)!r})
            sys.path.insert(0, str(root))
            from acceptance import harness
            harness.WORK = work
            sys.argv = [str(runner), "--out", str(result)]
            runpy.run_path(str(runner), run_name="__main__")
            """
        )
        env = dict(os.environ)
        for name in (
            "FERC_API_KEY",
            "FERC_SCHEMA_AUTOMIGRATE",
            "FERC_DEBUG_AGG",
            "PYTHONHOME",
            "PYTHONSTARTUP",
            "PYTHONUSERBASE",
        ):
            env.pop(name, None)
        env.update(
            {
                "HOME": str(home),
                "TMPDIR": str(runtime_tmp),
                "PYTHONPATH": "",
                "PYTHONNOUSERSITE": "1",
                "PYTHONSAFEPATH": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "FERC_OFFLINE": "1",
                "FERC_STAGING_DB": str(db),
                "FERC_ACCEPTANCE_REFERENCE_DB": str(db),
                "FERC_ACCEPTANCE_REFERENCE_TREE": str(ROOT),
            }
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", bootstrap],
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=3600,
        )
        cls._returncode = completed.returncode
        cls._stdout = completed.stdout
        cls._stderr = completed.stderr

        if RUNNER.read_bytes() != runner_before:
            raise AssertionError("production acceptance runner changed while it executed")
        if not result.is_file():
            raise AssertionError(
                "production acceptance runner emitted no machine-readable result; "
                f"exit={completed.returncode}; stdout={completed.stdout[-1200:]!r}; "
                f"stderr={completed.stderr[-1200:]!r}"
            )
        try:
            report = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AssertionError(f"acceptance result is not valid UTF-8 JSON: {exc}") from None
        combined = report.get("combined")
        rows = combined.get("results") if isinstance(combined, dict) else None
        if not isinstance(rows, list) or len(rows) != EXPECTED_TOTAL:
            raise AssertionError(
                f"acceptance result has {len(rows) if isinstance(rows, list) else 'no'} "
                f"case rows, expected {EXPECTED_TOTAL}"
            )
        if f"acceptance suite: {EXPECTED_TOTAL} registered case(s)" not in completed.stdout:
            raise AssertionError(
                "production runner stdout lacks its exact registered-case banner; "
                f"runner_sha256={runner_hash}"
            )

        actual = {}
        for row in rows:
            if not isinstance(row, dict):
                raise AssertionError("acceptance result contains a non-object case row")
            key = _result_key(row)
            if key in actual:
                raise AssertionError(f"acceptance result repeats case identity: {key}")
            actual[key] = row
        expected = {_case_key(case) for case in CASES}
        if set(actual) != expected:
            missing = sorted(expected - set(actual), key=repr)
            extra = sorted(set(actual) - expected, key=repr)
            raise AssertionError(
                f"runner/result population differs from the registered cases; "
                f"missing={missing[:3]}, extra={extra[:3]}"
            )

        declared_totals = combined.get("totals")
        statuses = ("PASS", "FAIL", "ERROR", "SKIPPED", "XFAIL", "XPASS")
        observed_totals = {
            status: sum(row.get("status") == status for row in rows) for status in statuses
        }
        if declared_totals != observed_totals:
            raise AssertionError(
                f"acceptance summary totals disagree with case rows: "
                f"declared={declared_totals}, observed={observed_totals}"
            )
        expected_returncode = (
            1 if observed_totals["FAIL"] or observed_totals["ERROR"] else 0
        )
        if completed.returncode != expected_returncode:
            raise AssertionError(
                "production acceptance runner exit status disagrees with its case rows: "
                f"exit={completed.returncode}, expected={expected_returncode}, "
                f"totals={observed_totals}"
            )
        cls._by_key = actual

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_temporary()
        super().tearDownClass()


def _case_method(case):
    key = _case_key(case)
    external_identity = (case.issue, case.fn.__module__, case.fn.__name__)

    def test_case(self):
        row = self._by_key[key]
        status = row.get("status")
        detail = str(row.get("detail") or "")
        if status == "PASS":
            return
        if status == "SKIPPED" and external_identity in EXTERNAL_OPTIONAL:
            self.skipTest(
                "actual production runner classified an exact external_optional case "
                f"as unavailable: {detail}"
            )
        self.fail(
            f"production acceptance case returned {status!r}, which is not an accepted "
            f"outcome for tier {case.tier!r}; runner_exit={self._returncode}; "
            f"detail={detail}; stderr={self._stderr[-600:]!r}"
        )

    test_case.__doc__ = (
        f"[{case.tier}] {case.issue}: {case.name} "
        f"({case.fn.__module__}.{case.fn.__name__})"
    )
    return test_case


_method_names = set()
for _number, _case in enumerate(CASES, 1):
    _module = _case.fn.__module__.rsplit(".", 1)[-1]
    _name = (
        f"test_case_{_number:03d}_{_slug(_case.issue)}_"
        f"{_slug(_module)}_{_slug(_case.fn.__name__)}"
    )
    if _name in _method_names:
        raise RuntimeError(f"duplicate generated acceptance bridge test id: {_name}")
    _method_names.add(_name)
    setattr(AcceptanceSuiteBridgeTests, _name, _case_method(_case))


if __name__ == "__main__":
    unittest.main()
