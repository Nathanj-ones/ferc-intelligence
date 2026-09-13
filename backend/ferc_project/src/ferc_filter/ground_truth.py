"""Small manually validated ground-truth set for classifier testing.

These examples are NOT production rules.

They exist only to evaluate whether general classifier changes
continue to identify known material and routine events correctly.
"""

GROUND_TRUTH = [
    # ------------------------------------------------------------
    # Rio Grande LNG
    # ------------------------------------------------------------

    {
        "accession": "20260220-5151",
        "project": "Rio Grande LNG",
        "expected_decision": "REVIEW",
        "event_type": "authorization_request",
        "reason": (
            "Applicant requests major construction authorization; "
            "no FERC approval has yet occurred."
        ),
    },
    {
        "accession": "20260312-3060",
        "project": "Rio Grande LNG",
        "expected_decision": "ALERT",
        "event_type": "regulatory_decision",
        "reason": "FERC grants major construction authorization.",
    },
    {
        "accession": "20260424-5284",
        "project": "Rio Grande LNG",
        "expected_decision": "REVIEW",
        "event_type": "unknown",
        "reason": (
            "Applicant requests a material extension of time; "
            "the requested schedule change has not yet been granted."
        ),
    },
    {
        "accession": "20260526-3043",
        "project": "Rio Grande LNG",
        "expected_decision": "ALERT",
        "event_type": "regulatory_decision",
        "reason": "FERC grants material extension of project completion deadline.",
    },
    {
        "accession": "20260102-5155",
        "project": "Rio Grande LNG",
        "expected_decision": "SUPPRESS",
        "event_type": "routine_reporting",
        "reason": "Recurring weekly noise reporting.",
    },
    {
        "accession": "20260115-5107",
        "project": "Rio Grande LNG",
        "expected_decision": "SUPPRESS",
        "event_type": "routine_reporting",
        "reason": "Recurring monthly status reporting.",
    },

    # ------------------------------------------------------------
    # Mississippi Crossing
    # ------------------------------------------------------------

    {
        "accession": "20260130-3001",
        "project": "Mississippi Crossing",
        "expected_decision": "ALERT",
        "event_type": "environmental_milestone",
        "reason": "FERC issues Draft Environmental Impact Statement.",
    },
    {
        "accession": "20260626-3002",
        "project": "Mississippi Crossing",
        "expected_decision": "ALERT",
        "event_type": "environmental_milestone",
        "reason": "FERC issues Final Environmental Impact Statement.",
    },
    {
        "accession": "20260731-3085",
        "project": "Mississippi Crossing",
        "expected_decision": "ALERT",
        "event_type": "regulatory_decision",
        "reason": "FERC issues certificate order.",
    },
    {
        "accession": "20260320-5168",
        "project": "Mississippi Crossing",
        "expected_decision": "REVIEW",
        "event_type": "environmental_comment",
        "reason": "Government agency comments on Draft EIS.",
    },
    {
        "accession": "20260323-5193",
        "project": "Mississippi Crossing",
        "expected_decision": "REVIEW",
        "event_type": "environmental_comment",
        "reason": "Third-party comments on Draft EIS.",
    },
    {
        "accession": "20260312-4000",
        "project": "Mississippi Crossing",
        "expected_decision": "SUPPRESS",
        "event_type": "third_party_procedural_activity",
        "reason": "Public scoping meeting transcript.",
    },

    # ------------------------------------------------------------
    # Lea County Expansion
    # ------------------------------------------------------------

    {
        "accession": "20240419-5284",
        "project": "Lea County Expansion",
        "expected_decision": "REVIEW",
        "event_type": "authorization_request",
        "reason": "Prior Notice request to construct and operate.",
    },
    {
        "accession": "20240628-3000",
        "project": "Lea County Expansion",
        "expected_decision": "ALERT",
        "event_type": "environmental_milestone",
        "reason": "FERC issues Environmental Assessment.",
    },
    {
        "accession": "20240815-5072",
        "project": "Lea County Expansion",
        "expected_decision": "SUPPRESS",
        "event_type": "routine_reporting",
        "reason": "Recurring weekly construction status report.",
    },
]