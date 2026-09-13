from ferc_filter.company_ferc_identity_learner import (
    is_plausible_operating_entity,
    learn_entities,
)

assert is_plausible_operating_entity(
    "Transcontinental Gas Pipe Line Company, LLC"
)
assert is_plausible_operating_entity("Targa Gas Marketing LLC")
print("PASS | Plausible legal operating entities accepted")

for bad in [
    "Office of the Secretary, FERC",
    "Individual No Affiliation",
    "Justus CPP, Black River Guardians, Citizens of Williamsburg",
    "Citizens of Williamsburg",
]:
    assert not is_plausible_operating_entity(bad)

print("PASS | FERC offices and public participants rejected")
print("PASS | Multi-party commenter strings rejected")

filings = [
    {"applicant": "Example Pipeline Company, LLC"},
    {"applicant": "Example Pipeline Company, LLC"},
    {"applicant": "Office of the Secretary, FERC"},
    {"applicant": "Office of the Secretary, FERC"},
    {"applicant": "Individual No Affiliation"},
]

learned = learn_entities(filings, minimum_filings=2)
eligible = [item["entity"] for item in learned if item["eligible"]]
assert eligible == ["Example Pipeline Company, LLC"]
print("PASS | Learner only promotes recurring operating entities")

print()
print("ALL COMPANY FERC IDENTITY LEARNER v0.2 TESTS PASSED")
