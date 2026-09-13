from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


ACCESSION_PATTERN = re.compile(
    r"\b20\d{6}-\d{4}\b"
)

DATE_PATTERNS = (
    re.compile(
        r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b"
    ),
    re.compile(
        r"\b(20\d{2})-(\d{2})-(\d{2})\b"
    ),
)


@dataclass
class ActivityRecord:
    project: str
    docket: str
    accession: str
    date: Optional[datetime]
    event_type: str
    description: str
    document_class: Optional[str] = None
    document_type: Optional[str] = None

    referenced_accessions: set[str] = field(
        default_factory=set
    )

    referenced_dates: set[str] = field(
        default_factory=set
    )


@dataclass
class ActivityGroup:
    project: str
    docket: str
    activity_type: str
    relationship: str
    records: list[ActivityRecord] = field(
        default_factory=list
    )


# ==============================================================
# TEXT / DATE EXTRACTION
# ==============================================================

def extract_referenced_accessions(
    description: str,
    own_accession: str,
) -> set[str]:
    """
    Extract explicit FERC accession references from the
    filing description.
    """

    matches = set(
        ACCESSION_PATTERN.findall(
            description or ""
        )
    )

    matches.discard(
        own_accession
    )

    return matches


def extract_referenced_dates(
    description: str,
) -> set[str]:
    """
    Extract explicit dates from the filing description
    and normalize them to YYYY-MM-DD.
    """

    text = description or ""
    dates: set[str] = set()

    for match in DATE_PATTERNS[0].finditer(
        text
    ):
        month, day, year = match.groups()

        try:
            parsed = datetime(
                int(year),
                int(month),
                int(day),
            )
        except ValueError:
            continue

        dates.add(
            parsed.strftime("%Y-%m-%d")
        )

    for match in DATE_PATTERNS[1].finditer(
        text
    ):
        year, month, day = match.groups()

        try:
            parsed = datetime(
                int(year),
                int(month),
                int(day),
            )
        except ValueError:
            continue

        dates.add(
            parsed.strftime("%Y-%m-%d")
        )

    return dates


# ==============================================================
# ACTIVITY FAMILIES
# ==============================================================

def activity_family(
    event_type: str,
    description: str,
) -> str:
    """
    Map classifier event types into broader activity families.

    This does not change the classifier's decision.
    It is purely an aggregation layer.
    """

    text = (
        description or ""
    ).lower()

    if event_type in {
        "ferc_information_request",
        "applicant_followup",
    }:
        return "information_exchange"

    if event_type == "third_party_comment":
        return "stakeholder_activity"

    if event_type == "environmental_comment":
        return "environmental_review"

    if event_type == "authorization_request":
        return "authorization"

    if event_type == "legal_activity":
        return "legal"

    if event_type == "delegated_order":
        return "delegated_authorization"

    if event_type == "context_dependent":
        return "construction_compliance"

    if event_type == "unknown":

        if any(
            phrase in text
            for phrase in (
                "landowner",
                "stakeholder list",
                "service list",
            )
        ):
            return "administrative"

        if any(
            phrase in text
            for phrase in (
                "environmental assessment",
                "environmental impact statement",
                "environmental issues",
                "scoping",
            )
        ):
            return "environmental_review"

        if any(
            phrase in text
            for phrase in (
                "notice of application",
                "application and establishing",
                "application and intervention",
            )
        ):
            return "authorization"

    return "other"


# ==============================================================
# EXPLICIT RELATIONSHIPS
# ==============================================================

def explicit_accession_relationship(
    left: ActivityRecord,
    right: ActivityRecord,
) -> bool:
    """
    True when either filing explicitly references the
    other's accession number.
    """

    return (
        right.accession
        in left.referenced_accessions
        or left.accession
        in right.referenced_accessions
    )


def _date_reference_language(
    description: str,
) -> bool:
    """
    Only treat a referenced date as a filing relationship
    when the description also contains relationship language.

    This prevents ordinary text such as "Submission Date"
    from being mistaken for a filing relationship.
    """

    text = (
        description or ""
    ).lower()

    return any(
        phrase in text
        for phrase in (
            "response to",
            "responds to",
            "reply to",
            "supplement to",
            "supplemental to",
            "in response to",
            "rehearing of",
            "motion to stay",
            "order dated",
            "notice dated",
            "request dated",
            "request of",
            "based on the",
        )
    )


