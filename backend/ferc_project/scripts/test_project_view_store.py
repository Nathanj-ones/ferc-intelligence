from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ferc_filter.activity_grouper import (
    ActivityGroup,
    ActivityRecord,
)
from ferc_filter.project_view import (
    build_project_view,
)
from ferc_filter.project_view_store import (
    ProjectViewStore,
    ProjectViewValidationError,
)


TEST_DIR = Path(
    "data/project_view_store_test"
)


def build_test_view():
    alerts = [
        {
            "accession": "20260129-3076",
            "date": datetime(
                2026,
                1,
                29,
            ),
            "event_type": (
                "regulatory_decision"
            ),
            "description": (
                "Order Issuing Certificate "
                "for Test Project."
            ),
        },
        {
            "accession": "20260225-3042",
            "date": datetime(
                2026,
                2,
                25,
            ),
            "event_type": (
                "construction_or_service_authorization"
            ),
            "description": (
                "Letter granting request to "
                "commence construction."
            ),
        },
    ]

    records = [
        ActivityRecord(
            project="Test Project",
            docket="CP00-000",
            accession="20260201-0001",
            date=datetime(
                2026,
                2,
                1,
            ),
            event_type=(
                "ferc_information_request"
            ),
            description=(
                "Letter requesting applicant "
                "to respond to data request."
            ),
        ),
        ActivityRecord(
            project="Test Project",
            docket="CP00-000",
            accession="20260205-0002",
            date=datetime(
                2026,
                2,
                5,
            ),
            event_type=(
                "applicant_followup"
            ),
            description=(
                "Applicant submits response "
                "to FERC's 02/01/2026 "
                "data request."
            ),
        ),
    ]

    group = ActivityGroup(
        project="Test Project",
        docket="CP00-000",
        activity_type=(
            "information_exchange"
        ),
        relationship=(
            "referenced_prior_date"
        ),
        records=records,
    )

    return build_project_view(
        project="Test Project",
        docket="CP00-000",
        raw_count=10,
        deduped_count=10,
        alerts=alerts,
        review_count=6,
        suppressed_count=2,
        activity_groups=[group],
    )


def main():
    if TEST_DIR.exists():
        shutil.rmtree(
            TEST_DIR
        )

    store = ProjectViewStore(
        TEST_DIR
    )

    print()
    print("=" * 80)
    print("PROJECT VIEW STORE TEST")
    print("=" * 80)

    # ----------------------------------------------------------
    # TEST 1: SAVE
    # ----------------------------------------------------------

    view = build_test_view()

    path = store.save(
        view
    )

    print()
    print(
        "PASS | Initial save"
    )
    print(
        "Path:",
        path,
    )

    # ----------------------------------------------------------
    # TEST 2: LOAD
    # ----------------------------------------------------------

    loaded = store.load(
        "CP00-000"
    )

    assert loaded is not None

    assert (
        loaded["current_stage"]
        == "Construction"
    )

    assert (
        loaded["counts"][
            "activity_groups"
        ]
        == 1
    )

    print(
        "PASS | Load + validation"
    )

    # ----------------------------------------------------------
    # TEST 3: ATOMIC REPLACEMENT
    # ----------------------------------------------------------

    original_generated_at = (
        loaded["generated_at"]
    )

    replacement = replace(
        view,
        generated_at=(
            "2026-09-08T15:00:00+00:00"
        ),
    )

    store.save(
        replacement
    )

    loaded_again = store.load(
        "CP00-000"
    )

    assert loaded_again is not None

    assert (
        loaded_again["generated_at"]
        != original_generated_at
    )

    assert (
        loaded_again["generated_at"]
        == "2026-09-08T15:00:00+00:00"
    )

    print(
        "PASS | Atomic replacement"
    )

    # ----------------------------------------------------------
    # TEST 4: INVALID DATA MUST NOT REPLACE GOOD FILE
    # ----------------------------------------------------------

    good_path = store.path_for_docket(
        "CP00-000"
    )

    with good_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        good_before = file.read()

    bad_data = dict(
        loaded_again
    )

    bad_data["counts"] = dict(
        bad_data["counts"]
    )

    bad_data["counts"][
        "activity_groups"
    ] = 999

    try:
        store.validate_dict(
            bad_data
        )

    except ProjectViewValidationError:
        print(
            "PASS | Invalid object rejected"
        )

    else:
        raise AssertionError(
            "Invalid ProjectView was accepted."
        )

    with good_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        good_after = file.read()

    assert (
        good_before
        == good_after
    )

    print(
        "PASS | Existing JSON preserved"
    )

    # ----------------------------------------------------------
    # TEST 5: JSON IS READABLE
    # ----------------------------------------------------------

    with good_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        json.load(
            file
        )

    print(
        "PASS | Stored JSON readable"
    )

    print()
    print(
        store.describe(
            "CP00-000"
        )
    )

    print()
    print("=" * 80)
    print("ALL STORE TESTS PASSED")
    print("=" * 80)


if __name__ == "__main__":
    main()