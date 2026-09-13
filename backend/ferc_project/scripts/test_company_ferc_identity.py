import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ferc_filter.company_ferc_identity import aliases_for_company


with TemporaryDirectory() as tmp:
    path = Path(tmp) / "identities.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "company_ferc_identities_v1",
                "companies": {
                    "TEST": {
                        "ferc_entities": [
                            "Example Pipeline Company, LLC",
                            "Example Storage LLC",
                            "Example Pipeline Company, LLC"
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    aliases = aliases_for_company(
        "TEST",
        company_name="Example Energy",
        path=path,
    )

assert aliases == [
    "Example Energy",
    "Example Pipeline Company, LLC",
    "Example Storage LLC",
]
print("PASS | Parent company included in FERC identity search")
print("PASS | Multiple FERC operating entities supported")
print("PASS | Duplicate entity names removed")
print()
print("ALL COMPANY FERC IDENTITY v0.1 TESTS PASSED")