def explicit_date_relationship(
    left: ActivityRecord,
    right: ActivityRecord,
) -> bool:
    """
    True when one filing explicitly refers to the date
    of the other filing and uses relationship language.
    """

    if (
        left.date is None
        or right.date is None
    ):
        return False

    left_date = left.date.strftime(
        "%Y-%m-%d"
    )

    right_date = right.date.strftime(
        "%Y-%m-%d"
    )

    left_text_has_relationship = (
        _date_reference_language(
            left.description
        )
    )

    right_text_has_relationship = (
        _date_reference_language(
            right.description
        )
    )

    return (
        (
            right_date
            in left.referenced_dates
            and left_text_has_relationship
        )
        or (
            left_date
            in right.referenced_dates
            and right_text_has_relationship
        )
    )


# ==============================================================
# ACTIVITY COMPATIBILITY
# ==============================================================

def compatible_activity(
    left: ActivityRecord,
    right: ActivityRecord,
) -> bool:
    """
    Determine whether two filings belong to compatible
    activity families.
    """

    left_family = activity_family(
        left.event_type,
        left.description,
    )

    right_family = activity_family(
        right.event_type,
        right.description,
    )

    if left_family == right_family:
        return True

    # FERC request + applicant response belong together.
    return {
        left.event_type,
        right.event_type,
    } == {
        "ferc_information_request",
        "applicant_followup",
    }


# ==============================================================
# TEMPORAL FALLBACK
# ==============================================================

def category_time_relationship(
    left: ActivityRecord,
    right: ActivityRecord,
) -> bool:
    """
    Conservative temporal fallback.

    Time proximity is category-specific.

    IMPORTANT:
        Stakeholder and legal activity are deliberately excluded
        here. Those are handled as bounded phases/discrete events
        rather than chained relationships.
    """

    if not compatible_activity(
        left,
        right,
    ):
        return False

    if (
        left.date is None
        or right.date is None
    ):
        return False

    gap = abs(
        (
            left.date
            - right.date
        ).days
    )

    family = activity_family(
        left.event_type,
        left.description,
    )

    if family == "information_exchange":
        return gap <= 7

    if family == "environmental_review":
        return gap <= 14

    if family == "construction_compliance":
        return gap <= 14

    if family == "authorization":
        return gap <= 7

    # Do not time-chain stakeholder comments.
    if family == "stakeholder_activity":
        return False

    # Legal activity should remain discrete unless explicitly linked.
    if family == "legal":
        return False

    # Delegated orders should remain discrete.
    if family == "delegated_authorization":
        return False

    # Administrative records should not chain broadly.
    if family == "administrative":
        return False

    return False


# ==============================================================
# UNION-FIND
# ==============================================================

class UnionFind:
    """
    Small disjoint-set implementation for relationship grouping.
    """

    def __init__(
        self,
        size: int,
    ):
        self.parent = list(
            range(size)
        )

    def find(
        self,
        item: int,
    ) -> int:

        while (
            self.parent[item]
            != item
        ):
            self.parent[item] = (
                self.parent[
                    self.parent[item]
                ]
            )

            item = self.parent[item]

        return item

    def union(
        self,
        left: int,
        right: int,
    ) -> None:

        left_root = self.find(
            left
        )

        right_root = self.find(
            right
        )

        if (
            left_root
            != right_root
        ):
            self.parent[
                right_root
            ] = left_root


# ==============================================================
# STAKEHOLDER PHASES
# ==============================================================

def build_stakeholder_phases(
    records: list[ActivityRecord],
    gap_days: int = 14,
) -> list[ActivityGroup]:
    """
    Build bounded stakeholder-comment phases.

    Stakeholder comments are NOT relationship-chained.
    Instead, comments form a phase only when consecutive
    comments are close enough in time.

    A 30-day gap or more starts a new phase.

    The default is deliberately conservative.
    """

    comments = [
        record
        for record in records
        if activity_family(
            record.event_type,
            record.description,
        )
        == "stakeholder_activity"
    ]

    comments.sort(
        key=lambda record: (
            record.date
            or datetime.min
        )
    )

    phases: list[
        ActivityGroup
    ] = []

    current: list[
        ActivityRecord
    ] = []

    for record in comments:

        if not current:
            current = [record]
            continue

        previous = current[-1]

        if (
            previous.date is not None
            and record.date is not None
            and (
                record.date
                - previous.date
            ).days
            <= gap_days
        ):
            current.append(record)

        else:
            phases.append(
                ActivityGroup(
                    project=current[0].project,
                    docket=current[0].docket,
                    activity_type="stakeholder_activity",
                    relationship="bounded_phase",
                    records=current,
                )
            )

            current = [record]

    if current:

        phases.append(
            ActivityGroup(
                project=current[0].project,
                docket=current[0].docket,
                activity_type="stakeholder_activity",
                relationship="bounded_phase",
                records=current,
            )
        )

    return phases


