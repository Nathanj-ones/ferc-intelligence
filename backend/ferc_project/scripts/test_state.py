from datetime import datetime, timezone

from ferc_filter.state import MonitorState


state = MonitorState(
    "data/test_ferc_monitor.db"
)


accession = "TEST-0001"

timestamp = datetime.now(
    timezone.utc
).isoformat()


print("Database created.")


print(
    "Initially processed:",
    state.has_filing(accession),
)


state.save_enrichment(
    accession,
    {
        "category": "Issuance",
        "classTypes": [
            {
                "documentClass": "Order/Opinion",
                "documentType": "Delegated Order",
            }
        ],
    },
    timestamp,
)


cached = state.get_enrichment(
    accession
)

print(
    "Cached enrichment:",
    cached,
)


state.save_filing(
    accession=accession,
    docket="CP00-000",
    project_name="Test Project",
    filed_date="2026-09-07",
    category="Issuance",
    description="Test filing",
    document_class="Order/Opinion",
    document_type="Delegated Order",
    decision="REVIEW",
    event_type="test",
    processed_at=timestamp,
    raw_record={
        "accession_no": accession,
    },
    enriched_record={
        "accession_no": accession,
        "document_class": "Order/Opinion",
        "document_type": "Delegated Order",
    },
)


print(
    "After save:",
    state.has_filing(accession),
)

print(
    "Stored filings:",
    state.count_filings(),
)

print(
    "Cached enrichments:",
    state.count_cached_enrichments(),
)