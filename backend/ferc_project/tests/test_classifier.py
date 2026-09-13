from ferc_filter.classifier import classify_filing
from ferc_filter.models import Decision, FilingRecord


def test_known_high_signal_examples():
    cases = [
        ("Order Issuing Certificates and Approving Abandonment", Decision.ALERT),
        ("Environmental Assessment Report", Decision.ALERT),
        ("Request for Three-Year Extension of Time", Decision.ALERT),
    ]
    for description, expected in cases:
        assert classify_filing(FilingRecord(description=description)).decision == expected


def test_ambiguous_examples_are_reviewed():
    cases = [
        "Data Request",
        "Approval to proceed with the construction of Trains 4 and 5",
        "Field Inspection Report",
        "Request for Rehearing and Motion for Stay",
    ]
    for description in cases:
        assert classify_filing(FilingRecord(description=description)).decision == Decision.REVIEW


def test_recurring_noise_examples_are_suppressed():
    cases = [
        "Monthly Status Report No. 81",
        "Quarterly Construction Status Report",
        "Weekly Noise Data Report",
        "Motion to Intervene",
    ]
    for description in cases:
        assert classify_filing(FilingRecord(description=description)).decision == Decision.SUPPRESS
