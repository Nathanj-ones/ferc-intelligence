from __future__ import annotations

import json
import sys
from pathlib import Path


REQUIRED_TOP_LEVEL = {
    "project",
    "docket",
    "current_stage",
    "generated_at",
    "counts",
    "milestones",
    "activity",
    "regulatory_outlook",
}

REQUIRED_COUNTS = {
    "raw",
    "deduped",
    "alerts",
    "review",
    "suppressed",
    "activity_groups",
}

REQUIRED_MILESTONE_FIELDS = {
    "accession",
    "date",
    "event_type",
    "title",
    "description",
}

REQUIRED_ACTIVITY_FIELDS = {
    "activity_type",
    "label",
    "start_date",
    "end_date",
    "filing_count",
    "event_types",
    "relationship",
    "representative_accessions",
    "summary",
}


REQUIRED_OUTLOOK_FIELDS = {
    "regulatory_status",
    "tracked_ferc_requests",
    "responded_ferc_requests",
    "open_ferc_requests",
    "latest_material_milestone",
    "schedule_watch",
    "progression_counts",
    "next_expected_activity",
    "investor_summary",
}

REQUIRED_SCHEDULE_WATCH_FIELDS = {
    "kind",
    "request_accession",
    "request_date",
    "estimated_due_date",
    "response_date",
    "detail",
}



def fail(message: str) -> None:
    print(f"FAIL | {message}")
    raise SystemExit(1)


