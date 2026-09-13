from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ferc_filter.project_view import (
    ProjectView,
    project_view_to_dict,
)


PROJECT_VIEW_VERSION = "1"


class ProjectViewValidationError(ValueError):
    pass


class ProjectViewStore:
    """
    Persistence layer for dashboard ProjectView JSON files.

    Responsibilities:
        - determine the output path for a docket
        - load an existing ProjectView JSON object
        - validate the ProjectView v1 contract
        - atomically save a new ProjectView

    ProjectView JSON is a derived dashboard artifact.
    It is not the underlying source of truth.
    """

    def __init__(
        self,
        output_dir: str | Path = "data",
    ):
        self.output_dir = Path(
            output_dir
        )

    # ==========================================================
    # PATHS
    # ==========================================================

    @staticmethod
    def _safe_docket(
        docket: str,
    ) -> str:
        """
        Make a docket safe for use in a filename.
        """

        return (
            docket
            .replace("/", "_")
            .replace("\\", "_")
        )

    def path_for_docket(
        self,
        docket: str,
    ) -> Path:
        safe_docket = self._safe_docket(
            docket
        )

        return (
            self.output_dir
            / f"project_view_{safe_docket}.json"
        )

    # ==========================================================
    # LOAD
    # ==========================================================

    def exists(
        self,
        docket: str,
    ) -> bool:
        return self.path_for_docket(
            docket
        ).exists()

    def load(
        self,
        docket: str,
    ) -> dict[str, Any] | None:
        """
        Load an existing ProjectView JSON object.

        Returns None when no view exists yet.
        """

        path = self.path_for_docket(
            docket
        )

        if not path.exists():
            return None

        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(
                file
            )

        self.validate_dict(
            data
        )

        return data

    # ==========================================================
    # VALIDATION
    # ==========================================================

    @staticmethod
    def validate_dict(
        data: dict[str, Any],
    ) -> None:
        """
        Validate the stable ProjectView v1 dashboard contract.

        Raises ProjectViewValidationError on failure.
        """

        if not isinstance(
            data,
            dict,
        ):
            raise ProjectViewValidationError(
                "ProjectView must be an object."
            )

        required_top_level = {
            "project",
            "docket",
            "current_stage",
            "generated_at",
            "counts",
            "milestones",
            "activity",
        }

        missing = (
            required_top_level
            - set(data)
        )

        if missing:
            raise ProjectViewValidationError(
                "Missing top-level fields: "
                + ", ".join(
                    sorted(missing)
                )
            )

        for field in (
            "project",
            "docket",
            "current_stage",
            "generated_at",
        ):
            if not isinstance(
                data[field],
                str,
            ):
                raise ProjectViewValidationError(
                    f"{field} must be a string."
                )

        counts = data["counts"]

        if not isinstance(
            counts,
            dict,
        ):
            raise ProjectViewValidationError(
                "counts must be an object."
            )

        required_counts = {
            "raw",
            "deduped",
            "alerts",
            "review",
            "suppressed",
            "activity_groups",
        }

        missing_counts = (
            required_counts
            - set(counts)
        )

        if missing_counts:
            raise ProjectViewValidationError(
                "Missing count fields: "
                + ", ".join(
                    sorted(missing_counts)
                )
            )

        for field in required_counts:
            value = counts[field]

            if (
                not isinstance(
                    value,
                    int,
                )
                or isinstance(
                    value,
                    bool,
                )
            ):
                raise ProjectViewValidationError(
                    f"counts.{field} "
                    "must be an integer."
                )

            if value < 0:
                raise ProjectViewValidationError(
                    f"counts.{field} "
                    "cannot be negative."
                )

        milestones = data[
            "milestones"
        ]

        if not isinstance(
            milestones,
            list,
        ):
            raise ProjectViewValidationError(
                "milestones must be an array."
            )

        required_milestone_fields = {
            "accession",
            "date",
            "event_type",
            "title",
            "description",
        }

        for index, milestone in enumerate(
            milestones
        ):
            if not isinstance(
                milestone,
                dict,
            ):
                raise ProjectViewValidationError(
                    f"milestones[{index}] "
                    "must be an object."
                )

            missing_fields = (
                required_milestone_fields
                - set(milestone)
            )

            if missing_fields:
                raise ProjectViewValidationError(
                    f"milestones[{index}] "
                    "missing: "
                    + ", ".join(
                        sorted(
                            missing_fields
                        )
                    )
                )

            if not isinstance(
                milestone["accession"],
                str,
            ):
                raise ProjectViewValidationError(
                    f"milestones[{index}]"
                    ".accession must be a string."
                )

        activity = data[
            "activity"
        ]

        if not isinstance(
            activity,
            list,
        ):
            raise ProjectViewValidationError(
                "activity must be an array."
            )

        required_activity_fields = {
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

        for index, item in enumerate(
            activity
        ):
            if not isinstance(
                item,
                dict,
            ):
                raise ProjectViewValidationError(
                    f"activity[{index}] "
                    "must be an object."
                )

            missing_fields = (
                required_activity_fields
                - set(item)
            )

            if missing_fields:
                raise ProjectViewValidationError(
                    f"activity[{index}] "
                    "missing: "
                    + ", ".join(
                        sorted(
                            missing_fields
                        )
                    )
                )

            filing_count = item[
                "filing_count"
            ]

            if (
                not isinstance(
                    filing_count,
                    int,
                )
                or isinstance(
                    filing_count,
                    bool,
                )
            ):
                raise ProjectViewValidationError(
                    f"activity[{index}]"
                    ".filing_count must be "
                    "an integer."
                )

            if filing_count < 1:
                raise ProjectViewValidationError(
                    f"activity[{index}]"
                    ".filing_count must be "
                    "at least 1."
                )

            if not isinstance(
                item["event_types"],
                dict,
            ):
                raise ProjectViewValidationError(
                    f"activity[{index}]"
                    ".event_types must be "
                    "an object."
                )

            accessions = item[
                "representative_accessions"
            ]

            if not isinstance(
                accessions,
                list,
            ):
                raise ProjectViewValidationError(
                    f"activity[{index}]"
                    ".representative_accessions "
                    "must be an array."
                )

            if not all(
                isinstance(
                    accession,
                    str,
                )
                for accession in accessions
            ):
                raise ProjectViewValidationError(
                    f"activity[{index}] "
                    "contains a non-string "
                    "accession."
                )

        if (
            counts["activity_groups"]
            != len(activity)
        ):
            raise ProjectViewValidationError(
                "counts.activity_groups "
                "does not match the activity "
                "array length."
            )

        if (
            counts["alerts"]
            != len(milestones)
        ):
            raise ProjectViewValidationError(
                "counts.alerts does not match "
                "the milestones array length."
            )

        if (
            counts["deduped"]
            != (
                counts["alerts"]
                + counts["review"]
                + counts["suppressed"]
            )
        ):
            raise ProjectViewValidationError(
                "deduped count does not equal "
                "alerts + review + suppressed."
            )

    # ==========================================================
    # SAVE
    # ==========================================================

    def save(
        self,
        view: ProjectView,
    ) -> Path:
        """
        Validate and atomically save a ProjectView.

        The existing JSON is not replaced unless:
            1. serialization succeeds
            2. validation succeeds
            3. temporary-file writing succeeds
        """

        data = project_view_to_dict(
            view
        )

        self.validate_dict(
            data
        )

        self.output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        destination = (
            self.path_for_docket(
                view.docket
            )
        )

        temp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.output_dir,
                prefix=(
                    f".{destination.stem}_"
                ),
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(
                    temp_file.name
                )

                json.dump(
                    data,
                    temp_file,
                    indent=2,
                    ensure_ascii=False,
                )

                temp_file.write(
                    "\n"
                )

                temp_file.flush()

                os.fsync(
                    temp_file.fileno()
                )

            # Verify that what we actually wrote
            # can be read and passes the contract.
            with temp_path.open(
                "r",
                encoding="utf-8",
            ) as file:
                written_data = json.load(
                    file
                )

            self.validate_dict(
                written_data
            )

            # Atomic replacement on the same filesystem.
            os.replace(
                temp_path,
                destination,
            )

            temp_path = None

            return destination

        finally:
            if (
                temp_path is not None
                and temp_path.exists()
            ):
                temp_path.unlink()

    # ==========================================================
    # SUMMARY
    # ==========================================================

    def describe(
        self,
        docket: str,
    ) -> str:
        data = self.load(
            docket
        )

        if data is None:
            return (
                f"{docket}: "
                "no ProjectView stored"
            )

        return (
            f"{data['project']} "
            f"({data['docket']}) | "
            f"stage={data['current_stage']} | "
            f"milestones="
            f"{len(data['milestones'])} | "
            f"activity="
            f"{len(data['activity'])}"
        )