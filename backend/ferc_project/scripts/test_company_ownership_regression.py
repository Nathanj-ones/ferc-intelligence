from ferc_filter.ferc_docket_discovery import match_company


companies = [
    {"id": "KMI", "name": "Kinder Morgan", "enabled": True},
    {"id": "TRGP", "name": "Targa Resources", "enabled": True},
    {"id": "WMB", "name": "Williams", "enabled": True},
    {"id": "OKE", "name": "ONEOK", "enabled": True},
    {"id": "LNG", "name": "Cheniere Energy", "enabled": True},
    {"id": "NEXT", "name": "NextDecade", "enabled": True},
]

identity_map = {
    "KMI": [
        "Tennessee Gas Pipeline Company, L.L.C.",
        "Southern Natural Gas Company, L.L.C.",
        "Natural Gas Pipeline Company of America LLC",
    ],
    "TRGP": ["Targa Gas Marketing LLC"],
    "WMB": [
        "Transcontinental Gas Pipe Line Company, LLC",
        "Northwest Pipeline LLC",
    ],
    "OKE": ["Sabine Pipe Line LLC"],
    "LNG": [
        "Corpus Christi Liquefaction, LLC",
        "Cheniere Creole Trail Pipeline, L.P.",
        "Sabine Pass Liquefaction, LLC",
    ],
    "NEXT": ["Rio Grande LNG, LLC"],
}


def assert_owner(applicant, expected):
    company_id, reason, confidence = match_company(
        applicant,
        companies,
        identity_map,
    )
    assert company_id == expected, (
        f"{applicant!r}: expected {expected!r}, got "
        f"{company_id!r} ({reason}, {confidence})"
    )
    return reason, confidence


reason, confidence = assert_owner(
    "Transcontinental Gas Pipe Line Company, LLC",
    "WMB",
)
assert confidence == "high"
print("PASS | Transco maps exclusively to WMB")

reason, confidence = assert_owner(
    "Tennessee Gas Pipeline Company, L.L.C.",
    "KMI",
)
assert confidence == "high"
print("PASS | Tennessee Gas Pipeline maps exclusively to KMI")

reason, confidence = assert_owner(
    "Rio Grande LNG, LLC",
    "NEXT",
)
assert confidence == "high"
print("PASS | Rio Grande LNG maps exclusively to NEXT")

company_id, reason, confidence = match_company(
    "Completely Unknown Pipeline LLC",
    companies,
    identity_map,
)
assert company_id is None
print("PASS | Unknown applicant remains unmatched")

# Explicit regression for the false-promotion incident:
# CP26-576 applicant is Transco, so it may only belong to WMB.
owners = []
for company in companies:
    company_id, _, _ = match_company(
        "Transcontinental Gas Pipe Line Company, LLC",
        [company],
        {
            company["id"]: identity_map.get(company["id"], [])
        },
    )
    if company_id:
        owners.append(company_id)

assert owners == ["WMB"], f"CP26-576 ownership leaked to: {owners}"
print("PASS | CP26-576 applicant resolves to WMB only")

print()
print("ALL COMPANY OWNERSHIP REGRESSION v0.1 TESTS PASSED")
