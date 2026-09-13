from dataclasses import dataclass

from ferc_filter.project_materiality import (
    DROP,
    TRACK,
    WATCH,
    assess_materiality,
)


@dataclass
class Candidate:
    docket: str
    project_names: list[str]
    sample_filings: list[str]


@dataclass
class Qualification:
    decision: str


growth = Candidate(
    docket="CP26-999",
    project_names=["Example Expansion Project"],
    sample_filings=[
        "Abbreviated Application for a Certificate to construct a new "
        "pipeline and provide additional capacity.",
        "Response to FERC's data request.",
    ],
)
result = assess_materiality(
    growth,
    Qualification("PROMOTE"),
    capacity=750000,
)
assert result.decision == TRACK
assert result.capacity == 750000
assert result.capex is None
print("PASS | Scaled active growth project is tracked")
print("PASS | Missing capex retained without inference")

unknown_scale = Candidate(
    docket="CP26-998",
    project_names=["Example Connector Project"],
    sample_filings=[
        "Prior Notice Request to construct a new interconnection."
    ],
)
result = assess_materiality(
    unknown_scale,
    Qualification("PROMOTE"),
)
assert result.decision in {WATCH, TRACK}
assert result.capex is None
assert result.capacity is None
print("PASS | Active project with unknown scale remains visible")

abandonment = Candidate(
    docket="CP26-997",
    project_names=["Example Abandonment Project"],
    sample_filings=[
        "Application for abandonment authorization for existing facilities."
    ],
)
result = assess_materiality(
    abandonment,
    Qualification("PROMOTE"),
)
assert result.decision != TRACK
print("PASS | Abandonment-only project cannot auto-track")

ignored = Candidate(
    docket="CP26-996",
    project_names=["Routine Project"],
    sample_filings=["Semi-annual operating report."],
)
result = assess_materiality(
    ignored,
    Qualification("IGNORE"),
)
assert result.decision == DROP
print("PASS | Qualification IGNORE bypasses materiality tracking")

print()
print("ALL PROJECT MATERIALITY v0.1 TESTS PASSED")
