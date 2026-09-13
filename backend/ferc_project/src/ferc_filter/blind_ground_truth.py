"""Manually audited blind-test cases.

These are evaluation labels, not production rules.
They must never be used by the classifier itself.
"""

BLIND_GROUND_TRUTH = [
    {"accession": "20241029-5076", "expected": "CONTEXT"},
    {"accession": "20241112-3035", "expected": "SUPPRESS"},
    {"accession": "20241113-3035", "expected": "SUPPRESS"},
    {"accession": "20241114-3055", "expected": "SUPPRESS"},
    {"accession": "20241203-5159", "expected": "CONTEXT"},
    {"accession": "20241203-5247", "expected": "SUPPRESS"},
    {"accession": "20250113-5003", "expected": "CONTEXT"},
    {"accession": "20250131-5401", "expected": "CONTEXT"},
    {"accession": "20250328-5273", "expected": "SUPPRESS"},
    {"accession": "20250331-5559", "expected": "CONTEXT"},
    {"accession": "20250624-5595", "expected": "CONTEXT"},
    {"accession": "20250627-5148", "expected": "CONTEXT"},
    {"accession": "20250709-3038", "expected": "CONTEXT"},
    {"accession": "20250714-5083", "expected": "CONTEXT"},
    {"accession": "20250714-5164", "expected": "CONTEXT"},
    {"accession": "20250930-5021", "expected": "CONTEXT"},
    {"accession": "20250930-5023", "expected": "CONTEXT"},
    {"accession": "20251031-3000", "expected": "MATERIAL"},
    {"accession": "20251201-5707", "expected": "CONTEXT"},
    {"accession": "20251202-3046", "expected": "CONTEXT"},
    {"accession": "20251209-0013", "expected": "CONTEXT"},
    {"accession": "20260122-5121", "expected": "CONTEXT"},
    {"accession": "20260129-3076", "expected": "MATERIAL"},
    {"accession": "20260225-3042", "expected": "MATERIAL"},
    {"accession": "20260501-5308", "expected": "CONTEXT"},
    {"accession": "20260504-0020", "expected": "CONTEXT"},
    {"accession": "20260723-3019", "expected": "CONTEXT"},
    {"accession": "20260817-3020", "expected": "SUPPRESS"},
]