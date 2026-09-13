from dataclasses import dataclass

from ferc_filter.ferc_identity_expansion import expand_identities


@dataclass
class Filing:
    accession: str
    docket: str
    description: str
    applicant: str


DATA = {
    "Parent Energy": [
        Filing("1", "", "Parent filing", "Operating Entity LLC"),
        Filing("2", "", "Parent filing", "Operating Entity LLC"),
    ],
    "Operating Entity LLC": [
        Filing("3", "CP26-100", "Project filing", "Pipeline Subsidiary LLC"),
        Filing("4", "CP26-100", "Project filing", "Pipeline Subsidiary LLC"),
    ],
    "Pipeline Subsidiary LLC": [
        Filing("5", "CP26-100", "Application for project", "Pipeline Subsidiary LLC"),
    ],
}


def search(identity):
    return DATA.get(identity, [])


result = expand_identities(
    ["Parent Energy"],
    search,
    minimum_filings=2,
    max_rounds=4,
)

assert "Operating Entity LLC" in result.identities
assert "Pipeline Subsidiary LLC" in result.identities
assert any(f.docket == "CP26-100" for f in result.filings)
print("PASS | Identity expansion traverses recurring FERC entities")
print("PASS | Downstream project docket becomes discoverable")

singleton_data = {
    "Parent Energy": [
        Filing("1", "", "One filing", "One-Off Entity LLC"),
    ]
}

result = expand_identities(
    ["Parent Energy"],
    lambda identity: singleton_data.get(identity, []),
    minimum_filings=2,
)
assert "One-Off Entity LLC" not in result.identities
print("PASS | Singleton identity does not expand graph")

print()
print("ALL ITERATIVE FERC IDENTITY EXPANSION v0.1 TESTS PASSED")