def validate_file(path: Path) -> None:
    print()
    print("=" * 80)
    print(f"VALIDATING: {path}")
    print("=" * 80)

    if not path.exists():
        fail(f"File does not exist: {path}")

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        fail(f"Invalid JSON: {exc}")

    if not isinstance(data, dict):
        fail("Top-level JSON value must be an object.")

    missing = (
        REQUIRED_TOP_LEVEL
        - set(data.keys())
    )

    if missing:
        fail(
            "Missing top-level fields: "
            + ", ".join(sorted(missing))
        )

    if not isinstance(
        data["project"],
        str,
    ):
        fail("project must be a string.")

    if not isinstance(
        data["docket"],
        str,
    ):
        fail("docket must be a string.")

    if not isinstance(
        data["current_stage"],
        str,
    ):
        fail("current_stage must be a string.")

    if not isinstance(
        data["generated_at"],
        str,
    ):
        fail("generated_at must be a string.")

    counts = data["counts"]

    if not isinstance(
        counts,
        dict,
    ):
        fail("counts must be an object.")

    missing_counts = (
        REQUIRED_COUNTS
        - set(counts.keys())
    )

    if missing_counts:
        fail(
            "Missing count fields: "
            + ", ".join(
                sorted(missing_counts)
            )
        )

    for field in REQUIRED_COUNTS:
        if not isinstance(
            counts[field],
            int,
        ):
            fail(
                f"counts.{field} must be an integer."
            )

        if counts[field] < 0:
            fail(
                f"counts.{field} cannot be negative."
            )

    milestones = data["milestones"]

    if not isinstance(
        milestones,
        list,
    ):
        fail("milestones must be an array.")

    for index, milestone in enumerate(
        milestones
    ):
        if not isinstance(
            milestone,
            dict,
        ):
            fail(
                f"milestones[{index}] must be an object."
            )

        missing_fields = (
            REQUIRED_MILESTONE_FIELDS
            - set(milestone.keys())
        )

        if missing_fields:
            fail(
                f"milestones[{index}] missing: "
                + ", ".join(
                    sorted(missing_fields)
                )
            )

    activity = data["activity"]

    if not isinstance(
        activity,
        list,
    ):
        fail("activity must be an array.")

    for index, item in enumerate(
        activity
    ):
        if not isinstance(
            item,
            dict,
        ):
            fail(
                f"activity[{index}] must be an object."
            )

        missing_fields = (
            REQUIRED_ACTIVITY_FIELDS
            - set(item.keys())
        )

        if missing_fields:
            fail(
                f"activity[{index}] missing: "
                + ", ".join(
                    sorted(missing_fields)
                )
            )

        if not isinstance(
            item["filing_count"],
            int,
        ):
            fail(
                f"activity[{index}].filing_count "
                "must be an integer."
            )

        if item["filing_count"] < 0:
            fail(
                f"activity[{index}].filing_count "
                "cannot be negative."
            )

        if not isinstance(
            item["event_types"],
            dict,
        ):
            fail(
                f"activity[{index}].event_types "
                "must be an object."
            )

        if not isinstance(
            item["representative_accessions"],
            list,
        ):
            fail(
                f"activity[{index}].representative_accessions "
                "must be an array."
            )

        for accession in (
            item["representative_accessions"]
        ):
            if not isinstance(
                accession,
                str,
            ):
                fail(
                    f"activity[{index}] contains "
                    "a non-string accession."
                )

    outlook = data["regulatory_outlook"]

    if not isinstance(outlook, dict):
        fail("regulatory_outlook must be an object.")

    missing_outlook = (
        REQUIRED_OUTLOOK_FIELDS
        - set(outlook.keys())
    )

    if missing_outlook:
        fail(
            "regulatory_outlook missing: "
            + ", ".join(sorted(missing_outlook))
        )

    for field in (
        "tracked_ferc_requests",
        "responded_ferc_requests",
        "open_ferc_requests",
    ):
        if not isinstance(outlook[field], int):
            fail(f"regulatory_outlook.{field} must be an integer.")
        if outlook[field] < 0:
            fail(f"regulatory_outlook.{field} cannot be negative.")

    if outlook["responded_ferc_requests"] > outlook["tracked_ferc_requests"]:
        fail("responded_ferc_requests cannot exceed tracked_ferc_requests.")

    if outlook["open_ferc_requests"] != (
        outlook["tracked_ferc_requests"]
        - outlook["responded_ferc_requests"]
    ):
        fail("open_ferc_requests must equal tracked minus responded.")

    if not isinstance(outlook["regulatory_status"], str):
        fail("regulatory_outlook.regulatory_status must be a string.")

    if not isinstance(outlook["next_expected_activity"], str):
        fail("regulatory_outlook.next_expected_activity must be a string.")

    if not isinstance(outlook["investor_summary"], str):
        fail("regulatory_outlook.investor_summary must be a string.")

    if not isinstance(outlook["progression_counts"], dict):
        fail("regulatory_outlook.progression_counts must be an object.")

    schedule_watch = outlook["schedule_watch"]
    if not isinstance(schedule_watch, list):
        fail("regulatory_outlook.schedule_watch must be an array.")

    for index, watch in enumerate(schedule_watch):
        if not isinstance(watch, dict):
            fail(f"schedule_watch[{index}] must be an object.")
        missing_watch = REQUIRED_SCHEDULE_WATCH_FIELDS - set(watch.keys())
        if missing_watch:
            fail(
                f"schedule_watch[{index}] missing: "
                + ", ".join(sorted(missing_watch))
            )

    latest = outlook["latest_material_milestone"]
    if latest is not None:
        if not isinstance(latest, dict):
            fail("latest_material_milestone must be an object or null.")
        missing_latest = REQUIRED_MILESTONE_FIELDS - set(latest.keys())
        if missing_latest:
            fail(
                "latest_material_milestone missing: "
                + ", ".join(sorted(missing_latest))
            )

    # Cross-check the derived activity count.
    if (
        counts["activity_groups"]
        != len(activity)
    ):
        fail(
            "counts.activity_groups does not "
            "match activity array length."
        )

    print("PASS | ProjectView v2 contract valid.")
    print(
        f"Project: {data['project']}"
    )
    print(
        f"Docket: {data['docket']}"
    )
    print(
        f"Stage: {data['current_stage']}"
    )
    print(
        f"Milestones: {len(milestones)}"
    )
    print(
        f"Activity groups: {len(activity)}"
    )
    print(
        f"Regulatory status: {outlook['regulatory_status']}"
    )
    print(
        f"Open FERC requests: {outlook['open_ferc_requests']}"
    )


def main() -> None:
    if len(sys.argv) < 2:
        paths = [
            Path(
                "data/project_view_CP25-10.json"
            ),
            Path(
                "data/project_view_CP25-528.json"
            ),
            Path(
                "data/project_view_CP25-219.json"
            ),
        ]
    else:
        paths = [
            Path(arg)
            for arg in sys.argv[1:]
        ]

    for path in paths:
        validate_file(path)


if __name__ == "__main__":
    main()