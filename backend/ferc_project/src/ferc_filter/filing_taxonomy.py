"""General FERC filing-type triage taxonomy.

Class/Type is used as a first-stage priority signal only.

HIGH:
    Filing types that should normally receive detailed inspection.

CONTEXT:
    Filing types where the description and surrounding events determine
    whether further inspection is warranted.

LOW:
    Filing types that are usually routine or procedural.

UNKNOWN:
    Filing types we have not yet evaluated sufficiently.
"""

from __future__ import annotations


HIGH_PRIORITY_TYPES = {
    (
        "Application/Petition/Request",
        "Certificate of Public Convenience and Necessity",
    ),
    (
        "Order/Opinion",
        "Commission Order/Opinion",
    ),
    (
        "FERC Report/Study",
        "Environmental Assessment (EA) and Environmental Impact Statement (EIS)",
    ),
    (
        "Applicant Correspondence",
        "Request for Delay of Action/Extension of Time",
    ),
    (
        "Pleading/Motion",
        "Request for Rehearing or Appeal",
    ),
}


CONTEXT_PRIORITY_TYPES = {
    (
        "Order/Opinion",
        "Delegated Order",
    ),
    (
        "Notice",
        "Formal Notice",
    ),
    (
        "Report/Form",
        "Certificate of Compliance Report",
    ),
    (
        "FERC Report/Study",
        "Construction Inspection Report",
    ),
    (
        "FERC Correspondence With Applicant",
        "Request for Additional Information",
    ),
    (
        "FERC Correspondence With Applicant",
        "General Correspondence",
    ),
    (
        "Applicant Correspondence",
        "General Correspondence",
    ),
    (
        "Applicant Correspondence",
        "Supplemental/Additional Information",
    ),
    (
        "FERC Correspondence With Government Agencies",
        "FERC Correspondence With Government Agencies",
    ),
    (
        "FERC Correspondence with Tribal Governments",
        "FERC Correspondence with Tribal Governments",
    ),
    (
        "Other Submittal",
        "Government Agency Submittal",
    ),
    (
        "Other Submittal",
        "Tribal Government Submittal",
    ),
    (
        "Pleading/Motion",
        "Request for Hearing",
    ),
    (
        "FERC Memo",
        "Telephone Conversation or Electronic Mail Memo",
    ),
}


LOW_PRIORITY_TYPES = {
    (
        "Comments/Protest",
        "Comment on Filing",
    ),
    (
        "Pleading/Motion",
        "Procedural Motion",
    ),
    (
        "Pleading/Motion",
        "Answer/Response to a Pleading/Motion",
    ),
    (
        "Transcript",
        "Conference/Meeting Transcript",
    ),
    (
        "Intervention",
        "Motion/Notice of Intervention",
    ),
    (
        "Intervention",
        "Motion to Intervene Out of Time",
    ),
}


def classify_filing_type(
    document_class: str | None,
    document_type: str | None,
) -> str:
    """Return the first-stage priority for a FERC filing type."""

    key = (
        document_class or "",
        document_type or "",
    )

    if key in HIGH_PRIORITY_TYPES:
        return "HIGH"

    if key in CONTEXT_PRIORITY_TYPES:
        return "CONTEXT"

    if key in LOW_PRIORITY_TYPES:
        return "LOW"

    return "UNKNOWN"