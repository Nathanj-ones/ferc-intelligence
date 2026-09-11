#!/usr/bin/env python3
"""Fail-closed parser for complete ``unittest -v`` logs.

CPython may print a test's short description on the line after its verbose
identifier. Tests that write to stdout/stderr may also place arbitrary output
between unittest's ``...`` marker and its terminal per-test outcome. Parsing
one physical line therefore silently loses cases. This module segments the log
by verbose headers and requires exactly one unambiguous outcome per segment.
"""

from __future__ import annotations

import re
from typing import Any, Dict


COUNT_KEYS = ("collected", "executed", "pass", "fail", "error", "skip",
              "xfail", "xpass")
SUMMARY_RE = re.compile(
    r"^Ran\s+(\d+)\s+tests?\s+in\s+[^\r\n]+\s*$", re.MULTILINE)
TERMINAL_OK_RE = re.compile(
    r"^OK(?:\s*\(([^\r\n)]*)\))?\s*$", re.MULTILINE)
TERMINAL_FAILED_RE = re.compile(
    r"^FAILED\s*\(([^\r\n)]*)\)\s*$", re.MULTILINE)

# The optional second line is unittest.TestCase.shortDescription(). unittest
# emits its ellipsis before calling the test, so this safely starts a segment
# even when the test subsequently writes arbitrary diagnostic output.
HEADER_RE = re.compile(
    r"^(test[^\s()]+) \(([^)\r\n]+)\)"
    r"(?:\r?\n[^\r\n]*)?[ \t]+\.\.\.[ \t]*",
    re.MULTILINE,
)
OUTCOME_TEXT = (
    r"ok|FAIL|ERROR|expected failure|unexpected success|"
    r"skipped(?:[ \t]+[^\r\n]*)?"
)
OUTCOME_LINE_RE = re.compile(
    r"^(" + OUTCOME_TEXT + r")[ \t]*$", re.MULTILINE)


class UnittestLogRefused(ValueError):
    """The bytes do not prove one complete, internally consistent test run."""


def _parse_count_items(text: str) -> Dict[str, int]:
    aliases = {
        "failures": "fail", "errors": "error", "skipped": "skip",
        "expected failures": "xfail", "unexpected successes": "xpass",
    }
    counts = {name: 0 for name in ("fail", "error", "skip", "xfail", "xpass")}
    seen = set()
    if not text.strip():
        return counts
    for item in text.split(","):
        if item.count("=") != 1:
            raise UnittestLogRefused(
                f"malformed unittest terminal count item {item!r}")
        key, value = (part.strip().lower() for part in item.split("=", 1))
        if key not in aliases or not value.isdigit() or int(value) <= 0:
            raise UnittestLogRefused(
                f"unknown or malformed unittest terminal count item {item!r}")
        name = aliases[key]
        if name in seen:
            raise UnittestLogRefused(
                f"duplicate unittest terminal count item {key!r}")
        seen.add(name)
        counts[name] = int(value)
    return counts


def _normalise_test_id(method: str, parenthetical_id: str, label: str) -> str:
    """Normalise CPython 3.9 and 3.14 verbose identities without guessing."""
    last_component = parenthetical_id.rsplit(".", 1)[-1]
    if last_component == method:
        return parenthetical_id
    if last_component.startswith("test"):
        raise UnittestLogRefused(
            f"{label} has inconsistent verbose test id {parenthetical_id}")
    if "." not in parenthetical_id:
        raise UnittestLogRefused(
            f"{label} has incomplete verbose test parent {parenthetical_id}")
    return parenthetical_id + "." + method


def parse_unittest_log(raw: bytes, label: str) -> Dict[str, Any]:
    """Return exact results/counts for one complete verbose log or refuse it."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnittestLogRefused(
            f"{label} is not valid UTF-8: {exc}") from None
    if re.search(r"\r(?!\n)", text):
        raise UnittestLogRefused(f"{label} contains an ambiguous bare CR")

    summaries = list(SUMMARY_RE.finditer(text))
    if len(summaries) != 1:
        raise UnittestLogRefused(
            f"{label} must contain exactly one unittest 'Ran N tests' summary; "
            f"found {len(summaries)}")
    summary = summaries[0]
    executed = int(summary.group(1))
    if executed == 0:
        raise UnittestLogRefused(f"{label} is a zero-case run")

    tail = text[summary.end():]
    oks = list(TERMINAL_OK_RE.finditer(tail))
    failures = list(TERMINAL_FAILED_RE.finditer(tail))
    if len(oks) + len(failures) != 1:
        raise UnittestLogRefused(
            f"{label} lacks one unambiguous unittest result line")
    terminal_ok = bool(oks)
    terminal = (oks or failures)[0]
    if tail[:terminal.start()].strip() or tail[terminal.end():].strip():
        raise UnittestLogRefused(
            f"{label} has nonblank material outside its terminal result line")

    result_region = text[:summary.start()]
    headers = list(HEADER_RE.finditer(result_region))
    if len(headers) != executed:
        raise UnittestLogRefused(
            f"{label} is incomplete or non-verbose: Ran {executed}, but found "
            f"{len(headers)} verbose test headers")

    status_map = {
        "ok": "pass", "FAIL": "fail", "ERROR": "error",
        "expected failure": "xfail", "unexpected success": "xpass",
    }
    results: Dict[str, str] = {}
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) \
            else len(result_region)
        segment = result_region[header.end():end]
        outcomes = list(OUTCOME_LINE_RE.finditer(segment))
        method, parenthetical_id = header.groups()
        test_id = _normalise_test_id(method, parenthetical_id, label)
        if len(outcomes) != 1:
            raise UnittestLogRefused(
                f"{label} test {test_id} has {len(outcomes)} unambiguous "
                "per-test outcome lines")
        if test_id in results:
            raise UnittestLogRefused(
                f"{label} repeats verbose test id {test_id}")
        outcome = outcomes[0].group(1)
        results[test_id] = "skip" if outcome.startswith("skipped") \
            else status_map[outcome]

    counts = {name: sum(status == name for status in results.values())
              for name in ("pass", "fail", "error", "skip", "xfail", "xpass")}
    try:
        summary_counts = _parse_count_items(terminal.group(1) or "")
    except UnittestLogRefused as exc:
        raise UnittestLogRefused(f"{label} {exc}") from None
    for name in ("fail", "error", "skip", "xfail", "xpass"):
        if counts[name] != summary_counts[name]:
            raise UnittestLogRefused(
                f"{label} verbose {name} count {counts[name]} disagrees with "
                f"summary {summary_counts[name]}")
    hard_failures = counts["fail"] + counts["error"] + counts["xpass"]
    if terminal_ok and hard_failures:
        raise UnittestLogRefused(
            f"{label} terminal OK contradicts failing per-test outcomes")
    if not terminal_ok and not hard_failures:
        raise UnittestLogRefused(
            f"{label} terminal FAILED has no failing per-test outcome")
    counts["executed"] = executed
    counts["collected"] = executed
    successful = terminal_ok and not any(
        counts[name] for name in ("fail", "error", "xpass"))
    return {
        "counts": {key: counts[key] for key in COUNT_KEYS},
        "results": results,
        "successful": successful,
        "text": text,
    }