# ==============================================================
# SINGLE-EVENT GROUPS
# ==============================================================

def build_discrete_groups(
    records: list[ActivityRecord],
) -> list[ActivityGroup]:
    """
    Keep legal and delegated-authorization activity discrete
    unless an explicit accession/date relationship has been found.
    """

    discrete_families = {
        "legal",
        "delegated_authorization",
    }

    result = []

    for record in records:

        family = activity_family(
            record.event_type,
            record.description,
        )

        if family not in discrete_families:
            continue

        result.append(
            ActivityGroup(
                project=record.project,
                docket=record.docket,
                activity_type=family,
                relationship="discrete",
                records=[record],
            )
        )

    return result


# ==============================================================
# RELATIONSHIP GROUPING
# ==============================================================

def _relationship_signal(
    records: list[ActivityRecord],
) -> str:

    if len(records) <= 1:
        return "unlinked"

    for i in range(
        len(records)
    ):

        for j in range(
            i + 1,
            len(records),
        ):

            left = records[i]
            right = records[j]

            if explicit_accession_relationship(
                left,
                right,
            ):
                return "explicit_accession"

            if explicit_date_relationship(
                left,
                right,
            ):
                return "referenced_prior_date"

    return "category_plus_time"


def group_relationship_records(
    records: list[ActivityRecord],
) -> list[ActivityGroup]:
    """
    Group records using high-confidence relationships.

    Excluded from generic relationship chaining:
        - stakeholder activity
        - legal activity
        - delegated authorization

    Those are handled separately.
    """

    if not records:
        return []

    relationship_records = [
        record
        for record in records
        if activity_family(
            record.event_type,
            record.description,
        )
        not in {
            "stakeholder_activity",
            "legal",
            "delegated_authorization",
        }
    ]

    if not relationship_records:
        return []

    union_find = UnionFind(
        len(
            relationship_records
        )
    )

    for i in range(
        len(relationship_records)
    ):

        for j in range(
            i + 1,
            len(relationship_records),
        ):

            left = relationship_records[i]
            right = relationship_records[j]

            if (
                left.project
                != right.project
            ):
                continue

            # Strongest signal first.
            if explicit_accession_relationship(
                left,
                right,
            ):
                union_find.union(
                    i,
                    j,
                )
                continue

            if explicit_date_relationship(
                left,
                right,
            ):
                union_find.union(
                    i,
                    j,
                )
                continue

            if category_time_relationship(
                left,
                right,
            ):
                union_find.union(
                    i,
                    j,
                )

    grouped = defaultdict(
        list
    )

    for index in range(
        len(relationship_records)
    ):

        root = union_find.find(
            index
        )

        grouped[root].append(
            relationship_records[index]
        )

    result = []

    for items in grouped.values():

        items.sort(
            key=lambda item: (
                item.date
                or datetime.min
            )
        )

        families = defaultdict(
            int
        )

        for item in items:

            families[
                activity_family(
                    item.event_type,
                    item.description,
                )
            ] += 1

        activity_type = max(
            families,
            key=families.get,
        )

        result.append(
            ActivityGroup(
                project=items[0].project,
                docket=items[0].docket,
                activity_type=activity_type,
                relationship=_relationship_signal(
                    items
                ),
                records=items,
            )
        )

    result.sort(
        key=lambda group: (
            group.records[0].date
            or datetime.min
        )
    )

    return result


# ==============================================================
# PUBLIC API
# ==============================================================

def group_review_records(
    records: list[ActivityRecord],
    stakeholder_gap_days: int = 14,
) -> list[ActivityGroup]:
    """
    Public entry point.

    Produces user-oriented activity groups from REVIEW records.

    Grouping strategy:

        1. Explicit accession references
        2. Explicit prior-date references with relationship language
        3. Category-specific temporal grouping
        4. Bounded stakeholder phases
        5. Discrete legal/delegated events
    """

    if not records:
        return []

    stakeholder_groups = (
        build_stakeholder_phases(
            records,
            gap_days=stakeholder_gap_days,
        )
    )

    relationship_groups = (
        group_relationship_records(
            records
        )
    )

    discrete_groups = (
        build_discrete_groups(
            records
        )
    )

    groups = (
        relationship_groups
        + stakeholder_groups
        + discrete_groups
    )

    groups.sort(
        key=lambda group: (
            group.records[0].date
            or datetime.min
        )
    )

    return groups