from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Callable, Iterable, Any


@dataclass
class IdentityExpansionResult:
    filings: list[Any]
    identities: list[str]
    learned: list[str]
    rounds: int


def _dedupe_filings(filings: Iterable[Any]) -> list[Any]:
    deduped = OrderedDict()
    for filing in filings:
        key = (
            getattr(filing, "accession", ""),
            getattr(filing, "docket", ""),
            getattr(filing, "description", ""),
        )
        deduped.setdefault(key, filing)
    return list(deduped.values())


def _recurring_applicants(
    filings: Iterable[Any],
    minimum_filings: int,
) -> list[str]:
    counts = Counter(
        str(getattr(filing, "applicant", "") or "").strip()
        for filing in filings
        if str(getattr(filing, "applicant", "") or "").strip()
    )
    return [
        entity
        for entity, count in counts.most_common()
        if count >= minimum_filings
    ]


def expand_identities(
    seed_identities: list[str],
    search: Callable[[str], list[Any]],
    *,
    minimum_filings: int = 2,
    max_rounds: int = 3,
    max_identities: int = 20,
) -> IdentityExpansionResult:
    """
    Iteratively search known identities and learn recurring applicant names.

    Guardrails:
    - recurring applicant required
    - bounded rounds
    - bounded total identities
    - case-insensitive dedupe
    """
    identities = []
    seen = set()

    for seed in seed_identities:
        value = str(seed or "").strip()
        key = value.casefold()
        if value and key not in seen:
            identities.append(value)
            seen.add(key)

    learned = []
    all_filings = []
    searched = set()
    rounds = 0

    while rounds < max_rounds:
        rounds += 1
        round_filings = []

        for identity in list(identities):
            key = identity.casefold()
            if key in searched:
                continue
            searched.add(key)
            round_filings.extend(search(identity))

        if not round_filings:
            break

        all_filings.extend(round_filings)
        all_filings = _dedupe_filings(all_filings)

        new_entities = []
        for entity in _recurring_applicants(
            all_filings,
            minimum_filings,
        ):
            key = entity.casefold()
            if key in seen:
                continue
            if len(identities) + len(new_entities) >= max_identities:
                break
            seen.add(key)
            new_entities.append(entity)

        if not new_entities:
            break

        identities.extend(new_entities)
        learned.extend(new_entities)

    return IdentityExpansionResult(
        filings=_dedupe_filings(all_filings),
        identities=identities,
        learned=learned,
        rounds=rounds,
    )
