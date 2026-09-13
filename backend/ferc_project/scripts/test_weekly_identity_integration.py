from ferc_filter.company_ferc_identity_learner import learn_entities


filings = [
    {"applicant": "Operating Pipeline LLC"},
    {"applicant": "Operating Pipeline LLC"},
    {"applicant": "One-Off Commenter"},
]

learned = learn_entities(filings, minimum_filings=2)

eligible = [
    item["entity"]
    for item in learned
    if item["eligible"]
]

assert eligible == ["Operating Pipeline LLC"]
print("PASS | Weekly bootstrap learns recurring FERC entity")
print("PASS | One-off affiliation is not trusted")
print()
print("ALL WEEKLY IDENTITY INTEGRATION v0.1 TESTS PASSED")
