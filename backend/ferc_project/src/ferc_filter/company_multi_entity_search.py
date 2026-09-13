from __future__ import annotations

from collections import OrderedDict

from ferc_filter.company_ferc_identity import aliases_for_company
from ferc_filter.company_project_discovery import FERCDiscoveryClient


def search_company_identities(
    client: FERCDiscoveryClient,
    company_id: str,
    company_name: str,
    start_date: str,
    end_date: str,
):
    """
    Search the parent name plus all learned FERC identities, then deduplicate
    filings by accession+docket+description.
    """
    aliases = aliases_for_company(
        company_id,
        company_name=company_name,
    )

    deduped = OrderedDict()

    for alias in aliases:
        hits = client.search_company(
            company_name=alias,
            start_date=start_date,
            end_date=end_date,
        )

        for hit in hits:
            key = (
                hit.accession,
                hit.docket,
                hit.description,
            )
            deduped.setdefault(key, hit)

    return list(deduped.values()), aliases
