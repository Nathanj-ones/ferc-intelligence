from __future__ import annotations

from ferc_filter.app_project import AppProject, app_project_to_dict


def attach_project_evidence(
    app_project: AppProject,
    evidence: list[dict],
) -> AppProject:
    """
    Add app-facing filing evidence without changing the upstream
    classification or project intelligence layers.
    """

    app_project.evidence = {
        "latest_material_accession": (
            app_project.evidence.get(
                "latest_material_accession"
            )
        ),
        "key_milestone_accessions": (
            app_project.evidence.get(
                "key_milestone_accessions",
                [],
            )
        ),
        "material_filings": evidence,
    }

    return app_project
