#!/usr/bin/env python3
"""Build hash-bound final implementation records from a completed Build A.

This program is deliberately a *consumer* of a finished candidate.  It never
runs adapters, migrates a database, fetches a source, or rewrites an input.  It
opens SQLite read-only, independently verifies the published export generation,
checks the final audit/draft populations, binds test claims to complete logs,
and publishes the resulting records as one content-addressed collection.

The test-run manifest is small but intentionally strict::

  {
    "schema": "ferc-final-test-run-manifest-v2",
    "issue_test_evidence": {"A01": ["tests.test_module.Case.test_name"]},
    "exception_test_evidence": {"blk-...": ["tests.test_module.Case.test_name"]},
    "runs": [{
      "run_id": "python314-complete",
      "kind": "complete_suite",
      "acceptance_role": "acceptance",
      "complete_suite": true,
      "log": "full_build_a_python314.log",
      "bytes": 1234,
      "sha256": "...",
      "command": ["python3", "-m", "unittest", "discover", "-v"],
      "interpreter": "CPython 3.14.7",
      "exit_code": 0,
      "counts": {"collected": 400, "executed": 400, "pass": 400,
                 "fail": 0, "error": 0, "skip": 0, "xfail": 0,
                 "xpass": 0},
      "skip_details": [],
      "xfail_details": [],
    }]
  }

Paths in ``log`` are relative to ``--logs-dir``.  Historical defective-baseline
runs may use ``acceptance_role=historical_negative_control`` and must actually
show a non-zero/failing result.  Every test identifier in either evidence map
must occur as a successful case in a hash-bound verbose acceptance log; prose
``targeted_test`` labels are specifications and are never silently treated as
executed tests.  A skipped/expected-failure required capability is refused.
"""

from __future__ import annotations

import argparse
import collections
import copy
import csv
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from implementation.unittest_log_parser import (
        UnittestLogRefused, parse_unittest_log as _parse_shared_unittest_log)
except ModuleNotFoundError:  # Direct script execution from implementation/.
    from unittest_log_parser import (  # type: ignore
        UnittestLogRefused, parse_unittest_log as _parse_shared_unittest_log)


LEDGER_ROWS = 64
EXCEPTION_ROWS = 34
INPUT_ROWS = 13
ADDITIONAL_INPUT_ROWS = 5
RELATED_FINDING_IDS = {
    "R23-CAPACITY-FILER-IDENTITY-COLLISION",
    "R24-CPYTHON39-VERBOSE-TEST-ID",
    "R25-ELIBRARY-AS-OF-FUTURE-OCCURRENCE-LEAK",
    "R26-ELIBRARY-HIDDEN-PROCEEDING-SEED",
    "R27-ELIBRARY-RUN-INPUT-IDENTITY",
    "R28-LNG-ROUTING-AUTHORITY-ASSOCIATIONS",
    "R29-ELIBRARY-SIBLING-FILER-CONTAMINATION",
    "R30-RETRIEVAL-UNIT-PARTIAL-ROW-LEAKAGE",
    "R31-REDUNDANT-INPUT-SNAPSHOT-PACKAGING-MISMATCH",
    "R32-PENDING-TEST-SPEC-PAYLOAD-LEAK",
    "R33-UNDECLARED-ELIBRARY-REPLAY-DEPENDENCIES",
    "R34-STALE-PROVISIONAL-PAYLOAD-LEAKS",
    "R35-MUTABLE-CACHE-PATH-FOR-HISTORICAL-IDENTITY",
    "R36-NAMED-LNG-FILER-COVERAGE-GATE",
    "R37-STALE-TAXONOMY-CACHE-FREEZE",
    "R38-IOC-LINEAGE-VALIDATOR-SCOPE-AND-EXTENDED-POINT-CODE",
    "R39-BLANK-IOC-POINT-CODE-PROMOTION",
    "R40-IOC-ZERO-POINT-CENSUS-CANDIDATE-COUNT",
    "R41-IOC-INTEGRATION-FIXTURE-FK-LIFECYCLE",
    "R42-A13-LAST-GOOD-ATTEMPT-RECEIPT-ASSERTION",
    "R43-FINAL-LEDGER-SUBSET-HIDDEN-W5-DEPENDENCY",
    "R44-CPYTHON39-CSV-C0-CONTROL-PORTABILITY",
    "R45-BUILD-B-PENDING-INSTRUCTION-CORRUPTS-EXTRACTION",
    "R46-CANDIDATE-MATERIALIZER-RETAINS-HIDDEN-SCRATCH",
    "R47-FORM549D-HIDDEN-CANDIDATE-WRITE",
    "R48-BUILD-B-REPLAY-LEDGER-OBJECT-ORDER",
    "R49-BUILD-B-ACCEPTANCE-MIRROR-MISSING-DEPENDENCY",
    "R50-ELIBRARY-CAPTURE-TIME-DETERMINISM",
    "R51-RUN-ENVELOPE-PUBLICATION-DETERMINISM",
    "R52-BUILD-B-REFUSAL-COMPARISON-EVIDENCE",
    "R53-SHARED-OCCURRENCE-LISTING-TIMESTAMP-CONVERGENCE",
    "R54-FULL-RELEASE-INSTRUCTION-DEFER-GATE",
    "R55-PLACEHOLDER-MARKER-DOMAIN-TERM-COLLISION",
}
RELATED_FINDING_ROWS = len(RELATED_FINDING_IDS)
ADDITIONAL_INPUT_IDS = {
    "elibrary:20260226-5162",
    "elibrary-search:IS26-587:44205e596cb7cb8201a3bcfe63eaa6d0178a6c3a97c6e35e0a6d126dd1686db1",
    "elibrary-search:RP26-1091:2280cc641139baafaf01d5f47c1cf7b404c6e2938c8dbe7fd84f98eda7205005",
    "elibrary-search:RP26-981:15d2a70f0df065e0b787634f1d90a289f55e63f857b76c94e04b93b28445c7ee",
    "elibrary-dependency-capture:build-a-v4:20260910",
}
ADDITIONAL_INPUT_ACCEPTANCE_RESULTS = {
    "accepted_for_candidate",
    "accepted_as_repair_evidence_not_execution_prerequisite",
}
CORE_REQUIREMENTS = {"REQUIRED", "CONDITIONALLY_REQUIRED"}
FUTURE_SLOT = "future_not_yet_due"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ACCESSION = re.compile(r"^[0-9]{8}-[0-9]{4}$")
PLACEHOLDER = re.compile(
    r"(?:\bTBD\b|\bTODO\b|\bFIXME\b|\bPLACEHOLDER\b|"
    r"pending_build_a|pending Build A|fixed_pending_build|unclaimed\s*$)", re.I)
BASIS_PLACEHOLDER = re.compile(
    r"^\s*(?:(?:TBD|TODO|FIXME|PLACEHOLDER)\b.*|pending_build_a|"
    r"pending Build A|fixed_pending_build|unclaimed)\s*$", re.I)
UNDECODABLE_SCOPE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
MIN_COMPLETE_SUITE_CASES = 100
DISPLAY_CONTRACT = {
    "operating_margin_pct": ("percent", 1.0),
    "liq_operating_margin_pct": ("percent", 1.0),
    "cap_peak_day_ratio": ("percent", 1.0),
    "ioc_top5_shipper_concentration": ("percent", 1.0),
    "ioc_affiliate_share": ("percent", 1.0),
    "ioc_identity_coverage": ("percent", 1.0),
    "i311_firm_share": ("percent", 1.0),
    "i311_top5_shipper_share": ("percent", 1.0),
    "i311_affiliate_activity": ("percent", 1.0),
    "p700_wacc": ("percent", 100.0),
    "p700_capital_structure_debt": ("percent", 100.0),
    "p700_capital_structure_equity": ("percent", 100.0),
    "p700_revenue_to_cost_ratio": ("ratio", 1.0),
    "liq_revenue_per_barrel": ("USD/bbl", 1.0),
    "liq_opex_per_barrel": ("USD/bbl", 1.0),
    "liq_oil_pipeline_index_factor": ("multiplier", 1.0),
    "liq_oil_pipeline_index_change": ("percent", 100.0),
}
LNG_ROUTING_SEED = pathlib.PurePosixPath(
    "inputs/official_ferc/elibrary/lng_facility_sources_v1.csv")
LNG_ROUTING_SEED_BYTES = 36275
LNG_ROUTING_SEED_SHA256 = \
    "1d3bce4f7d9ecc0b0c40ad5d93f6aaf22b8eea2dcea0eb9da0fe64e285cc2e00"
LNG_AUTHORITY_ROLE = "official_lng_facility_authority_v1"
LNG_AUTHORITY_PAIR_SHA256 = \
    "b9a71bcfd6d14c71c9ee8d4b3a35584defa9fffae530904ea9d4da8e69e67216"
LNG_SHARED_METRICS = {
    "lng_operational_report", "lng_inspection", "lng_status_operating"}
LNG_NAMED_FILER_METRICS = {
    "lng_liquefaction_capacity", "lng_regas_sendout_capacity",
    "lng_storage_capacity", "lng_status_requested",
    "lng_status_commissioning", "lng_status_authorised",
    "lng_status_operating", "lng_operational_report", "lng_inspection",
    "lng_material_order",
}
FILING_ENTITY_ROLES = {
    "source_entity", "named_filer", "commission_docket_subject",
    "facility_subject", "compatibility_anchor",
}

# Exact observations which exposed the distinction between a shared-facility
# relation and an occurrence-specific named legal filer.  These are frozen
# Build-A acceptance records, not selectors used by the production adapter.
R36_NAMED_FILER_OBSERVATIONS = {
    "obs-3f6a3dceba0be24e00757ab2dfe9e01b": {
        "filing_id": "20161012-3036",
        "entity_key": "NO-FERC-CID:Sabine Pass Liquefaction, LLC",
        "compatibility_entity": "NO-FERC-CID:Sabine Pass LNG, L.P.",
        "metric_id": "lng_status_authorised", "facility_key": "sabine-pass",
        "common_dockets": ("CP11-72",), "evidence_kind": "indexed_occurrence",
        "classification": "historical_or_noncurrent_occurrence_quality",
        "current_version": True, "unit_contract_ok": True, "unit_gate": "",
    },
    "obs-46bac3c37b38fc21dc86d98ff48ee868": {
        "filing_id": "20241121-3047",
        "entity_key": "NO-FERC-CID:Elba Liquefaction Company, L.L.C.",
        "compatibility_entity": "C000039",
        "metric_id": "lng_liquefaction_capacity", "facility_key": "elba",
        "common_dockets": ("CP23-375",), "evidence_kind": "filed_occurrence",
        "classification": "current_occurrence_quality",
        "current_version": True, "unit_contract_ok": True, "unit_gate": "",
    },
    "obs-5901fa2a4130d36afd0ff21200404a33": {
        "filing_id": "20241122-3097",
        "entity_key": "NO-FERC-CID:Elba Liquefaction Company, L.L.C.",
        "compatibility_entity": "C000039", "metric_id": "lng_material_order",
        "facility_key": "elba", "common_dockets": ("CP23-375",),
        "evidence_kind": "indexed_occurrence",
        "classification": "current_occurrence_quality",
        "current_version": True, "unit_contract_ok": True, "unit_gate": "",
    },
    "obs-9a232e39e00dcb2d4db28be69b8ed199": {
        "filing_id": "20241121-3047",
        "entity_key": "NO-FERC-CID:Elba Liquefaction Company, L.L.C.",
        "compatibility_entity": "C000039",
        "metric_id": "lng_liquefaction_capacity", "facility_key": "elba",
        "common_dockets": ("CP23-375",), "evidence_kind": "filed_occurrence",
        "classification": "historical_or_noncurrent_occurrence_quality",
        "current_version": False, "unit_contract_ok": False,
        "unit_gate": "superseded_version",
    },
    "obs-dcd1fb5a7fe5a9cfcb3dd69a07e64caa": {
        "filing_id": "20140310-5158",
        "entity_key": "NO-FERC-CID:Elba Liquefaction Company, L.L.C.",
        "compatibility_entity": "C000039", "metric_id": "lng_status_requested",
        "facility_key": "elba", "common_dockets": ("CP14-103",),
        "evidence_kind": "indexed_occurrence",
        "classification": "historical_or_noncurrent_occurrence_quality",
        "current_version": True, "unit_contract_ok": True, "unit_gate": "",
    },
    "obs-dfb107168e7dbd978a6daaa98d00cdb1": {
        "filing_id": "20110131-5063",
        "entity_key": "NO-FERC-CID:Sabine Pass Liquefaction, LLC",
        "compatibility_entity": "NO-FERC-CID:Sabine Pass LNG, L.P.",
        "metric_id": "lng_status_requested", "facility_key": "sabine-pass",
        "common_dockets": ("CP11-72",), "evidence_kind": "indexed_occurrence",
        "classification": "historical_or_noncurrent_occurrence_quality",
        "current_version": True, "unit_contract_ok": True, "unit_gate": "",
    },
    "obs-e00c7ed8756114bf836a0e1086366774": {
        "filing_id": "20230428-5621",
        "entity_key": "NO-FERC-CID:Elba Liquefaction Company, L.L.C.",
        "compatibility_entity": "C000039", "metric_id": "lng_status_requested",
        "facility_key": "elba", "common_dockets": ("CP14-103", "CP23-375"),
        "evidence_kind": "indexed_occurrence",
        "classification": "historical_or_noncurrent_occurrence_quality",
        "current_version": True, "unit_contract_ok": True, "unit_gate": "",
    },
    "obs-e7409e9a31f26e658e839fd1d41cf67e": {
        "filing_id": "20241121-3047",
        "entity_key": "NO-FERC-CID:Elba Liquefaction Company, L.L.C.",
        "compatibility_entity": "C000039", "metric_id": "lng_material_order",
        "facility_key": "elba", "common_dockets": ("CP23-375",),
        "evidence_kind": "filed_occurrence",
        "classification": "current_occurrence_quality",
        "current_version": True, "unit_contract_ok": True, "unit_gate": "",
    },
}
R36_ROUTE_PAIRS = {
    (row["entity_key"], row["metric_id"])
    for row in R36_NAMED_FILER_OBSERVATIONS.values()
}
IOC_VALIDATION_CHECK = "IOC aggregates redraw from their persisted source populations"
R38_OBSERVATION_ID = "obs-65199973861961ad89dda4a2f43be6f5"
R38_POPULATION_ID = "pop-4637ebf709b91d5a98cb86dffd9945d9"
R38_FILING_ID = "20250808-5173"
R38_SOURCE_FACT_ID = "P00005"
R38_MEMBER_DIGEST = \
    "f4010d74ac6a8849ae3b8fd3f7dbab525cf2919bc5e0d6cae33ac6d6b0d5d336"
R39_INVALID_OBSERVATION_ID = "obs-b88fa75ca303069090bff41279ed43d7"
R39_INVALID_POPULATION_ID = "pop-7351c8563a7d53f0317eab2a790376f5"
R39_HEAD_OBSERVATION_ID = "obs-afaef0b47b3e803331ec404a0c7d6469"
R39_HEAD_POPULATION_ID = "pop-a434fa603aab5f5b5334f75104c094b2"
R39_FILING_ID = "20251001-5149"
R39_BLANK_FACT_IDS = {"P01406", "P01457"}
R40_ENTITY_KEY = "C001012"
R40_ZERO_POINT_ROWS = {
    "20240102-5176": ("obs-4952218c315d35d13923a19928873a83",
                      "pop-bb8b4359313d73868e270683227f4d68"),
    "20240401-5209": ("obs-3dc9e7eae0b8008049165c259339ab49",
                      "pop-eafab6ea046d9e8f6ec28f12b4adc012"),
    "20240701-5063": ("obs-5857c0d81b6cb9521ba5903b5db99dd8",
                      "pop-2f598b9085ae87cce49c094dfa05521e"),
    "20241001-5081": ("obs-63b68edeb06f8bf92458b22d2a5f3714",
                      "pop-88a0e1954b9c67673b9d2c20e2c79ae8"),
    "20250102-5082": ("obs-ede690fc9f63f1783e6ca01662170c29",
                      "pop-a2c6f27116184d8ebd7e7de3d61676ff"),
    "20250401-5104": ("obs-f2df879fa747f7ad19a16d1616be6a68",
                      "pop-0e9ef18bcb1a2b7ee2a035373da36d4c"),
    "20250701-5084": ("obs-df8b7ff849d23fa438e54956688dcee9",
                      "pop-43500ebb4d38040fc83ae0e781e7b721"),
    "20251001-5072": ("obs-6b2945622d0bb9d54a8674605fbac5ed",
                      "pop-97d98c1550b448e38ad982e8e55f2a87"),
    "20260102-5094": ("obs-4ff7e05520dfb22f9254e998b947fdd2",
                      "pop-589dcaa239777db28161a515d99f3001"),
    "20260331-5115": ("obs-de4107482f3e49981de6facee9157cae",
                      "pop-b8eeb1e4051dcc884fbb831b212cf9c3"),
    "20260701-5148": ("obs-dcb182f69c8a58abd3e3b63982dd78f4",
                      "pop-fd8eb70a925b2e3360a6d9758785a9bc"),
}
W5_HORIZON_ATTACHED_UNIT_FACTS = {
    "dfact-c1edd827514880ae2657ea04": "20250227-5068",
    "dfact-619d3e5441f99fbffdf04e8c": "20260223-5045",
}
TAXONOMY_PIN_FORMS = {
    "Form 2", "Form 2A", "Form 3Q Gas", "Form 6", "Form 6Q"}
TAXONOMY_PIN_YEARS = {2024, 2025, 2026}

# The FULL package retains this official response at its content-addressed
# cache path and omits only the byte-identical capture-tree alias.  Keep this
# declaration independent of run.py/build_release.py: the finalizer must verify
# the candidate it is certifying, rather than trust or import its implementation
# of the same control.
REDUNDANT_INPUT_ALIAS = {
    "alias_path": (
        "inputs/official_ferc_recovery/individual_responses/20231229-5212/"
        "07_F19520DA-2360-C302-8619-8CB6CB100000.body"),
    "cache_path": (
        "objects/da/"
        "dae325d243bb7ecb3ac6db58c7163e197ffcc593b73e583330372f0fe03c400b"),
    "sha256": "dae325d243bb7ecb3ac6db58c7163e197ffcc593b73e583330372f0fe03c400b",
    "bytes": 109287608,
    "accession": "20231229-5212",
    "attachment_id": "F19520DA-2360-C302-8619-8CB6CB100000",
    "source_url": (
        "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
        "#body=3322ba94850c5522a19cdba79dcb6a52"),
    "results_path": (
        "implementation_logs/input_recovery/"
        "OFFICIAL_FERC_INDIVIDUAL_20231229-5212.json"),
    "results_sha256": "54acae8440cff3e9547e643b19e3b723f0113387f0ee114517123a482fefd43f",
    "results_bytes": 16735,
    "ledger_path": "implementation_logs/input_recovery/cache_import_runs.jsonl",
    "ledger_sha256": "fd8ac93a2e00c528d8d57f2e990e8cd46ed3b88fe2349d0c3a6a77a0eeb225d4",
    "ledger_bytes": 26862,
}

# Fixed record-level expectations for the 34 exceptions in the completed
# independent audit.  These are intentionally repeated in the finalizer rather
# than inferred from the candidate database or a mutable repair narrative.  A
# candidate can only close one of these records by satisfying its exact source
# occurrence and semantic contract.  The synthetic finalizer tests use a
# disjoint population and therefore continue to exercise the generic plumbing.
IOC_EXCEPTION_EXPECTATIONS = {
    "blk-14a56caaab90cb00": ("20240102-5300", "utf8_bom", "C000626", "ae32aa82f0ffab9b902a9e3aa4b13a67640ec5ebb732f082a706043d8cbb1f2a", 32316, "utf-8-sig", 488, 488, 16, 1, "original"),
    "blk-1fdc86170bf76cff": ("20240401-5299", "utf8_bom", "C000626", "31e5221abc98902d4eb08c9f9bb4ff3f5d1605b1b6a1aa5658e44240adea5a5a", 30839, "utf-8-sig", 468, 468, 17, 1, "original"),
    "blk-05a8dedba157934e": ("20240701-5096", "utf8_bom", "C000626", "47fcad9455bea5e15edc736bec1142209f1892b41a58e503735ccc4472934ab1", 32289, "utf-8-sig", 488, 488, 17, 1, "original"),
    "blk-65725cb603ec902d": ("20241001-5148", "utf8_bom", "C000626", "70641eedd9862c122016f15cfd78a4d7394540c64b27e78d4d6a9bf5a1fbee75", 32236, "utf-8-sig", 487, 487, 17, 1, "original"),
    "blk-eb9f11f46f600233": ("20250102-5086", "utf8_bom", "C000626", "7ea9423a5daaaad469113b872c22cabc90e34dd2578ec2e796e75f73b2cd6827", 36189, "utf-8-sig", 548, 548, 17, 1, "original"),
    "blk-75cce911464fd9f7": ("20250401-5141", "utf8_bom", "C000626", "3bf5b7cd435c23b1bd34da3f5804d6cbb23897a043e703027694da125bf3fb12", 36578, "utf-8-sig", 555, 555, 17, 1, "original"),
    "blk-acacbee7fb646a01": ("20250701-5124", "utf8_bom", "C000626", "c7ad9df1342a5d36572819ce0305f0186e2509480b85f1bea80ad42833edc80f", 32815, "utf-8-sig", 498, 498, 17, 1, "original"),
    "blk-23902889f085cfa0": ("20251001-5109", "utf8_bom", "C000626", "1c950c6f32076218f939c58c76cd5f0e3ac5212645259d0c501a8c53894a002a", 32756, "utf-8-sig", 499, 499, 17, 1, "original"),
    "blk-ccd4f5d8651f2179": ("20260102-5087", "utf8_bom", "C000626", "c2e56d01a711d8e3b143a98aceae703e5ff5dca4c1152ef31cc0bc2383499c4c", 40743, "utf-8-sig", 619, 619, 17, 1, "original"),
    "blk-c0c35027d5a35025": ("20260401-5124", "utf8_bom", "C000626", "d7ad830c922c28be8fd5b3bdea6ae01cfdd58f617959d1412fbeda70b82baf42", 36273, "utf-8-sig", 552, 552, 17, 1, "original"),
    "blk-010f11ba7ab3f034": ("20260701-5124", "utf8_bom", "C000626", "8175a80fae1035f64c4234b29e2be2984599e6e0f58d6fd4628e0dbba3a54cc5", 40153, "utf-8-sig", 610, 610, 17, 1, "original"),
    "blk-813db4fdd559d280": ("20250331-5099", "preamble", "C000830", "34401d32354da79d52ef68500731e7433a6018ea719a9ad593300779a76d5af8", 1470, "utf-8", 29, 24, 17, 1, "original"),
    "blk-283ff9fe93424302": ("20240102-5281", "shifted_header", "C001031", "774680cc97e80821d082213b53ab6a5b47d75e21d8af18f9b4827ad4780387a4", 20313, "utf-8", 311, 307, 16, 1, "original"),
    "blk-7a2d5583942ea884": ("20240401-5372", "shifted_header", "C001031", "d7034a6589f8d9eeb00e99b5b24750ab320d04e04ea7aeba7971f186bfb094c0", 20335, "utf-8", 312, 308, 17, 1, "original"),
    "blk-bcf7aaefcfb67a22": ("20240701-5084", "shifted_header", "C001031", "1b12103baf52041fb25d8d4177b35e468f37868a1e2d386cabf098afc06396a2", 20996, "utf-8", 321, 317, 17, 1, "original"),
    "blk-951b30d9725b40b2": ("20241001-5033", "shifted_header", "C001031", "ee4bc424615630558f680a0870d0cf252f54d166ea182c8fed7ed3ddcc218d80", 23351, "utf-8", 356, 352, 17, 1, "original"),
    "blk-3baeb084500d53e2": ("20250102-5120", "shifted_header", "C001031", "bf6d6096e51b29a60f27261cf41e99cfdf4de68219ddaa399091b57cc7469aa4", 23290, "utf-8", 356, 352, 0, 0, "original"),
    "blk-4e70dc991829dbe4": ("20250114-5047", "shifted_header", "C001031", "9114decfb80e9f3f073b591aa2036a7cb6ae1469838ddf70d6a2666b09302571", 23284, "utf-8", 356, 352, 17, 1, "revised"),
    "blk-0e1bef5b42c6c2c8": ("20241001-5327", "utf16_le", "C000087", "15b3b65168f5dd51730253e8caa4c9d85fa9a25ed95169ebcbc2444933846934", 23588, "utf-16", 202, 199, 17, 1, "original"),
}

IOC_ACCESS_EXPECTATIONS = {
    "blk-fda56f7e4db97a5f": ("20250701-5067", "C000654", "20250709-5064", "2025-07-01", 28),
    "blk-be0da85a84688d1a": ("20250401-5132", "C000236", "20250401-5144", "2025-04-01", 17),
    "blk-61aa163f18d6bf88": ("20250401-5093", "C000087", "20250401-5098", "2025-04-01", 17),
    "blk-d5a42d025ee876fa": ("20250401-5205", "C000087", "20250401-5098", "2025-04-01", 17),
    "blk-dd6dc6d6d74b49c4": ("20260701-5096", "C001058", "20260701-5117", "2026-07-01", 27),
}

FORM549D_SEMANTIC_EXPECTATIONS = {
    "blk-f8a4430ac37587d6": ("C000826", 2024, "18901", 27, Decimal("10812326")),
    "blk-11652f800db8da1f": ("C000826", 2025, "20443", 19, Decimal("13346064")),
    "blk-b93bb12170552fd3": ("C001773", 2024, "18903", 81, Decimal("8090679")),
    "blk-86a5207f2d741448": ("C001773", 2025, "20445", 70, Decimal("7049958")),
}
FORM549D_POLICY_EXCEPTION_IDS = {
    "blk-c29e1a69a09275d1", "blk-df2155105afc957e"}
FORM549D_CREDENTIAL_EXPECTATIONS = {
    "blk-f9fc2cc2c28a2f14": "C000435",
    "blk-6c1ae60577ce89ca": "C004698",
}
CAPACITY_EXCEPTION_EXPECTATIONS = {
    "blk-433ea58ae2a4a07c": ("20240223-5073", "C003373", "31f9219d3da5bc260392b6e2351cde0313ab38978ade4ee714f70308d7036d94", 337391, Decimal("420"), Decimal("23.7")),
    "blk-187aa7808a87cf34": ("20240223-5075", "C001591", "8e2069eeb07fdfdca7167b323b25cf91f0c34bbf91fd82a8bd82caf9826c0040", 339731, Decimal("465"), Decimal("11.96")),
}
STRICT_EXCEPTION_IDS = (
    set(IOC_EXCEPTION_EXPECTATIONS) | set(IOC_ACCESS_EXPECTATIONS)
    | set(FORM549D_SEMANTIC_EXPECTATIONS) | FORM549D_POLICY_EXCEPTION_IDS
    | set(FORM549D_CREDENTIAL_EXPECTATIONS) | set(CAPACITY_EXCEPTION_EXPECTATIONS)
)

FINAL_FILES = (
    "FINAL_REPAIR_LEDGER.json",
    "FINAL_REPAIR_LEDGER.csv",
    "FINAL_EXCEPTION_DISPOSITIONS_34.json",
    "FINAL_EXCEPTION_DISPOSITIONS_34.csv",
    "FINAL_INPUT_INVENTORY_13.json",
    "FINAL_INPUT_INVENTORY_13.csv",
    "FINAL_ADDITIONAL_INPUTS_DISCOVERED.json",
    "FINAL_ADDITIONAL_INPUTS_DISCOVERED.csv",
    "INTEGRATED_REPAIR_REPORT.md",
    "FINAL_TEST_EVIDENCE_INDEX.json",
)
RECEIPT_NAME = "FINAL_RECORDS_RECEIPT.json"


class FinalRecordsRefused(RuntimeError):
    """The supplied candidate is not eligible for final-record publication."""


class OutputSpec:
    def __init__(self, columns: Sequence[str], backing_table: str,
                 emptiness: str = "if_table", exact_rows: bool = False):
        self.columns = tuple(columns)
        self.backing_table = backing_table
        self.emptiness = emptiness
        self.exact_rows = exact_rows


# A fixed subset of the consumer contract is repeated here on purpose.  A bad
# candidate cannot weaken this finalizer merely by editing acceptance/contract.py.
OUTPUT_CONTRACT: Dict[str, OutputSpec] = {
    "quarterly_key_metrics.csv": OutputSpec(
        ("observation_id", "entity_key", "metric_id", "reporting_year",
         "reporting_period", "period_basis", "period_start", "period_end",
         "value", "value_num", "unit", "scope", "scope_rule",
         "availability", "origin", "method", "version_status", "validation", "qa_flags",
         "review_status", "missing_reason", "filing_id", "source_fact_id", "derivation",
         "display_value", "display_unit", "display_scale"),
        "observations", "may_be_empty"),
    "annual_key_metrics.csv": OutputSpec(
        ("observation_id", "entity_key", "metric_id", "reporting_year",
         "period_basis", "instant_date", "value", "value_num", "unit", "scope",
         "scope_rule",
         "availability", "origin", "method", "version_status", "validation", "qa_flags",
         "review_status", "missing_reason", "filing_id", "source_fact_id",
         "display_value", "display_unit", "display_scale"),
        "observations", "may_be_empty"),
    "canonical_observations.csv": OutputSpec(
        ("observation_id", "entity_key", "legal_name", "metric_id", "source_regime",
         "period_basis", "period_start", "period_end", "instant_date", "reporting_year",
         "reporting_period", "period_label", "scope", "scope_rule", "unit", "value",
         "value_num", "normalized_iso", "availability", "origin",
         "method", "version_status", "validation", "qa_flags", "review_status",
         "missing_reason", "applicability_evidence", "source_system", "filing_id",
         "source_fact_id", "source_context_id", "accession_number", "document_id",
         "selector", "derivation", "concept_local", "taxonomy_version",
         "registry_version", "schedule_page", "candidate_count", "notes",
         "display_value", "display_unit", "display_scale"),
        "observations", exact_rows=True),
    "coverage_by_slot.csv": OutputSpec(
        ("slot_id", "entity_key", "metric_id", "requirement", "outcome",
         "populated", "source_matched", "validated"), "coverage_expected",
        exact_rows=True),
    "coverage_by_entity.csv": OutputSpec(
        ("entity_key", "observations", "populated", "validated"), "observations"),
    "coverage_by_metric.csv": OutputSpec(
        ("metric_id", "source_regime", "observations", "populated", "validated"),
        "observations"),
    "field_status.csv": OutputSpec(
        ("template", "field_id", "metric_id", "adapter", "implementation",
         "outcome", "evidence", "blocker", "adapter_implemented",
         "has_data_in_template", "validated_in_template"), "field_status", "always",
        exact_rows=True),
    "reviewed_source_annotations.csv": OutputSpec(
        ("source_system", "entity_key", "filing_id", "source_fact_id", "metric_id",
         "filed_text", "review_status", "rationale", "evidence_ref",
         "evidence_hash", "reviewer", "reviewed_at", "applied_count"),
        "reviewed_source_annotations", exact_rows=True),
    "lineage_edges.csv": OutputSpec(
        ("observation_id", "input_order", "input_role", "input_source_system",
         "input_filing_id", "input_source_fact_id", "input_observation_id"),
        "lineage_edges", exact_rows=True),
    "filing_inventory.csv": OutputSpec(
        ("source_system", "filing_id", "entity_key", "form", "content_hash",
         "version_status", "is_canonical", "associated_entity_keys",
         "associated_entity_roles"), "filings", exact_rows=True),
    "source_manifest.csv": OutputSpec(
        ("cache_key", "content_hash", "source_system", "source_url", "media_type",
         "byte_size", "first_seen_at", "last_seen_at", "fetch_count", "note"),
        "source_manifest", "always"),
    "documents.csv": OutputSpec(
        ("document_id", "source_system", "filing_id", "media_type", "text_layer",
         "availability", "content_hash", "cache_path"), "documents", exact_rows=True),
    "document_facts.csv": OutputSpec(
        ("document_fact_id", "document_id", "entity_key", "assertion_type",
         "verbatim_span", "extraction_method"), "document_facts", exact_rows=True),
    "events.csv": OutputSpec(
        ("event_id", "entity_key", "event_class", "event_type", "headline",
         "destination", "is_backfill", "comparison_basis", "confidence_note"),
        "events", exact_rows=True),
    "blockers.csv": OutputSpec(
        ("blocker_id", "adapter", "kind", "summary", "exact_error",
         "human_decision_needed"), "blockers", exact_rows=True),
    "applicability.csv": OutputSpec((), "applicability", "may_be_empty",
                                    exact_rows=True),
}
JSON_OUTPUTS = {
    "coverage_statistics.json": (
        "expected_total", "expected_core", "core_populated", "core_validated",
        "all_populated", "all_validated", "core_outcomes", "all_outcomes"),
    "coverage_groups.json": ("by_template", "by_adapter", "by_metric"),
    "field_status_summary.json": (
        "field_rows", "distinct_metrics", "by_outcome", "by_template"),
}

SEMANTIC_TABLES = (
    "entities", "assets", "asset_entity_map", "ownership", "dockets",
    "asset_dockets", "filings", "filing_entities", "filing_dockets", "documents", "source_facts",
    "source_contexts", "source_dimensions", "source_units", "source_manifest",
    "observations", "observation_versions", "lineage_populations", "lineage_edges",
    "document_facts", "events", "coverage_expected", "coverage_measured",
    "field_status", "requirements_crosswalk", "applicability", "taxonomy_sources",
    "blockers", "reviewed_source_annotations",
)


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: pathlib.Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise FinalRecordsRefused(f"{label} is absent or a symlink: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise FinalRecordsRefused(f"cannot read {label} {path}: {exc}") from None


def _load_json(path: pathlib.Path, label: str) -> Tuple[bytes, Any]:
    raw = _read(path, label)
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalRecordsRefused(f"{label} is not valid UTF-8 JSON: {exc}") from None


def _identity(path: pathlib.Path, label: str, logical_path: Optional[str] = None) -> Dict[str, Any]:
    raw = _read(path, label)
    return {"path": logical_path or str(path), "bytes": len(raw), "sha256": _sha(raw)}


def _require_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise FinalRecordsRefused(f"{label} is empty")
    return text


def _require_list(value: Any, label: str, nonempty: bool = True) -> list:
    if not isinstance(value, list) or (nonempty and not value):
        raise FinalRecordsRefused(f"{label} must be {'a non-empty' if nonempty else 'a'} list")
    return value


def _unique_ids(rows: Any, field: str, expected: int, label: str) -> Tuple[List[dict], set]:
    rows = _require_list(rows, f"{label}.rows")
    if len(rows) != expected:
        raise FinalRecordsRefused(f"{label} has {len(rows)} rows, expected exactly {expected}")
    ids: List[str] = []
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise FinalRecordsRefused(f"{label} row {number} is not an object")
        ids.append(_require_text(row.get(field), f"{label} row {number}.{field}"))
    if len(ids) != len(set(ids)):
        duplicates = sorted(k for k, n in collections.Counter(ids).items() if n > 1)
        raise FinalRecordsRefused(f"{label} has duplicate {field} values: {duplicates}")
    return rows, set(ids)


def _path_within(path: pathlib.Path, base: pathlib.Path, label: str) -> pathlib.Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        raise FinalRecordsRefused(f"{label} escapes declared directory {base}: {path}") from None
    return resolved


def _snapshot_digest(root: pathlib.Path, paths: Sequence[pathlib.Path], *,
                     excluded_files: Optional[Iterable[pathlib.Path]] = None) -> str:
    """Recompute the pipeline snapshot using the production byte algorithm.

    This is intentionally implemented here rather than importing ``run.py``:
    importing the candidate while issuing final records would execute code from
    the system being certified and could also read its process configuration.
    """
    root = root.resolve()
    excluded = {
        _path_within(pathlib.Path(path), root, "snapshot exclusion")
        for path in (excluded_files or ())
    }
    digest = hashlib.sha256()
    files: List[pathlib.Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(p for p in path.rglob("*") if p.is_file()
                         and "__pycache__" not in p.parts and p.suffix != ".pyc"
                         and p.resolve() not in excluded)
        elif path.is_file():
            if path.resolve() not in excluded:
                files.append(path)
    for path in sorted(set(files), key=lambda p: str(p)):
        resolved = _path_within(path, root, "snapshot input")
        try:
            label = str(resolved.relative_to(root))
        except ValueError:  # pragma: no cover - _path_within already refuses this
            label = str(resolved)
        digest.update(label.encode("utf-8") + b"\0")
        digest.update(_sha(_read(resolved, f"snapshot file {label}")).encode("ascii") + b"\n")
    return digest.hexdigest()


def _verified_redundant_input_alias(root: pathlib.Path) -> Dict[str, Any]:
    """Verify the sole input alias which packaging is allowed to omit.

    A Build-A tree may carry the capture-tree alias while a clean package does
    not.  Those states are equivalent only when the content-addressed cache
    object, its exact index relation, and both retained provenance records are
    intact.  Missing aliases are accepted; every retained identity is mandatory.
    """
    root = root.resolve()
    declaration = REDUNDANT_INPUT_ALIAS

    def declared_path(key: str, label: str, *, under_cache: bool = False) -> pathlib.Path:
        value = _require_text(declaration.get(key), f"redundant input declaration.{key}")
        relative = pathlib.Path(value)
        if relative.is_absolute():
            raise FinalRecordsRefused(
                f"{label} must be a candidate-relative path: {relative}")
        base = root / "source_cache" if under_cache else root
        path = base / relative
        _path_within(path, base, label)
        return path

    alias = declared_path("alias_path", "redundant official-response capture alias")
    canonical = declared_path(
        "cache_path", "retained canonical cache object for redundant input",
        under_cache=True)
    results_path = declared_path(
        "results_path", "official-FERC individual-response provenance")
    ledger_path = declared_path(
        "ledger_path", "append-only cache-import provenance")
    index_path = root / "source_cache" / "index.json"

    expected_hash = _require_text(
        declaration.get("sha256"), "redundant input declaration.sha256")
    expected_bytes = declaration.get("bytes")
    if not HEX64.fullmatch(expected_hash) or not isinstance(expected_bytes, int) \
            or expected_bytes < 0:
        raise FinalRecordsRefused(
            "redundant input declaration has an invalid canonical identity")

    def exact_file(path: pathlib.Path, *, sha256: str, byte_size: int,
                   label: str, logical_path: str) -> Tuple[bytes, Dict[str, Any]]:
        raw = _read(path, label)
        actual = {"path": logical_path, "bytes": len(raw), "sha256": _sha(raw)}
        if actual["bytes"] != byte_size or actual["sha256"] != sha256:
            raise FinalRecordsRefused(
                f"{label} identity mismatch: expected {byte_size}/{sha256}, "
                f"read {actual['bytes']}/{actual['sha256']}")
        return raw, actual

    _, canonical_identity = exact_file(
        canonical, sha256=expected_hash, byte_size=expected_bytes,
        label="retained canonical cache object for redundant input",
        logical_path=str(pathlib.PurePosixPath("source_cache") /
                         pathlib.PurePosixPath(str(declaration["cache_path"]))))

    _, cache_index = _load_json(index_path, "source-cache index for redundant input")
    source_url = _require_text(
        declaration.get("source_url"), "redundant input declaration.source_url")
    cache_key = _sha(source_url.encode("utf-8"))
    entry = cache_index.get(cache_key) if isinstance(cache_index, dict) else None
    expected_entry = {
        "content_hash": expected_hash,
        "cache_path": declaration["cache_path"],
        "byte_size": expected_bytes,
        "source_system": "eLibrary",
        "source_url": source_url,
    }
    actual_entry = ({key: entry.get(key) for key in expected_entry}
                    if isinstance(entry, dict) else None)
    if actual_entry != expected_entry:
        raise FinalRecordsRefused(
            "source-cache index does not bind the redundant input to its exact "
            "retained official-FERC object")

    results_hash = _require_text(
        declaration.get("results_sha256"),
        "redundant input declaration.results_sha256")
    results_bytes = declaration.get("results_bytes")
    if not HEX64.fullmatch(results_hash) or not isinstance(results_bytes, int) \
            or results_bytes < 0:
        raise FinalRecordsRefused(
            "redundant input declaration has an invalid response-provenance identity")
    results_raw, results_identity = exact_file(
        results_path, sha256=results_hash, byte_size=results_bytes,
        label="official-FERC individual-response provenance",
        logical_path=str(declaration["results_path"]))
    try:
        results = json.loads(results_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalRecordsRefused(
            f"official-FERC individual-response provenance is invalid: {exc}") from None
    result_matches = []
    for row in results.get("results", []) if isinstance(results, dict) else []:
        response = row.get("response") if isinstance(row, dict) else None
        request = row.get("request") if isinstance(row, dict) else None
        if (row.get("accession_number") == declaration.get("accession")
                and row.get("attachment_id") == declaration.get("attachment_id")
                and isinstance(response, dict) and isinstance(request, dict)
                and response.get("http_status") == 200
                and response.get("complete_body") is True
                and response.get("bytes") == expected_bytes
                and response.get("sha256") == expected_hash
                and request.get("cache_url") == source_url):
            result_matches.append(row)
    if len(result_matches) != 1:
        raise FinalRecordsRefused(
            "official-FERC response provenance does not contain exactly one matching "
            "successful capture for the redundant input")

    ledger_hash = _require_text(
        declaration.get("ledger_sha256"),
        "redundant input declaration.ledger_sha256")
    ledger_bytes = declaration.get("ledger_bytes")
    if not HEX64.fullmatch(ledger_hash) or not isinstance(ledger_bytes, int) \
            or ledger_bytes < 0:
        raise FinalRecordsRefused(
            "redundant input declaration has an invalid import-provenance identity")
    ledger_raw, ledger_identity = exact_file(
        ledger_path, sha256=ledger_hash, byte_size=ledger_bytes,
        label="append-only cache-import provenance",
        logical_path=str(declaration["ledger_path"]))
    ledger_matches = []
    try:
        for line in ledger_raw.decode("utf-8").splitlines():
            record = json.loads(line)
            if not isinstance(record, dict) or record.get("status") != "complete":
                continue
            result_identity = record.get("results_identity") or {}
            if (result_identity.get("bytes") != results_bytes
                    or result_identity.get("sha256") != results_hash):
                continue
            for entry_row in record.get("entries", []):
                if (isinstance(entry_row, dict)
                        and entry_row.get("accession") == declaration.get("accession")
                        and entry_row.get("attachment_ids") == [
                            declaration.get("attachment_id")]
                        and entry_row.get("action") in (
                            "imported", "already_present_identical")
                        and entry_row.get("http_status") == 200
                        and entry_row.get("cache_url") == source_url
                        and entry_row.get("byte_size") == expected_bytes
                        and entry_row.get("content_sha256") == expected_hash):
                    ledger_matches.append(entry_row)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalRecordsRefused(
            f"append-only cache-import provenance is invalid: {exc}") from None
    if len(ledger_matches) != 1:
        raise FinalRecordsRefused(
            "append-only cache-import provenance does not contain exactly one matching "
            "successful import for the redundant input")

    alias_identity: Optional[Dict[str, Any]] = None
    if alias.exists() or alias.is_symlink():
        _, alias_identity = exact_file(
            alias, sha256=expected_hash, byte_size=expected_bytes,
            label="redundant official-response capture alias",
            logical_path=str(declaration["alias_path"]))
    return {
        "excluded_path": alias,
        "alias_present": alias_identity is not None,
        "alias_identity": alias_identity,
        "canonical_identity": canonical_identity,
        "cache_index_key": cache_key,
        "cache_index_binding": actual_entry,
        "results_identity": results_identity,
        "ledger_identity": ledger_identity,
    }


def _candidate_input_snapshot(
        root: pathlib.Path,
        alias_verification: Optional[Mapping[str, Any]] = None) -> str:
    verification = (dict(alias_verification)
                    if alias_verification is not None
                    else _verified_redundant_input_alias(root))
    excluded_path = verification.get("excluded_path")
    if not isinstance(excluded_path, pathlib.Path):
        raise FinalRecordsRefused(
            "redundant input verification did not identify its sole excluded path")
    declaration = REDUNDANT_INPUT_ALIAS
    return _snapshot_digest(root, [
        root / "config" / "universe.csv", root / "config" / "annotations",
        root / "config" / "run_plan.json", root / "source_cache" / "index.json",
        root / "inputs" / "day3", root / "inputs" / "audit_baseline",
        root / "inputs" / "official_ferc",
        root / "inputs" / "official_ferc_recovery",
        root / "inputs" / "official_ferc_capacity_20260226_5162",
        root / "inputs" / "official_ferc_elibrary_search_recovery_20260909",
        root / "inputs" / "official_ferc_elibrary_dependency_recovery_20260910",
        root / "inputs" / "reference_regressions",
        root / str(declaration["results_path"]),
        root / str(declaration["ledger_path"]),
        root / "implementation_logs" / "input_recovery" /
            "build_a_v4_dependency_capture.log",
    ], excluded_files={excluded_path})


def _candidate_snapshots(
        root: pathlib.Path, *,
        alias_verification: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    code = _snapshot_digest(root, [
        root / "run.py", root / "build_crosswalk.py", root / "build_field_status.py",
        root / "exporters.py", root / "validate.py", root / "ferclib",
        root / "adapters", root / "migrations",
    ])
    inputs = _candidate_input_snapshot(root, alias_verification)
    return {"code_snapshot": code, "input_snapshot": inputs}


def _test_tool_snapshot(root: pathlib.Path) -> str:
    return _snapshot_digest(root, [
        root / "tests", root / "acceptance", root / "tools",
        root / "build_release.py",
        root / "implementation" / "build_final_release_records.py",
        root / "implementation" / "build_test_run_manifest.py",
        root / "implementation" / "unittest_log_parser.py",
        root / "implementation" / "run_build_b_acceptance.py",
        root / "implementation" / "finalize_external_release_evidence.py",
        root / "implementation" / "import_bounded_elibrary_search_capture.py",
    ])


def _expected_identity(path: pathlib.Path, expected: Mapping[str, Any], label: str) -> Dict[str, Any]:
    sha = expected.get("sha256")
    size = expected.get("bytes")
    if not isinstance(sha, str) or not HEX64.fullmatch(sha):
        raise FinalRecordsRefused(f"{label} has no valid declared SHA-256")
    if not isinstance(size, int) or size < 0:
        raise FinalRecordsRefused(f"{label} has no valid declared byte size")
    actual = _identity(path, label)
    if actual["bytes"] != size or actual["sha256"] != sha:
        raise FinalRecordsRefused(
            f"{label} identity mismatch: expected {size}/{sha}, "
            f"read {actual['bytes']}/{actual['sha256']}")
    return actual


def _resolve_declared_path(root: pathlib.Path, audit_dir: pathlib.Path,
                           declared: str, expected: Mapping[str, Any], label: str) -> pathlib.Path:
    path = pathlib.Path(declared)
    if not path.is_absolute():
        path = root / path
    if path.is_file() and not path.is_symlink():
        _expected_identity(path, expected, label)
        return path
    # Absolute audit paths may move.  Relocation is accepted only by exact
    # bytes+hash, never by a similar filename alone.
    if pathlib.Path(declared).is_absolute():
        same_name = sorted(p for p in audit_dir.rglob(path.name)
                           if p.is_file() and not p.is_symlink())
        matches = []
        for candidate in same_name:
            raw = _read(candidate, label)
            if len(raw) == expected.get("bytes") and _sha(raw) == expected.get("sha256"):
                matches.append(candidate)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise FinalRecordsRefused(f"{label} relocation is ambiguous: {matches}")
    raise FinalRecordsRefused(f"{label} declared input is absent: {declared}")


def _validate_source_identities(doc: Mapping[str, Any], root: pathlib.Path,
                                audit_dir: pathlib.Path, label: str) -> List[Dict[str, Any]]:
    identities = doc.get("source_identities")
    if not isinstance(identities, dict) or not identities:
        raise FinalRecordsRefused(f"{label}.source_identities is empty")
    found: List[Dict[str, Any]] = []

    def walk(value: Any, context: str) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("path"), str) and "sha256" in value and "bytes" in value:
                historical_boundary = ".candidate_artifacts_at_draft_boundary." in context
                if historical_boundary:
                    # These identities intentionally describe the code when the
                    # draft was authored. Repair continued afterward. Treating
                    # normal code evolution as input tampering would force the
                    # final candidate back to the draft. Current code is instead
                    # bound by the run-plan, publication code snapshot and test
                    # logs; the old identity remains explicit counterevidence.
                    path = pathlib.Path(value["path"])
                    path = path if path.is_absolute() else root / path
                    if path.is_file() and not path.is_symlink():
                        actual = _identity(path, context)
                        item = {
                            **actual, "historical_expected_bytes": value["bytes"],
                            "historical_expected_sha256": value["sha256"],
                            "status": ("unchanged_from_draft_boundary"
                                       if actual["bytes"] == value["bytes"]
                                       and actual["sha256"] == value["sha256"]
                                       else "changed_after_draft_boundary")}
                    else:
                        item = {
                            "path": str(path), "bytes": None, "sha256": None,
                            "historical_expected_bytes": value["bytes"],
                            "historical_expected_sha256": value["sha256"],
                            "status": "historical_artifact_not_carried_in_candidate",
                        }
                else:
                    path = _resolve_declared_path(root, audit_dir, value["path"], value, context)
                    item = _expected_identity(path, value, context)
                    item["status"] = "identity_verified"
                item["declared_path"] = value["path"]
                found.append(item)
                return
            for key, child in value.items():
                if (isinstance(child, dict) and "sha256" in child and "bytes" in child
                        and "path" not in child and isinstance(key, str)):
                    path = _resolve_declared_path(root, audit_dir, key, child,
                                                  f"{context}.{key}")
                    item = _expected_identity(path, child, f"{context}.{key}")
                    item["declared_path"] = key
                    found.append(item)
                else:
                    walk(child, f"{context}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{context}[{index}]")

    walk(identities, f"{label}.source_identities")
    if not found:
        raise FinalRecordsRefused(f"{label} contains no hash-verifiable source identity")
    return found


def _load_and_validate_drafts(root: pathlib.Path, audit_dir: pathlib.Path,
                              ledger_path: pathlib.Path, exception_path: pathlib.Path,
                              input_path: pathlib.Path) -> Dict[str, Any]:
    ledger_raw, ledger = _load_json(ledger_path, "repair-ledger draft")
    exception_raw, exceptions = _load_json(exception_path, "exception draft")
    input_raw, inputs = _load_json(input_path, "input-inventory draft")
    docs = (("repair ledger", ledger, LEDGER_ROWS, "issue_id"),
            ("exception ledger", exceptions, EXCEPTION_ROWS, "original_exception_id"),
            ("input inventory", inputs, INPUT_ROWS, "input_id"))
    validated_ids: Dict[str, set] = {}
    source_identities: Dict[str, list] = {}
    for label, doc, count, id_field in docs:
        if not isinstance(doc, dict) or doc.get("draft") is not True \
                or "draft" not in str(doc.get("schema", "")).lower():
            raise FinalRecordsRefused(
                f"{label} must be an explicitly labelled draft; a stale/final file "
                "may not be recycled as generator input")
        rows, ids = _unique_ids(doc.get("rows"), id_field, count, label)
        validated_ids[label] = ids
        declared_count = (doc.get("counts") or {}).get("rows")
        if declared_count != count:
            raise FinalRecordsRefused(
                f"{label} declared row count {declared_count!r}, expected {count}")
        for row in rows:
            basis = _require_text(row.get("root_cause") or row.get("classification_basis"),
                                  f"{label} {row[id_field]} root cause/basis")
            if basis.strip().lower() in {"tbd", "todo", "fixme", "placeholder", "unclaimed"}:
                raise FinalRecordsRefused(
                    f"{label} {row[id_field]} root cause/basis is unresolved placeholder text")
            evidence = row.get("evidence")
            if not isinstance(evidence, (dict, list)) or not evidence:
                raise FinalRecordsRefused(f"{label} {row[id_field]} has empty evidence")
            disposition_field = ("current_disposition" if label == "repair ledger"
                                 else "disposition")
            disposition = _require_text(
                row.get(disposition_field), f"{label} {row[id_field]}.{disposition_field}")
            if disposition.strip().lower() in {"unclaimed", "todo", "tbd", "placeholder"}:
                raise FinalRecordsRefused(
                    f"{label} {row[id_field]} remains unclaimed or placeholder-only")
            if label == "repair ledger" and row.get("audit_claim_preserved") is not True:
                raise FinalRecordsRefused(
                    f"repair ledger {row[id_field]} does not preserve the audit claim")
        source_identities[label] = _validate_source_identities(
            doc, root, audit_dir, label)

    return {
        "ledger": ledger, "exceptions": exceptions, "inputs": inputs,
        "ledger_identity": {"path": str(ledger_path.relative_to(root)),
                            "bytes": len(ledger_raw), "sha256": _sha(ledger_raw)},
        "exception_identity": {"path": str(exception_path.relative_to(root)),
                               "bytes": len(exception_raw), "sha256": _sha(exception_raw)},
        "input_identity": {"path": str(input_path.relative_to(root)),
                           "bytes": len(input_raw), "sha256": _sha(input_raw)},
        "ids": validated_ids,
        "source_identities": source_identities,
    }


def _load_and_validate_supplemental_drafts(
        root: pathlib.Path, audit_dir: pathlib.Path, related_path: pathlib.Path,
        additional_input_path: pathlib.Path) -> Dict[str, Any]:
    """Load repair-time discoveries without changing the audited populations.

    The independent audit's 64 issue rows and 13 absent inputs remain exact,
    immutable populations.  Defects and prerequisites discovered while fixing
    them are separately labelled here, then carried into the combined final
    repair ledger and a distinct additional-input inventory.
    """
    related_raw, related = _load_json(related_path, "related-finding draft")
    input_raw, additional = _load_json(
        additional_input_path, "additional-input draft")
    specifications = (
        ("related findings", related, RELATED_FINDING_ROWS, "issue_id",
         "ferc-related-repair-findings-draft-v1"),
        ("additional inputs", additional, ADDITIONAL_INPUT_ROWS, "input_id",
         "ferc-additional-inputs-discovered-draft-v1"),
    )
    ids: Dict[str, set] = {}
    source_identities: Dict[str, list] = {}
    for label, doc, count, id_field, schema in specifications:
        if not isinstance(doc, dict) or doc.get("schema") != schema \
                or doc.get("draft") is not True:
            raise FinalRecordsRefused(f"{label} is not the required labelled draft")
        rows, row_ids = _unique_ids(doc.get("rows"), id_field, count, label)
        if (doc.get("counts") or {}).get("rows") != count:
            raise FinalRecordsRefused(f"{label} declared row count is not {count}")
        ids[label] = row_ids
        source_identities[label] = _validate_source_identities(
            doc, root, audit_dir, label)
        for row in rows:
            basis = _require_text(
                row.get("root_cause") or row.get("classification_basis"),
                f"{label} {row[id_field]} root cause/basis")
            if BASIS_PLACEHOLDER.search(basis):
                raise FinalRecordsRefused(
                    f"{label} {row[id_field]} contains placeholder root cause/basis")
            evidence = row.get("evidence")
            if not isinstance(evidence, (dict, list)) or not evidence:
                raise FinalRecordsRefused(f"{label} {row[id_field]} has empty evidence")
    for row in related["rows"]:
        if row.get("relationship_to_final_audit") != \
                "not_in_final_audit_discovered_during_repair":
            raise FinalRecordsRefused(
                f"related finding {row['issue_id']} is not explicitly separated from audit")
        _require_text(row.get("targeted_test"),
                      f"related finding {row['issue_id']}.targeted_test")
        test_ids = _require_list(
            row.get("target_test_ids"),
            f"related finding {row['issue_id']}.target_test_ids")
        if not test_ids or not all(isinstance(value, str) and value.strip()
                                   for value in test_ids):
            raise FinalRecordsRefused(
                f"related finding {row['issue_id']} has invalid target-test identities")
        disposition = row.get("current_disposition")
        package_only = {"R32-PENDING-TEST-SPEC-PAYLOAD-LEAK",
                        "R34-STALE-PROVISIONAL-PAYLOAD-LEAKS",
                        "R45-BUILD-B-PENDING-INSTRUCTION-CORRUPTS-EXTRACTION"}
        if disposition != "fixed_pending_build" \
                and not (row["issue_id"] in package_only
                         and disposition == "fixed_pending_package"):
            raise FinalRecordsRefused(
                f"related finding {row['issue_id']} lacks a pre-Build-A disposition")
    if ids["related findings"] != RELATED_FINDING_IDS \
            or ids["additional inputs"] != ADDITIONAL_INPUT_IDS:
        raise FinalRecordsRefused(
            "supplemental draft population differs from the frozen repair-time discoveries")
    return {
        "related": related,
        "additional_inputs": additional,
        "ids": ids,
        "source_identities": source_identities,
        "related_identity": {
            "path": str(related_path.relative_to(root)),
            "bytes": len(related_raw), "sha256": _sha(related_raw)},
        "additional_input_identity": {
            "path": str(additional_input_path.relative_to(root)),
            "bytes": len(input_raw), "sha256": _sha(input_raw)},
    }


def _audit_file(audit_dir: pathlib.Path, name: str) -> pathlib.Path:
    direct = audit_dir / name
    if direct.is_file() and not direct.is_symlink():
        return direct
    matches = [p for p in audit_dir.rglob(name) if p.is_file() and not p.is_symlink()]
    if len(matches) != 1:
        raise FinalRecordsRefused(f"audit input {name} absent or ambiguous under {audit_dir}")
    return matches[0]


def _load_audit_population(audit_dir: pathlib.Path, drafts: Mapping[str, Any]) -> Dict[str, Any]:
    files = {
        "issues": _audit_file(audit_dir, "CODEX_REAUDIT_ISSUES.json"),
        "exceptions": _audit_file(audit_dir, "EXCEPTION_RECONCILIATION_34.json"),
        "inputs": _audit_file(audit_dir, "MISSING_INPUT_IMPACT.json"),
        "coverage": _audit_file(audit_dir, "EXACT_COVERAGE_COUNT_EXTRACTS.json"),
        "fields": _audit_file(audit_dir, "FIELD_RECONCILIATION_SUMMARY.json"),
    }
    audit_index_path = _audit_file(audit_dir, "EVIDENCE_INDEX.json")
    index_raw, audit_index = _load_json(audit_index_path, "final audit evidence index")
    entries = audit_index.get("entries") if isinstance(audit_index, dict) else None
    if not isinstance(entries, list):
        raise FinalRecordsRefused("final audit evidence index has no entries")
    indexed = {str(entry.get("path")): entry for entry in entries if isinstance(entry, dict)}
    loaded: Dict[str, Any] = {"identities": {
        "evidence_index": {"path": audit_index_path.name, "bytes": len(index_raw),
                           "sha256": _sha(index_raw)}}}
    for key, path in files.items():
        expected = indexed.get(path.name)
        if not isinstance(expected, dict):
            raise FinalRecordsRefused(f"final audit evidence index omits {path.name}")
        _expected_identity(path, expected, f"final audit indexed file {path.name}")
        raw, body = _load_json(path, f"final audit {key}")
        loaded[key] = body
        loaded["identities"][key] = {
            "path": path.name, "bytes": len(raw), "sha256": _sha(raw)}
    for audit_key, draft_key in (("issues", "repair ledger"),
                                 ("exceptions", "exception ledger"),
                                 ("inputs", "input inventory")):
        identity = loaded["identities"][audit_key]
        declared = drafts["source_identities"].get(draft_key, [])
        if not any(item.get("bytes") == identity["bytes"]
                   and item.get("sha256") == identity["sha256"] for item in declared):
            raise FinalRecordsRefused(
                f"loaded final audit {audit_key} is not the identity declared by {draft_key}")

    issue_rows, issue_ids = _unique_ids(
        loaded["issues"].get("rows"), "issue_id", LEDGER_ROWS, "final audit issues")
    if loaded["issues"].get("draft") is not False:
        raise FinalRecordsRefused("final audit issue register is not labelled final")
    if issue_ids != drafts["ids"]["repair ledger"]:
        raise FinalRecordsRefused("repair-ledger issue population differs from final audit")
    loaded["issue_by_id"] = {r["issue_id"]: r for r in issue_rows}
    expected_a = {f"A{i:02d}" for i in range(1, 23)}
    actual_a = {r["issue_id"] for r in issue_rows
                if r.get("source_row_type") == "original_audit_A01_A22"}
    if actual_a != expected_a:
        raise FinalRecordsRefused(
            f"original A01-A22 population mismatch: missing={sorted(expected_a-actual_a)}, "
            f"extra={sorted(actual_a-expected_a)}")

    audit_exception_rows, audit_exception_ids = _unique_ids(
        loaded["exceptions"].get("rows"), "blocker_id", EXCEPTION_ROWS,
        "final audit exceptions")
    if audit_exception_ids != drafts["ids"]["exception ledger"]:
        raise FinalRecordsRefused("exception draft population differs from final audit")
    loaded["exception_by_id"] = {r["blocker_id"]: r for r in audit_exception_rows}

    absent = []
    for row in loaded["inputs"].get("unique_requests", []):
        if row.get("present_in_current_and_shipped_full_cache_index") is False:
            accession = _require_text(row.get("accession_number"),
                                      "final audit missing input accession")
            absent.append(f"elibrary:{accession}")
    if len(absent) != INPUT_ROWS or len(set(absent)) != INPUT_ROWS:
        raise FinalRecordsRefused(
            f"final audit absent-input population is {len(absent)}, expected {INPUT_ROWS}")
    if set(absent) != drafts["ids"]["input inventory"]:
        raise FinalRecordsRefused("input draft population differs from final audit absent 13")
    loaded["input_by_accession"] = {
        str(r["accession_number"]): r for r in loaded["inputs"].get("unique_requests", [])
        if r.get("present_in_current_and_shipped_full_cache_index") is False}
    return loaded


def _verify_run_plan_inputs(root: pathlib.Path) -> Dict[str, Any]:
    plan_path = root / "config" / "run_plan.json"
    raw, plan = _load_json(plan_path, "run plan")
    if plan.get("schema") != "ferc_operating_assets_run_plan_v2" or not plan.get("cache_only"):
        raise FinalRecordsRefused("run plan is not the cache-only v2 plan")
    required = _require_list(plan.get("required_inputs"), "run_plan.required_inputs")
    seen = set()
    inputs = []
    for item in required:
        if not isinstance(item, dict):
            raise FinalRecordsRefused("run-plan required input is not an object")
        rel = _require_text(item.get("path"), "run-plan input path")
        if rel in seen:
            raise FinalRecordsRefused(f"duplicate run-plan required input: {rel}")
        seen.add(rel)
        path = _path_within(root / rel, root, "run-plan input")
        expected_sha = item.get("sha256")
        if not isinstance(expected_sha, str) or not HEX64.fullmatch(expected_sha):
            raise FinalRecordsRefused(f"run-plan input {rel} has no valid SHA-256")
        actual = _identity(path, f"run-plan input {rel}")
        if actual["sha256"] != expected_sha:
            raise FinalRecordsRefused(
                f"run-plan input {rel} hash mismatch: expected {expected_sha}, "
                f"read {actual['sha256']}")
        if "bytes" in item and item.get("bytes") != actual["bytes"]:
            raise FinalRecordsRefused(
                f"run-plan input {rel} size mismatch: expected {item.get('bytes')!r}, "
                f"read {actual['bytes']}")
        actual["path"] = rel
        inputs.append(actual)
    return {
        "plan": plan,
        "identity": {"path": "config/run_plan.json", "bytes": len(raw),
                     "sha256": _sha(raw)},
        "required_inputs": inputs,
    }


def _open_database(path: pathlib.Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise FinalRecordsRefused(f"Build A database is absent or a symlink: {path}")
    wal = pathlib.Path(str(path) + "-wal")
    if wal.is_file() and wal.stat().st_size:
        raise FinalRecordsRefused(
            f"Build A database has a non-empty WAL ({wal.stat().st_size} bytes); "
            "checkpoint/close it before hashing final state")
    uri = "file:" + path.resolve().as_posix() + "?mode=ro&immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        return con
    except sqlite3.Error as exc:
        raise FinalRecordsRefused(f"cannot open Build A database read-only: {exc}") from None


def _table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    return [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')]


def _count(con: sqlite3.Connection, table: str) -> int:
    return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _database_checks(con: sqlite3.Connection, db_path: pathlib.Path,
                     root: pathlib.Path) -> Dict[str, Any]:
    quick = [str(row[0]) for row in con.execute("PRAGMA quick_check")]
    if quick != ["ok"]:
        raise FinalRecordsRefused(f"SQLite quick_check failed: {quick[:10]}")
    fk = [tuple(row) for row in con.execute("PRAGMA foreign_key_check")]
    if fk:
        raise FinalRecordsRefused(f"SQLite foreign-key violations: {fk[:10]}")

    required_tables = set(spec.backing_table for spec in OUTPUT_CONTRACT.values()) | {
        "coverage_measured", "publication_generations", "runs", "checkpoints",
        "requirements_crosswalk", "reviewed_source_annotations", "filings",
        "observations", "documents", "events", "field_status", "lineage_edges",
    }
    present = {str(row[0]) for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required_tables - present)
    if missing:
        raise FinalRecordsRefused(f"Build A database lacks required tables: {missing}")
    table_counts = {table: _count(con, table) for table in sorted(required_tables)}
    for table in ("observations", "filings", "coverage_expected", "coverage_measured",
                  "requirements_crosswalk", "field_status", "reviewed_source_annotations"):
        if not table_counts[table]:
            raise FinalRecordsRefused(f"Build A database required table is empty: {table}")
    if table_counts["coverage_expected"] != table_counts["coverage_measured"]:
        raise FinalRecordsRefused("coverage expected/measured row counts disagree")
    orphan = con.execute(
        "SELECT COUNT(*) FROM coverage_expected e LEFT JOIN coverage_measured m "
        "USING(slot_id) WHERE m.slot_id IS NULL").fetchone()[0]
    reverse_orphan = con.execute(
        "SELECT COUNT(*) FROM coverage_measured m LEFT JOIN coverage_expected e "
        "USING(slot_id) WHERE e.slot_id IS NULL").fetchone()[0]
    if orphan or reverse_orphan:
        raise FinalRecordsRefused(
            f"coverage logical orphan counts expected={orphan}, measured={reverse_orphan}")
    if table_counts["field_status"] != 168:
        raise FinalRecordsRefused(
            f"field_status has {table_counts['field_status']} rows, expected exactly 168")
    if table_counts["requirements_crosswalk"] != 146:
        raise FinalRecordsRefused(
            "requirements_crosswalk does not retain the 146-row source population")
    if table_counts["reviewed_source_annotations"] != 7:
        raise FinalRecordsRefused("reviewed annotation population is not exactly seven")

    running = con.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0]
    in_progress = con.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE state='in_progress'").fetchone()[0]
    if running or in_progress:
        raise FinalRecordsRefused(
            f"Build A has unfinished durable status: running_runs={running}, "
            f"in_progress_checkpoints={in_progress}")
    field_rows = [dict(row) for row in con.execute("SELECT * FROM field_status")]
    headline_rows = [row for row in field_rows
                     if "role=headline" in str(row.get("note") or "")]
    def readiness(rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
        return {
            "rows": len(rows),
            "adapter_implemented": sum(bool(row.get("adapter_implemented")) for row in rows),
            "has_data_in_template": sum(bool(row.get("has_data_in_template")) for row in rows),
            "validated_in_template": sum(bool(row.get("validated_in_template")) for row in rows),
        }
    return {
        "identity": _identity(db_path, "Build A database",
                              str(db_path.relative_to(root))),
        "sqlite": {
            "quick_check": quick[0], "foreign_key_violations": len(fk),
            "schema_version": int(con.execute("PRAGMA schema_version").fetchone()[0]),
            "user_version": int(con.execute("PRAGMA user_version").fetchone()[0]),
            "journal_mode": str(con.execute("PRAGMA journal_mode").fetchone()[0]),
            "running_runs": int(running), "in_progress_checkpoints": int(in_progress),
        },
        "table_counts": table_counts,
        "field_readiness": {
            "all_fields": readiness(field_rows),
            "headline_fields": readiness(headline_rows),
            "basis": ("Counts are recomputed from this Build A field_status population; "
                      "adapter implementation, local data and local validation are separate."),
        },
    }


def _database_semantic_identity(con: sqlite3.Connection) -> str:
    """Independently bind consumer/provenance state, not SQLite page layout."""
    digest = hashlib.sha256()
    for table in SEMANTIC_TABLES:
        pragma = list(con.execute(f'PRAGMA table_info("{table}")'))
        columns = [str(row[1]) for row in pragma]
        if not columns:
            raise FinalRecordsRefused(f"semantic-identity table is absent: {table}")
        primary = [str(row[1]) for row in sorted(pragma, key=lambda row: row[5]) if row[5]]
        order = primary or columns
        digest.update((table + "\0" + "\0".join(columns) + "\n").encode("utf-8"))
        sql = ('SELECT ' + ','.join('"%s"' % c for c in columns) +
               ' FROM "%s" ORDER BY ' % table + ','.join('"%s"' % c for c in order))
        for row in con.execute(sql):
            digest.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":"),
                                     default=str).encode("utf-8") + b"\n")
    return digest.hexdigest()


def _verify_manifest_generation(root: pathlib.Path, receipt_path: pathlib.Path,
                                compatibility_dir: pathlib.Path,
                                expected_kind: str) -> Dict[str, Any]:
    raw, receipt = _load_json(receipt_path, f"{expected_kind} publication receipt")
    if not isinstance(receipt, dict) or receipt.get("kind") != expected_kind:
        raise FinalRecordsRefused(f"publication receipt kind is not {expected_kind}")
    generation_id = receipt.get("generation_id")
    files = receipt.get("files")
    metadata = receipt.get("metadata")
    if not isinstance(generation_id, str) or not HEX64.fullmatch(generation_id):
        raise FinalRecordsRefused("publication generation id is invalid")
    if not isinstance(files, dict) or not files or not isinstance(metadata, dict):
        raise FinalRecordsRefused("publication receipt has no files/metadata")
    expected_id = _sha(json.dumps(
        {"kind": expected_kind, "files": files, "metadata": metadata},
        sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if generation_id != expected_id:
        raise FinalRecordsRefused("publication generation id does not bind its manifest")
    generation_rel = pathlib.PurePosixPath(".generations") / expected_kind / generation_id
    if pathlib.PurePosixPath(str(receipt.get("generation_path", ""))) != generation_rel:
        raise FinalRecordsRefused("publication generation path disagrees with generation id")
    generation = _path_within(root / pathlib.Path(*generation_rel.parts), root,
                              "publication generation")
    if generation.is_symlink() or not generation.is_dir():
        raise FinalRecordsRefused(f"publication generation directory absent: {generation}")
    embedded_raw, embedded = _load_json(
        generation / "GENERATION_MANIFEST.json", "embedded publication manifest")
    if embedded != receipt:
        raise FinalRecordsRefused("publication receipt and embedded manifest disagree")
    identities = {}
    for name, expected in sorted(files.items()):
        pure = pathlib.PurePosixPath(str(name))
        if pure.is_absolute() or ".." in pure.parts or "\\" in str(name):
            raise FinalRecordsRefused(f"unsafe publication member path: {name}")
        generated = _path_within(generation / pathlib.Path(*pure.parts), generation,
                                 f"generated export {name}")
        actual = _expected_identity(generated, expected, f"generated export {name}")
        compatible = _path_within(compatibility_dir / pathlib.Path(*pure.parts),
                                  compatibility_dir, f"compatibility export {name}")
        compatibility = _expected_identity(compatible, expected,
                                           f"compatibility export {name}")
        actual["path"] = f"{generation_rel.as_posix()}/{name}"
        actual["compatibility_path"] = str(compatible.relative_to(root))
        if actual["sha256"] != compatibility["sha256"]:
            raise FinalRecordsRefused(f"generated and compatibility bytes differ: {name}")
        identities[str(name)] = actual
    return {
        "receipt_identity": {"path": str(receipt_path.relative_to(root)),
                             "bytes": len(raw), "sha256": _sha(raw)},
        "embedded_manifest_identity": {
            "path": str((generation / "GENERATION_MANIFEST.json").relative_to(root)),
            "bytes": len(embedded_raw), "sha256": _sha(embedded_raw)},
        "generation_id": generation_id,
        "manifest": receipt,
        "metadata": metadata,
        "files": identities,
    }


def _csv_rows(path: pathlib.Path, label: str) -> Tuple[List[str], List[dict], bytes]:
    raw = _read(path, label)
    if not raw.strip():
        return [], [], raw
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FinalRecordsRefused(f"{label} is not UTF-8: {exc}") from None
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise FinalRecordsRefused(f"{label} has no CSV header")
    return list(reader.fieldnames), list(reader), raw


def _csv_scalar(value: Any) -> str:
    return "" if value is None else str(value)


def _compare_export_rows(filename: str, rows: Sequence[Mapping[str, Any]],
                         expected_rows: Sequence[Mapping[str, Any]],
                         key_columns: Sequence[str], compare_columns: Sequence[str]) -> None:
    def keyed(population: Sequence[Mapping[str, Any]], side: str) -> Dict[Tuple[str, ...], dict]:
        result: Dict[Tuple[str, ...], dict] = {}
        for row in population:
            key = tuple(_csv_scalar(row.get(col)) for col in key_columns)
            if key in result:
                raise FinalRecordsRefused(f"{filename} has duplicate {side} identity {key}")
            result[key] = dict(row)
        return result

    actual = keyed(rows, "CSV")
    expected = keyed(expected_rows, "database")
    if set(actual) != set(expected):
        raise FinalRecordsRefused(
            f"{filename} identities differ from database "
            f"(csv_only={len(set(actual)-set(expected))}, "
            f"db_only={len(set(expected)-set(actual))})")
    for key in sorted(expected):
        for column in compare_columns:
            observed = _csv_scalar(actual[key].get(column))
            wanted = _csv_scalar(expected[key].get(column))
            if observed != wanted:
                raise FinalRecordsRefused(
                    f"{filename} row {key} column {column} differs from database/export "
                    f"contract: csv={observed!r}, expected={wanted!r}")


def _critical_export_parity(root: pathlib.Path, con: sqlite3.Connection,
                            parsed_rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    """Compare consumer-critical values, not merely row IDs and headers."""
    canonical_columns = OUTPUT_CONTRACT["canonical_observations.csv"].columns
    expected_canonical = [dict(row) for row in con.execute(
        "SELECT o.observation_id,o.entity_key,e.legal_name,o.metric_id,o.source_regime,"
        "o.period_basis,o.period_start,o.period_end,o.instant_date,o.reporting_year,"
        "o.reporting_period,o.period_label,o.scope,o.scope_rule,o.unit,o.value_text AS value,"
        "o.value_num,o.normalized_iso,o.availability,o.origin,o.method,o.version_status,"
        "o.validation,o.qa_flags,o.review_status,o.missing_reason,o.applicability_evidence,"
        "o.source_system,o.filing_id,o.source_fact_id,o.source_context_id,o.accession_number,"
        "o.document_id,o.selector,o.derivation,o.concept_local,o.taxonomy_version,"
        "o.registry_version,o.schedule_page,o.candidate_count,o.notes FROM observations o "
        "LEFT JOIN entities e ON e.entity_key=o.entity_key")]
    gate_note = ("scope_undecodable: the source document's font supplies no usable "
                 "character map, so this figure's scope extracted as glyph indices "
                 "rather than text. The value is as filed and is retained, but what "
                 "it measures cannot be established, so it must not be compared or "
                 "aggregated")
    for row in expected_canonical:
        if row.get("value") not in (None, "") and UNDECODABLE_SCOPE.search(
                str(row.get("scope") or "")):
            row["validation"] = "blocked_ambiguity"
            row["qa_flags"] = f"{gate_note}; {row.get('qa_flags') or ''}".strip("; ")
    _compare_export_rows(
        "canonical_observations.csv", parsed_rows["canonical_observations.csv"],
        expected_canonical, ("observation_id",),
        tuple(c for c in canonical_columns if not c.startswith("display_")))

    filtered_queries = {
        "quarterly_key_metrics.csv": (
            "SELECT o.observation_id,o.entity_key,o.metric_id,o.reporting_year,"
            "o.reporting_period,o.period_basis,o.period_start,o.period_end,o.value_text AS value,"
            "o.value_num,o.unit,o.scope,o.scope_rule,o.availability,o.origin,o.method,"
            "o.version_status,o.validation,o.qa_flags,o.review_status,o.missing_reason,"
            "o.filing_id,o.source_fact_id,o.derivation FROM observations o "
            "WHERE o.period_basis IN ('quarter','ytd')"),
        "annual_key_metrics.csv": (
            "SELECT o.observation_id,o.entity_key,o.metric_id,o.reporting_year,o.period_basis,"
            "o.instant_date,o.value_text AS value,o.value_num,o.unit,o.scope,o.scope_rule,"
            "o.availability,o.origin,o.method,o.version_status,o.validation,o.qa_flags,"
            "o.review_status,o.missing_reason,o.filing_id,o.source_fact_id FROM observations o "
            "WHERE o.period_basis IN ('annual','instant','annual_observation')"),
    }
    for filename, sql in filtered_queries.items():
        expected = [dict(row) for row in con.execute(sql)]
        for row in expected:
            if row.get("value") not in (None, "") and UNDECODABLE_SCOPE.search(
                    str(row.get("scope") or "")):
                row["validation"] = "blocked_ambiguity"
                row["qa_flags"] = f"{gate_note}; {row.get('qa_flags') or ''}".strip("; ")
        columns = tuple(c for c in OUTPUT_CONTRACT[filename].columns
                        if not c.startswith("display_"))
        _compare_export_rows(filename, parsed_rows[filename], expected,
                             ("observation_id",), columns)

    value_num = {str(row["observation_id"]): row["value_num"] for row in con.execute(
        "SELECT observation_id,value_num FROM observations")}
    for filename in ("canonical_observations.csv", "quarterly_key_metrics.csv",
                     "annual_key_metrics.csv"):
        for row in parsed_rows[filename]:
            observation_id = str(row.get("observation_id") or "")
            metric_id = str(row.get("metric_id") or "")
            stored_unit = str(row.get("unit") or "")
            expected_unit, expected_scale = DISPLAY_CONTRACT.get(
                metric_id, (stored_unit, 1.0))
            try:
                actual_scale = float(row.get("display_scale"))
            except (TypeError, ValueError):
                raise FinalRecordsRefused(
                    f"{filename} {observation_id} has invalid display_scale") from None
            if actual_scale != expected_scale or str(row.get("display_unit") or "") != expected_unit:
                raise FinalRecordsRefused(
                    f"{filename} {observation_id} violates the independent display contract")
            number = value_num.get(observation_id)
            expected_display = ("" if number in (None, "") else
                                f"{float(number) * expected_scale:.6f}".rstrip("0").rstrip("."))
            if str(row.get("display_value") or "") != expected_display:
                raise FinalRecordsRefused(
                    f"{filename} {observation_id} has wrong display_value")

    slot_columns = OUTPUT_CONTRACT["coverage_by_slot.csv"].columns
    expected_slots = [dict(row) for row in con.execute(
        "SELECT e.slot_id,e.entity_key,e.metric_id,e.requirement,m.outcome,m.populated,"
        "m.source_matched,m.validated FROM coverage_expected e JOIN coverage_measured m "
        "USING(slot_id)")]
    _compare_export_rows("coverage_by_slot.csv", parsed_rows["coverage_by_slot.csv"],
                         expected_slots, ("slot_id",), slot_columns)

    one_table = {
        "field_status.csv": ("field_status", ("template", "field_id")),
        "reviewed_source_annotations.csv": (
            "reviewed_source_annotations",
            ("source_system", "entity_key", "filing_id", "source_fact_id", "metric_id")),
        "lineage_edges.csv": ("lineage_edges", ("observation_id", "input_order")),
        "documents.csv": ("documents", ("document_id",)),
        "document_facts.csv": ("document_facts", ("document_fact_id",)),
        "events.csv": ("events", ("event_id",)),
        "blockers.csv": ("blockers", ("blocker_id",)),
    }
    for filename, (table, keys) in one_table.items():
        columns = OUTPUT_CONTRACT[filename].columns
        sql = "SELECT " + ",".join('"%s"' % c for c in columns) + ' FROM "%s"' % table
        expected = [dict(row) for row in con.execute(sql)]
        _compare_export_rows(filename, parsed_rows[filename], expected, keys, columns)

    # The filing inventory deliberately exposes the many-to-many occurrence
    # relationship rather than only the compatibility anchor in ``filings``.
    # These two values are computed export columns, so compare them to an
    # independently assembled, deterministically ordered database population.
    filing_columns = OUTPUT_CONTRACT["filing_inventory.csv"].columns
    expected_filings = [dict(row) for row in con.execute(
        "SELECT f.source_system,f.filing_id,f.entity_key,f.form,f.content_hash,"
        "f.version_status,f.is_canonical,"
        "(SELECT group_concat(entity_key, '|') FROM "
        " (SELECT entity_key FROM filing_entities fe "
        "  WHERE fe.source_system=f.source_system AND fe.filing_id=f.filing_id "
        "  ORDER BY entity_key)) AS associated_entity_keys,"
        "(SELECT group_concat(entity_key || ':' || association_role, '|') FROM "
        " (SELECT entity_key,association_role FROM filing_entities fe "
        "  WHERE fe.source_system=f.source_system AND fe.filing_id=f.filing_id "
        "  ORDER BY entity_key)) AS associated_entity_roles "
        "FROM filings f")]
    _compare_export_rows(
        "filing_inventory.csv", parsed_rows["filing_inventory.csv"],
        expected_filings, ("source_system", "filing_id"), filing_columns)

    # source_manifest.csv is cache-index backed rather than database backed.
    _, index = _load_json(root / "source_cache" / "index.json", "source-cache index")
    if not isinstance(index, dict):
        raise FinalRecordsRefused("source-cache index is not an object")
    manifest_columns = OUTPUT_CONTRACT["source_manifest.csv"].columns
    expected_manifest = []
    for cache_key, record in index.items():
        if not isinstance(record, dict):
            raise FinalRecordsRefused(f"source-cache index entry {cache_key} is not an object")
        expected_manifest.append({"cache_key": cache_key,
                                  **{c: record.get(c) for c in manifest_columns
                                     if c != "cache_key"}})
    _compare_export_rows("source_manifest.csv", parsed_rows["source_manifest.csv"],
                         expected_manifest, ("cache_key",), manifest_columns)

    expected_entities = [dict(row) for row in con.execute(
        "SELECT o.entity_key,COUNT(*) AS observations,"
        "SUM(o.availability='present') AS populated,"
        "SUM(o.validation='pass' AND o.availability='present') AS validated "
        "FROM observations o LEFT JOIN entities e ON e.entity_key=o.entity_key "
        "LEFT JOIN asset_entity_map m ON m.entity_key=o.entity_key "
        "LEFT JOIN assets a ON a.asset_id=m.asset_id GROUP BY o.entity_key")]
    _compare_export_rows(
        "coverage_by_entity.csv", parsed_rows["coverage_by_entity.csv"],
        expected_entities, ("entity_key",), OUTPUT_CONTRACT["coverage_by_entity.csv"].columns)
    expected_metrics = [dict(row) for row in con.execute(
        "SELECT metric_id,source_regime,COUNT(*) AS observations,"
        "SUM(availability='present') AS populated,"
        "SUM(validation='pass' AND availability='present') AS validated "
        "FROM observations GROUP BY metric_id,source_regime")]
    _compare_export_rows(
        "coverage_by_metric.csv", parsed_rows["coverage_by_metric.csv"],
        expected_metrics, ("metric_id", "source_regime"),
        OUTPUT_CONTRACT["coverage_by_metric.csv"].columns)

    applicability_columns = _table_columns(con, "applicability")
    applicability_rows = parsed_rows["applicability.csv"]
    if applicability_rows:
        missing_columns = sorted(set(applicability_columns) - set(applicability_rows[0]))
        if missing_columns:
            raise FinalRecordsRefused(
                f"applicability.csv omits database columns: {missing_columns}")
    expected_applicability = [dict(row) for row in con.execute(
        "SELECT * FROM applicability")]
    _compare_export_rows(
        "applicability.csv", applicability_rows, expected_applicability,
        ("form", "taxonomy_version", "concept_local"), applicability_columns)


def _validate_exports(root: pathlib.Path, con: sqlite3.Connection,
                      db_checks: Mapping[str, Any], publication: Mapping[str, Any]) -> Dict[str, Any]:
    files = publication["files"]
    missing = sorted((set(OUTPUT_CONTRACT) | set(JSON_OUTPUTS)) - set(files))
    if missing:
        raise FinalRecordsRefused(f"publication omits mandatory outputs: {missing}")
    row_counts: Dict[str, int] = {}
    headers: Dict[str, List[str]] = {}
    parsed_rows: Dict[str, List[dict]] = {}
    for name, spec in OUTPUT_CONTRACT.items():
        path = root / "exports" / name
        header, rows, _ = _csv_rows(path, f"required export {name}")
        absent = sorted(set(spec.columns) - set(header))
        if absent:
            raise FinalRecordsRefused(f"{name} lacks required columns: {absent}")
        n = len(rows)
        table_n = int(db_checks["table_counts"].get(spec.backing_table, 0))
        if spec.emptiness == "always" and n == 0:
            raise FinalRecordsRefused(f"required export is empty: {name}")
        if spec.emptiness == "if_table" and table_n and n == 0:
            raise FinalRecordsRefused(
                f"{name} is empty over populated table {spec.backing_table}")
        if spec.exact_rows and n != table_n:
            raise FinalRecordsRefused(
                f"{name} has {n} rows but {spec.backing_table} has {table_n}")
        row_counts[name] = n
        headers[name] = header
        parsed_rows[name] = rows
    json_values = {}
    for name, keys in JSON_OUTPUTS.items():
        _, body = _load_json(root / "exports" / name, f"required export {name}")
        if not isinstance(body, dict):
            raise FinalRecordsRefused(f"{name} is not a JSON object")
        absent = sorted(set(keys) - set(body))
        if absent:
            raise FinalRecordsRefused(f"{name} lacks required keys: {absent}")
        json_values[name] = body

    _critical_export_parity(root, con, parsed_rows)
    return {"row_counts": row_counts, "headers": headers, "json": json_values,
            "parity": "critical one-row consumer values independently matched"}


def _coverage_counts(con: sqlite3.Connection) -> Dict[str, Any]:
    rows = [dict(row) for row in con.execute(
        "SELECT e.slot_id,e.requirement,e.slot_state,e.source_health,"
        "e.denominator_origin,m.outcome,m.populated,m.source_matched,m.validated,"
        "m.in_review,m.candidates_refused FROM coverage_expected e JOIN "
        "coverage_measured m USING(slot_id) ORDER BY e.slot_id")]
    if not rows:
        raise FinalRecordsRefused("coverage population is empty")
    core = [r for r in rows if r["requirement"] in CORE_REQUIREMENTS]
    due = [r for r in core if r.get("slot_state") != FUTURE_SLOT]

    def sums(pool: Sequence[dict]) -> Dict[str, int]:
        return {name: sum(int(r.get(name) or 0) for r in pool)
                for name in ("populated", "source_matched", "validated", "in_review",
                             "candidates_refused")}

    core_sum, all_sum, due_sum = sums(core), sums(rows), sums(due)
    stats: Dict[str, Any] = {
        "expected_total": len(rows), "expected_core": len(core),
        "expected_core_due": len(due),
        "expected_not_required": sum(r["requirement"] == "NOT_REQUIRED" for r in rows),
        "expected_optional": sum(r["requirement"] == "OPTIONAL_OR_SUPPLEMENTAL"
                                 for r in rows),
        "expected_unknown": sum(r["requirement"] == "APPLICABILITY_UNKNOWN"
                                for r in rows),
        "slot_states": dict(collections.Counter(r.get("slot_state") or "unstated"
                                                for r in rows)),
        "source_health": dict(collections.Counter(r.get("source_health") or "unstated"
                                                  for r in rows)),
        "denominator_origin": dict(collections.Counter(
            r.get("denominator_origin") or "unstated" for r in rows)),
        "core_outcomes": dict(collections.Counter(r.get("outcome") or "missing"
                                                  for r in core)),
        "all_outcomes": dict(collections.Counter(r.get("outcome") or "missing"
                                                 for r in rows)),
    }
    for key, value in core_sum.items():
        stats["core_" + ("refused_candidates" if key == "candidates_refused" else key)] = value
    for key, value in all_sum.items():
        stats["all_" + ("refused_candidates" if key == "candidates_refused" else key)] = value
    stats["core_due_populated"] = due_sum["populated"]
    stats["core_due_validated"] = due_sum["validated"]
    if core:
        stats["core_populated_pct"] = round(100.0 * core_sum["populated"] / len(core), 4)
        stats["core_validated_pct"] = round(100.0 * core_sum["validated"] / len(core), 4)
    if due:
        stats["core_due_populated_pct"] = round(
            100.0 * due_sum["populated"] / len(due), 4)
        stats["core_due_validated_pct"] = round(
            100.0 * due_sum["validated"] / len(due), 4)
    return stats


def _compare_coverage_export(actual: Mapping[str, Any], exported: Mapping[str, Any]) -> None:
    for key, value in actual.items():
        if key not in exported:
            raise FinalRecordsRefused(f"coverage_statistics.json omits {key}")
        observed = exported[key]
        if isinstance(value, float):
            if not isinstance(observed, (int, float)) or abs(float(observed) - value) > 1e-9:
                raise FinalRecordsRefused(
                    f"coverage statistic {key} mismatch: database={value}, export={observed}")
        elif observed != value:
            raise FinalRecordsRefused(
                f"coverage statistic {key} mismatch: database={value!r}, export={observed!r}")


def _coverage_group_rows(con: sqlite3.Connection, key: str) -> List[dict]:
    rows = [dict(row) for row in con.execute(
        "SELECT e.*,m.outcome,m.populated,m.source_matched,m.validated,m.in_review,"
        "m.candidates_refused FROM coverage_expected e JOIN coverage_measured m "
        "USING(slot_id)")]
    grouped: Dict[str, List[dict]] = collections.defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "")].append(row)
    result = []
    for group, pool in sorted(grouped.items()):
        core = [row for row in pool if row["requirement"] in CORE_REQUIREMENTS]
        due = [row for row in core if row.get("slot_state") != FUTURE_SLOT]
        populated = sum(int(row.get("populated") or 0) for row in core)
        validated = sum(int(row.get("validated") or 0) for row in core)
        review = sum(int(row.get("in_review") or 0) for row in core)
        result.append({
            "group": group, "all_slots": len(pool), "core_slots": len(core),
            "core_due_slots": len(due), "populated": populated,
            "validated": validated, "in_review": review,
            "populated_pct": round(100.0 * populated / len(core), 4) if core else 0.0,
            "validated_pct": round(100.0 * validated / len(core), 4) if core else 0.0,
        })
    return result


def _validate_summary_exports(con: sqlite3.Connection,
                              json_values: Mapping[str, Mapping[str, Any]],
                              coverage: Mapping[str, Any]) -> None:
    groups = json_values["coverage_groups.json"]
    for output_key, db_key in (("by_template", "template"), ("by_metric", "metric_id")):
        if groups.get(output_key) != _coverage_group_rows(con, db_key):
            raise FinalRecordsRefused(f"coverage_groups.json {output_key} differs from database")
    adapter_rows = groups.get("by_adapter")
    if not isinstance(adapter_rows, list) or not adapter_rows:
        raise FinalRecordsRefused("coverage_groups.json by_adapter is empty or invalid")
    if len({str(row.get("group")) for row in adapter_rows}) != len(adapter_rows):
        raise FinalRecordsRefused("coverage_groups.json by_adapter has duplicate groups")
    summed = {field: sum(int(row.get(field) or 0) for row in adapter_rows)
              for field in ("all_slots", "core_slots", "core_due_slots", "populated",
                            "validated", "in_review")}
    expected = {"all_slots": coverage["expected_total"],
                "core_slots": coverage["expected_core"],
                "core_due_slots": coverage["expected_core_due"],
                "populated": coverage["core_populated"],
                "validated": coverage["core_validated"],
                "in_review": coverage["core_in_review"]}
    if summed != expected:
        raise FinalRecordsRefused(
            f"coverage_groups.json by_adapter totals differ: {summed} != {expected}")
    for row in adapter_rows:
        core = int(row.get("core_slots") or 0)
        for numerator, percentage in (("populated", "populated_pct"),
                                      ("validated", "validated_pct")):
            wanted = round(100.0 * int(row.get(numerator) or 0) / core, 4) if core else 0.0
            if row.get(percentage) != wanted:
                raise FinalRecordsRefused(
                    f"coverage_groups.json by_adapter {row.get('group')} {percentage} is wrong")

    field_rows = [dict(row) for row in con.execute("SELECT * FROM field_status")]
    by_outcome = dict(collections.Counter(row["outcome"] for row in field_rows))
    by_template: Dict[str, collections.Counter] = {}
    for row in field_rows:
        by_template.setdefault(row["template"], collections.Counter())[row["outcome"]] += 1
    expected_field = {
        "field_rows": len(field_rows),
        "distinct_metrics": len({row["metric_id"] for row in field_rows}),
        "by_outcome": by_outcome,
        "by_template": {key: dict(value) for key, value in sorted(by_template.items())},
        "rows_with_implemented_adapter": sum(bool(row["adapter_implemented"])
                                               for row in field_rows),
        "rows_with_local_data": sum(bool(row["has_data_in_template"])
                                    for row in field_rows),
        "rows_validated_locally": sum(bool(row["validated_in_template"])
                                      for row in field_rows),
        "readiness_basis": "within template, adapter, eligibility, period and quality gate",
    }
    actual_field = json_values["field_status_summary.json"]
    for key, value in expected_field.items():
        if actual_field.get(key) != value:
            raise FinalRecordsRefused(
                f"field_status_summary.json {key} differs from database/producer contract")


def _validate_crosswalk(root: pathlib.Path, con: sqlite3.Connection) -> Dict[str, Any]:
    req_header, req_rows, _ = _csv_rows(
        root / "config" / "requirements_crosswalk.csv", "requirements crosswalk")
    if len(req_rows) != 146 or "source_doc" not in req_header or "source_row" not in req_header:
        raise FinalRecordsRefused("requirements crosswalk is not the complete 146-row output")
    field_header, field_rows, _ = _csv_rows(
        root / "config" / "field_crosswalk_166.csv", "166-field crosswalk")
    required = {"source_doc", "source_row", "template", "disposition", "successors"}
    if not required.issubset(field_header) or len(field_rows) != 167:
        raise FinalRecordsRefused("field crosswalk does not contain the 167 reconciliation rows")
    audited = [r for r in field_rows if r.get("disposition") != "new"]
    if len(audited) != 166:
        raise FinalRecordsRefused("field crosswalk does not account for exactly 166 audit rows")
    bad = [r for r in field_rows if r.get("disposition") in {"lost", "partially_lost"}]
    if bad:
        raise FinalRecordsRefused("field crosswalk contains lost/partially_lost audit fields")
    targets = set()
    for row in field_rows:
        for successor in str(row.get("successors") or "").split(";"):
            successor = successor.strip()
            if successor:
                targets.add((row.get("template", ""), successor))
    db_targets = {(str(r[0]), str(r[1])) for r in con.execute(
        "SELECT template,field_id FROM field_status")}
    if targets != db_targets or len(targets) != 168:
        raise FinalRecordsRefused(
            f"field crosswalk targets differ from 168-row field status: "
            f"crosswalk={len(targets)}, database={len(db_targets)}")
    return {"requirements_rows": len(req_rows), "field_crosswalk_rows": len(field_rows),
            "audited_rows": len(audited), "target_keys": len(targets)}


def _validate_annotations(root: pathlib.Path, con: sqlite3.Connection) -> Dict[str, Any]:
    _, source = _load_json(root / "config" / "annotations" /
                           "reviewed_source_annotations.json", "annotation input")
    expected = source.get("annotations") if isinstance(source, dict) else None
    if not isinstance(expected, list) or len(expected) != 7:
        raise FinalRecordsRefused("annotation input does not contain exactly seven rows")
    key_cols = ("source_system", "entity_key", "filing_id", "source_fact_id", "metric_id")
    compare_cols = key_cols + ("filed_text", "review_status", "rationale", "evidence_ref",
                               "evidence_hash", "reviewer", "reviewed_at")
    expected_by_key = {tuple(str(r.get(k) or "") for k in key_cols): r for r in expected}
    if len(expected_by_key) != 7:
        raise FinalRecordsRefused("annotation input contains duplicate source identities")
    actual_rows = [dict(r) for r in con.execute(
        "SELECT * FROM reviewed_source_annotations ORDER BY source_system,entity_key,"
        "filing_id,source_fact_id,metric_id")]
    actual_by_key = {tuple(str(r.get(k) or "") for k in key_cols): r for r in actual_rows}
    if set(expected_by_key) != set(actual_by_key):
        raise FinalRecordsRefused("database annotation identities differ from required seven")
    for key, expected_row in expected_by_key.items():
        actual = actual_by_key[key]
        for col in compare_cols:
            if str(actual.get(col) or "") != str(expected_row.get(col) or ""):
                raise FinalRecordsRefused(f"annotation {key} differs in {col}")
    fayetteville = [r for r in actual_rows
                    if r["entity_key"] == "C001012" and str(r["filed_text"]) == "999999"]
    if len(fayetteville) != 4:
        raise FinalRecordsRefused("four Fayetteville 999999 annotations did not survive")
    warning_rows = []
    for ann in fayetteville:
        observations = [dict(r) for r in con.execute(
            "SELECT observation_id,value_text,qa_flags,review_status,validation FROM observations "
            "WHERE source_system=? AND filing_id=? AND source_fact_id=? AND metric_id=?",
            (ann["source_system"], ann["filing_id"], ann["source_fact_id"], ann["metric_id"]))]
        if not observations:
            raise FinalRecordsRefused(
                f"Fayetteville annotated fact has no observation: {ann['source_fact_id']}")
        for obs in observations:
            if str(obs.get("value_text")) != "999999" or not str(obs.get("qa_flags") or "").strip() \
                    or not str(obs.get("review_status") or "").strip():
                raise FinalRecordsRefused(
                    f"Fayetteville warning/value was stripped: {obs['observation_id']}")
            warning_rows.append(obs["observation_id"])
    return {"rows": len(actual_rows), "fayetteville_annotations": len(fayetteville),
            "fayetteville_observations_with_warning": len(warning_rows),
            "annotation_set_version": source.get("annotation_set_version")}


def _validate_a17(root: pathlib.Path) -> Dict[str, Any]:
    path = root / "verification" / "a17_reviewed_image_gate.json"
    raw, gate = _load_json(path, "A17 reviewed-image gate")
    if gate.get("schema") != "ferc-a17-reviewed-image-gate-v1" \
            or gate.get("status") != "verified" or gate.get("errors") != []:
        raise FinalRecordsRefused("A17 reviewed-image gate is not a clean verified result")
    summary = gate.get("summary") or {}
    if summary.get("required_sources") != 2 or summary.get("verified_sources") != 2 \
            or summary.get("failed_sources") != 0 or summary.get("error_count") != 0:
        raise FinalRecordsRefused("A17 gate does not verify exactly two required sources")
    claim = gate.get("claim_boundary") or {}
    if claim.get("network_used") is not False or claim.get("ocr_used") is not True \
            or claim.get("automatic_image_value_extraction_claimed") is not True \
            or claim.get("renders_retained") is not False:
        raise FinalRecordsRefused("A17 gate does not preserve its offline OCR claim boundary")
    fixture_path = root / "evidence" / "test_fixtures" / "reviewed_image_sources.json"
    _, fixture = _load_json(fixture_path, "A17 independent review fixture")
    if not isinstance(fixture, dict) or fixture.get("schema") != "ferc-reviewed-image-sources-v1":
        raise FinalRecordsRefused("A17 independent review fixture schema is invalid")
    fixture_sources = fixture.get("sources") or []
    if len(fixture_sources) != 2:
        raise FinalRecordsRefused("A17 independent review fixture does not have two sources")
    fixture_by_accession = {str(s.get("accession")): s for s in fixture_sources}
    if len(fixture_by_accession) != 2:
        raise FinalRecordsRefused("A17 independent review fixture has duplicate accessions")

    inputs = gate.get("inputs") or {}
    input_paths = {
        "source_cache_index": root / "source_cache" / "index.json",
        "review_fixture": fixture_path,
        "capacity_adapter": root / "adapters" / "capacity.py",
        "image_ocr_module": root / "ferclib" / "image_ocr.py",
        "vision_ocr_source": root / "tools" / "vision_ocr.swift",
    }
    input_identities = {}
    for name, actual_path in input_paths.items():
        declared = inputs.get(name)
        if not isinstance(declared, dict):
            raise FinalRecordsRefused(f"A17 gate omits input identity {name}")
        identity = _expected_identity(actual_path, declared, f"A17 {name}")
        identity["path"] = str(actual_path.relative_to(root))
        input_identities[name] = identity
    _, cache_index = _load_json(input_paths["source_cache_index"], "A17 cache index")
    if not isinstance(cache_index, dict):
        raise FinalRecordsRefused("A17 cache index is not an object")
    sources = gate.get("sources") or []
    if len(sources) != 2 or any(s.get("status") != "verified" for s in sources):
        raise FinalRecordsRefused("A17 per-source verification is incomplete")
    if {str(s.get("accession")) for s in sources} != set(fixture_by_accession):
        raise FinalRecordsRefused("A17 gate and independent fixture accession sets differ")
    verified = []
    for source in sources:
        accession = str(source.get("accession"))
        fixed = fixture_by_accession[accession]
        source_hash = _require_text(fixed.get("source_object_sha256"),
                                    f"A17 fixture {accession} source hash")
        source_bytes = fixed.get("source_bytes")
        if not HEX64.fullmatch(source_hash) or not isinstance(source_bytes, int):
            raise FinalRecordsRefused(f"A17 fixture {accession} source identity is invalid")
        canonical = root / "source_cache" / "objects" / source_hash[:2] / source_hash
        source_identity = _expected_identity(
            canonical, {"sha256": source_hash, "bytes": source_bytes},
            f"A17 source object {accession}")
        if not _read(canonical, f"A17 source object {accession}").startswith(b"%PDF-"):
            raise FinalRecordsRefused(f"A17 source object {accession} lacks PDF magic")
        declared_source = source.get("source_object") or {}
        if declared_source.get("sha256") != source_hash \
                or declared_source.get("bytes") != source_bytes \
                or declared_source.get("pdf_magic_valid") is not True:
            raise FinalRecordsRefused(f"A17 gate source identity differs for {accession}")
        matches = [record for record in cache_index.values() if isinstance(record, dict)
                   and record.get("content_hash") == source_hash]
        if not matches or any(record.get("byte_size") != source_bytes for record in matches):
            raise FinalRecordsRefused(f"A17 source {accession} is not correctly cache-indexed")
        render = source.get("page_1_render") or {}
        if render.get("sha256") != fixed.get("render_sha256") \
                or render.get("signature_valid") is not True \
                or render.get("crc_valid") is not True \
                or not isinstance(render.get("width"), int) or render.get("width") <= 0 \
                or not isinstance(render.get("height"), int) or render.get("height") <= 0 \
                or render.get("render_retained") is not False:
            raise FinalRecordsRefused(f"A17 rendered-page evidence is invalid for {accession}")
        pipeline = source.get("pipeline_extractor") or {}
        automatic = source.get("automatic_ocr") or {}
        review = source.get("reviewed_transcription") or {}
        if pipeline.get("rows") != 0 or pipeline.get("text_layer") is not False:
            raise FinalRecordsRefused(f"A17 source {accession} is not proven image-only")
        if not isinstance(automatic.get("rows"), int) or automatic.get("rows") <= 0 \
                or automatic.get("credential_environment_forwarded") is not False:
            raise FinalRecordsRefused(f"A17 automatic OCR did not run safely for {accession}")
        if review.get("manual_review") is not True or review.get("ocr_used") is not True \
                or review.get("ocr_checked_against_manual_review") is False:
            raise FinalRecordsRefused(f"A17 OCR/manual-review agreement is absent for {accession}")
        expected_figures = {(str(v.get("kind")), str(v.get("value_text")),
                             str(v.get("unit")), str(v.get("qualifier")))
                            for v in fixed.get("expected") or []}
        actual_figures = {(str(v.get("kind")), str(v.get("value_text")),
                           str(v.get("unit")), str(v.get("qualifier")))
                          for v in review.get("figures") or []}
        if len(expected_figures) != 2 or actual_figures != expected_figures:
            raise FinalRecordsRefused(f"A17 reviewed OCR values differ for {accession}")
        verified.append({"accession": accession, "source_object": source_identity,
                         "render_sha256": render["sha256"],
                         "ocr_rows": automatic["rows"],
                         "figures": sorted(actual_figures)})
    return {"identity": {"path": "verification/a17_reviewed_image_gate.json",
                         "bytes": len(raw), "sha256": _sha(raw)},
            "summary": summary, "claim_boundary": claim,
            "input_identities": input_identities, "verified_sources": verified,
            "accessions": sorted(str(s.get("accession")) for s in sources)}


def _validation_results(path: pathlib.Path, root: pathlib.Path) -> Dict[str, Any]:
    raw, body = _load_json(path, "integrated validation results")
    if not isinstance(body, dict) or not isinstance(body.get("summary"), dict) \
            or not isinstance(body.get("results"), list):
        raise FinalRecordsRefused("validation results schema is invalid")
    statuses = ("PASS", "FAIL", "ERROR", "SKIPPED")
    if set(body["summary"]) != set(statuses):
        raise FinalRecordsRefused("validation summary has missing or unknown status keys")
    unknown = [r for r in body["results"] if r.get("status") not in statuses]
    if unknown:
        raise FinalRecordsRefused("validation contains unknown result statuses")
    counted = {s: sum(1 for r in body["results"] if r.get("status") == s)
               for s in statuses}
    if any(body["summary"].get(s) != counted[s] for s in statuses):
        raise FinalRecordsRefused("validation summary does not match its result rows")
    if counted["PASS"] <= 0 or counted["FAIL"] or counted["ERROR"]:
        raise FinalRecordsRefused(f"integrated validation did not pass: {counted}")
    if counted["SKIPPED"]:
        raise FinalRecordsRefused(
            "integrated validation contains skipped controls; classify optional references in "
            "the verbose test manifest instead of skipping a production validation gate")
    bad = [r for r in body["results"] if not str(r.get("check") or "").strip()
           or not str(r.get("group") or "").strip()]
    if bad:
        raise FinalRecordsRefused("validation contains unnamed/unclassified result rows")
    return {
        "identity": {"path": str(path.relative_to(root)), "bytes": len(raw),
                     "sha256": _sha(raw)},
        "summary": counted,
        "skipped": [r for r in body["results"] if r.get("status") == "SKIPPED"],
        "result_count": len(body["results"]),
    }


def _parse_test_log(raw: bytes, label: str) -> Dict[str, Any]:
    try:
        parsed = _parse_shared_unittest_log(raw, label)
    except UnittestLogRefused as exc:
        raise FinalRecordsRefused(str(exc)) from None
    counts = dict(parsed["counts"])
    counts["result_success"] = parsed["successful"]
    return {"counts": counts, "results": parsed["results"],
            "text": parsed["text"]}


def _validated_test_evidence_map(value: Any, label: str, population: set,
                                 successful_test_ids: set) -> Dict[str, List[str]]:
    if not isinstance(value, dict) or set(value) != population:
        actual = set(value) if isinstance(value, dict) else set()
        raise FinalRecordsRefused(
            f"{label} does not cover exact record population: "
            f"missing={sorted(population-actual)}, extra={sorted(actual-population)}")
    result: Dict[str, List[str]] = {}
    for record_id in sorted(population):
        tests = _require_list(value.get(record_id), f"{label}.{record_id}")
        if not all(isinstance(test, str) and test.strip() for test in tests):
            raise FinalRecordsRefused(f"{label}.{record_id} has an invalid test id")
        if len(tests) != len(set(tests)):
            raise FinalRecordsRefused(f"{label}.{record_id} repeats a test id")
        absent = sorted(set(tests) - successful_test_ids)
        if absent:
            raise FinalRecordsRefused(
                f"{label}.{record_id} cites tests not observed passing: {absent}")
        result[record_id] = sorted(tests)
    return result


def _validate_test_runs(manifest_path: pathlib.Path, logs_dir: pathlib.Path,
                        issue_ids: set, exception_ids: set,
                        root: pathlib.Path) -> Dict[str, Any]:
    raw, manifest = _load_json(manifest_path, "final test-run manifest")
    if not isinstance(manifest, dict) \
            or manifest.get("schema") != "ferc-final-test-run-manifest-v2":
        raise FinalRecordsRefused("test-run manifest schema is not final v2")
    tested_tree_snapshot = _test_tool_snapshot(root)
    if manifest.get("tested_tree_snapshot") != tested_tree_snapshot:
        raise FinalRecordsRefused(
            "test-run manifest does not bind the current tests/acceptance/tools tree")
    runs = _require_list(manifest.get("runs"), "test-run manifest.runs")
    seen_run_ids = set()
    complete_success = 0
    successful_test_ids: set = set()
    records = []
    for number, run in enumerate(runs, 1):
        if not isinstance(run, dict):
            raise FinalRecordsRefused(f"test run {number} is not an object")
        run_id = _require_text(run.get("run_id"), f"test run {number}.run_id")
        if run_id in seen_run_ids:
            raise FinalRecordsRefused(f"duplicate test run id: {run_id}")
        seen_run_ids.add(run_id)
        role = run.get("acceptance_role")
        if role not in ("acceptance", "historical_negative_control"):
            raise FinalRecordsRefused(f"test run {run_id} has invalid acceptance_role")
        log_rel = pathlib.Path(_require_text(run.get("log"), f"test run {run_id}.log"))
        log_path = log_rel if log_rel.is_absolute() else logs_dir / log_rel
        log_path = _path_within(log_path, logs_dir, f"test log {run_id}")
        log_raw = _read(log_path, f"test log {run_id}")
        _expected_identity(log_path, run, f"test log {run_id}")
        parsed = _parse_test_log(log_raw, f"test log {run_id}")
        parsed_counts = parsed["counts"]
        declared = run.get("counts")
        required_count_keys = {"collected", "executed", "pass", "fail", "error",
                               "skip", "xfail", "xpass"}
        if not isinstance(declared, dict) or set(declared) != required_count_keys:
            raise FinalRecordsRefused(
                f"test run {run_id} counts must have exactly {sorted(required_count_keys)}")
        for key in required_count_keys:
            if not isinstance(declared[key], int) or declared[key] < 0 \
                    or declared[key] != parsed_counts[key]:
                raise FinalRecordsRefused(
                    f"test run {run_id} declared {key}={declared.get(key)!r}, "
                    f"log reports {parsed_counts[key]}")
        if declared["collected"] == 0 or declared["executed"] == 0:
            raise FinalRecordsRefused(f"test run {run_id} is a zero-case run")
        skip_details = run.get("skip_details", [])
        xfail_details = run.get("xfail_details", [])
        if not isinstance(skip_details, list) or len(skip_details) != declared["skip"]:
            raise FinalRecordsRefused(f"test run {run_id} does not classify every skip")
        if not isinstance(xfail_details, list) or len(xfail_details) != declared["xfail"]:
            raise FinalRecordsRefused(f"test run {run_id} does not classify every xfail")
        for detail in skip_details:
            if not isinstance(detail, dict) or not str(detail.get("test") or "").strip() \
                    or not str(detail.get("classification") or "").strip() \
                    or not str(detail.get("reason") or "").strip():
                raise FinalRecordsRefused(f"test run {run_id} has an incomplete skip record")
            if detail.get("required") is not False:
                raise FinalRecordsRefused(
                    f"test run {run_id} skipped required capability {detail.get('test')}")
            if parsed["results"].get(str(detail.get("test"))) != "skip":
                raise FinalRecordsRefused(
                    f"test run {run_id} skip detail does not name an observed skipped test")
        for detail in xfail_details:
            if not isinstance(detail, dict) or not str(detail.get("test") or "").strip() \
                    or not str(detail.get("classification") or "").strip() \
                    or not str(detail.get("reason") or "").strip():
                raise FinalRecordsRefused(f"test run {run_id} has an incomplete xfail record")
            if detail.get("required") is not False:
                raise FinalRecordsRefused(
                    f"test run {run_id} expected-failed required capability {detail.get('test')}")
            if parsed["results"].get(str(detail.get("test"))) != "xfail":
                raise FinalRecordsRefused(
                    f"test run {run_id} xfail detail does not name an observed expected failure")

        exit_code = run.get("exit_code")
        if not isinstance(exit_code, int):
            raise FinalRecordsRefused(f"test run {run_id} has no integer exit_code")
        command = run.get("command")
        if not ((isinstance(command, str) and command.strip())
                or (isinstance(command, list) and command
                    and all(isinstance(v, str) and v for v in command))):
            raise FinalRecordsRefused(f"test run {run_id} has no exact command")
        _require_text(run.get("interpreter"), f"test run {run_id}.interpreter")
        if role == "acceptance":
            if exit_code != 0 or not parsed_counts["result_success"]:
                raise FinalRecordsRefused(
                    f"acceptance test run {run_id} did not succeed (exit={exit_code})")
            successful_test_ids.update(
                test_id for test_id, status in parsed["results"].items() if status == "pass")
            if run.get("complete_suite") is True and run.get("kind") == "complete_suite":
                command_text = " ".join(command) if isinstance(command, list) else command
                if declared["executed"] < MIN_COMPLETE_SUITE_CASES \
                        or "unittest" not in command_text or "discover" not in command_text \
                        or not (" -v" in " " + command_text or "--verbose" in command_text):
                    raise FinalRecordsRefused(
                        f"test run {run_id} is not a verbose complete-suite invocation with at "
                        f"least {MIN_COMPLETE_SUITE_CASES} executed cases")
                complete_success += 1
        else:
            if exit_code == 0 and parsed_counts["result_success"]:
                raise FinalRecordsRefused(
                    f"historical negative control {run_id} did not demonstrate a bad baseline")
        records.append({
            "run_id": run_id, "kind": run.get("kind"), "acceptance_role": role,
            "complete_suite": run.get("complete_suite") is True,
            "log_identity": {"path": str(log_path.relative_to(logs_dir)),
                             "bytes": len(log_raw), "sha256": _sha(log_raw)},
            "command": command, "interpreter": run["interpreter"],
            "exit_code": exit_code, "counts": declared,
            "skip_details": skip_details, "xfail_details": xfail_details,
            "observed_test_ids": sorted(parsed["results"]),
        })
    if not complete_success:
        raise FinalRecordsRefused("no successful non-zero complete candidate suite is recorded")
    issue_map = _validated_test_evidence_map(
        manifest.get("issue_test_evidence"), "issue_test_evidence",
        issue_ids, successful_test_ids)
    exception_map = _validated_test_evidence_map(
        manifest.get("exception_test_evidence"), "exception_test_evidence",
        exception_ids, successful_test_ids)
    return {
        "manifest_identity": {"path": str(manifest_path.relative_to(root)),
                              "bytes": len(raw), "sha256": _sha(raw)},
        "tested_tree_snapshot": tested_tree_snapshot,
        "runs": records,
        "successful_complete_suites": complete_success,
        "issue_test_evidence": issue_map,
        "exception_test_evidence": exception_map,
    }


def _filing_application(con: sqlite3.Connection, accession: Optional[str] = None,
                        entity: Optional[str] = None, year: Optional[int] = None,
                        regime_hint: Optional[str] = None) -> Dict[str, Any]:
    clauses, params = [], []
    if accession:
        clauses.append("accession_number=?")
        params.append(accession)
    if entity:
        clauses.append("entity_key=?")
        params.append(entity)
    if year is not None:
        clauses.append("reporting_year=?")
        params.append(year)
    where = " AND ".join(clauses) or "1=1"
    filings = int(con.execute(f"SELECT COUNT(*) FROM filings WHERE {where}", params).fetchone()[0])
    observations = int(con.execute(
        f"SELECT COUNT(*) FROM observations WHERE {where}" +
        (" AND source_regime LIKE ?" if regime_hint else ""),
        params + (["%" + regime_hint + "%"] if regime_hint else [])).fetchone()[0])
    qualified_where = " AND ".join("f." + clause for clause in clauses) or "1=0"
    source_facts = int(con.execute(
        "SELECT COUNT(*) FROM source_facts sf JOIN filings f "
        "ON f.source_system=sf.source_system AND f.filing_id=sf.filing_id WHERE " +
        qualified_where, params).fetchone()[0])
    observation_where = " AND ".join("o." + clause for clause in clauses) or "1=0"
    lineage_edges = int(con.execute(
        "SELECT COUNT(*) FROM lineage_edges l JOIN observations o "
        "ON o.observation_id=l.observation_id WHERE " + observation_where,
        params).fetchone()[0])
    coverage_slots = int(con.execute(
        "SELECT COUNT(*) FROM coverage_measured m JOIN observations o "
        "ON o.observation_id=m.observation_id WHERE " + observation_where,
        params).fetchone()[0])
    # Documents/events have accession and entity but not reporting_year in both
    # tables; use the strongest common predicates without inventing a join.
    doc_clauses, doc_params = [], []
    if accession:
        doc_clauses.append("accession_number=?")
        doc_params.append(accession)
    documents = int(con.execute(
        "SELECT COUNT(*) FROM documents WHERE " + (" AND ".join(doc_clauses) or "1=0"),
        doc_params).fetchone()[0])
    event_clauses, event_params = [], []
    if accession:
        event_clauses.append("accession_number=?")
        event_params.append(accession)
    if entity:
        event_clauses.append("entity_key=?")
        event_params.append(entity)
    events = int(con.execute(
        "SELECT COUNT(*) FROM events WHERE " + (" AND ".join(event_clauses) or "1=0"),
        event_params).fetchone()[0])
    return {"filings": filings, "source_facts": source_facts,
            "observations": observations, "lineage_edges": lineage_edges,
            "coverage_slots": coverage_slots, "documents": documents, "events": events}


def _parse_scope(scope: str) -> Tuple[Optional[str], Optional[int]]:
    match = re.search(r"(C[0-9]{6})(?::([0-9]{4}))?", scope or "")
    if not match:
        return None, None
    return match.group(1), int(match.group(2)) if match.group(2) else None


def _verification(label: str, checks: Mapping[str, Any], failures: Sequence[str],
                  counterevidence: Optional[str] = None) -> Dict[str, Any]:
    """Return JSON-safe, non-throwing evidence for one exact exception gate."""
    result = {
        "gate": label,
        "accepted": not failures,
        "checks": dict(checks),
        "failures": list(failures),
    }
    if counterevidence:
        result["counterevidence"] = counterevidence
    return result


def _expect_one(rows: Sequence[sqlite3.Row], label: str,
                failures: List[str]) -> Optional[sqlite3.Row]:
    if len(rows) != 1:
        failures.append(f"{label}: expected exactly one row, found {len(rows)}")
        return None
    return rows[0]


def _as_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"not a decimal: {value!r}") from None


def _json_mapping(value: Any, label: str, failures: List[str]) -> Dict[str, Any]:
    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        failures.append(f"{label}: invalid JSON")
        return {}
    if not isinstance(result, dict):
        failures.append(f"{label}: expected a JSON object")
        return {}
    return result


def _lineage_failures(con: sqlite3.Connection, observations: Sequence[sqlite3.Row],
                      require_ioc_populations: bool = False,
                      filing_id: Optional[str] = None) -> Tuple[List[str], int]:
    """Resolve every direct lineage identity without importing production code."""
    failures: List[str] = []
    edge_count = 0
    for observation in observations:
        observation_id = str(observation["observation_id"])
        edges = con.execute(
            "SELECT * FROM lineage_edges WHERE observation_id=? ORDER BY input_order",
            (observation_id,)).fetchall()
        edge_count += len(edges)
        if not edges:
            failures.append(f"{observation_id}: no lineage edges")
        for edge in edges:
            identities = 0
            resolved = 0
            if edge["input_source_fact_id"]:
                identities += 1
                resolved += int(con.execute(
                    "SELECT COUNT(*) FROM source_facts WHERE source_system=? "
                    "AND filing_id=? AND source_fact_id=?",
                    (edge["input_source_system"], edge["input_filing_id"],
                     edge["input_source_fact_id"])).fetchone()[0] == 1)
            if edge["input_population_id"]:
                identities += 1
                resolved += int(con.execute(
                    "SELECT COUNT(*) FROM lineage_populations "
                    "WHERE population_id=? AND observation_id=?",
                    (edge["input_population_id"], observation_id)).fetchone()[0] == 1)
            if edge["input_observation_id"]:
                identities += 1
                resolved += int(con.execute(
                    "SELECT COUNT(*) FROM observations WHERE observation_id=?",
                    (edge["input_observation_id"],)).fetchone()[0] == 1)
            if not identities:
                failures.append(f"{observation_id}: lineage edge {edge['input_order']} "
                                "has no input identity")
            elif not resolved:
                failures.append(f"{observation_id}: lineage edge {edge['input_order']} "
                                "does not resolve")
        if require_ioc_populations and observation["metric_id"] != "ioc_mdq_change":
            populations = con.execute(
                "SELECT filing_ids,row_count,candidate_count,excluded_count "
                "FROM lineage_populations WHERE observation_id=?", (observation_id,)
            ).fetchall()
            if not populations:
                failures.append(f"{observation_id}: no IOC lineage population")
            for population in populations:
                try:
                    filing_ids = json.loads(population["filing_ids"])
                except (TypeError, json.JSONDecodeError):
                    filing_ids = []
                if filing_id not in filing_ids:
                    failures.append(f"{observation_id}: population omits {filing_id}")
                if (population["candidate_count"] < population["row_count"] or
                        population["candidate_count"] - population["row_count"] !=
                        population["excluded_count"]):
                    failures.append(f"{observation_id}: population arithmetic is inconsistent")
    return failures, edge_count


def _matching_blockers(con: sqlite3.Connection, exception_id: str, adapter: str,
                       scope: str) -> Tuple[List[dict], List[dict]]:
    """Find both a historical ID and replacement blockers at the same scope."""
    rows = con.execute(
        "SELECT blocker_id,adapter,scope,kind,summary,attempts,exact_error,"
        "human_decision_needed,resolved_at FROM blockers "
        "WHERE blocker_id=? OR (adapter=? AND scope=?) ORDER BY blocker_id",
        (exception_id, adapter, scope)).fetchall()
    records = [dict(row) for row in rows]
    unresolved = [row for row in records if not row.get("resolved_at")]
    return records, unresolved


def _verify_ioc_exception(con: sqlite3.Connection, exception_id: str) -> Dict[str, Any]:
    expected = IOC_EXCEPTION_EXPECTATIONS[exception_id]
    (accession, defect, entity, source_hash, source_bytes, encoding,
     rows_total, fact_count, observation_count, is_canonical, version_status) = expected
    failures: List[str] = []
    checks: Dict[str, Any] = {
        "accession_number": accession, "expected_entity": entity,
        "defect_branch": defect, "expected_source_hash": source_hash,
        "expected_source_bytes": source_bytes, "expected_source_facts": fact_count,
        "expected_observations": observation_count,
    }
    filing = _expect_one(con.execute(
        "SELECT * FROM filings WHERE source_system='eLibrary' AND filing_id=? "
        "AND accession_number=?", (accession, accession)).fetchall(),
        f"IOC filing {accession}", failures)
    if filing is not None:
        for field, wanted in (("entity_key", entity), ("form", "Form 549B IOC"),
                              ("content_hash", source_hash),
                              ("is_canonical", is_canonical),
                              ("version_status", version_status)):
            if filing[field] != wanted:
                failures.append(f"{accession}: filing {field}={filing[field]!r}, "
                                f"expected {wanted!r}")
    document = _expect_one(con.execute(
        "SELECT * FROM documents WHERE source_system='eLibrary' AND filing_id=? "
        "AND content_hash=?", (accession, source_hash)).fetchall(),
        f"IOC document {accession}", failures)
    if document is not None:
        if document["byte_size"] != source_bytes:
            failures.append(f"{accession}: document byte size differs")
        if document["availability"] != "retrieved":
            failures.append(f"{accession}: document is not retrieved")

    facts = con.execute(
        "SELECT * FROM source_facts WHERE source_system='eLibrary' AND filing_id=?",
        (accession,)).fetchall()
    checks["actual_source_facts"] = len(facts)
    if len(facts) != fact_count:
        failures.append(f"{accession}: {len(facts)} source facts, expected {fact_count}")
    headers = [fact for fact in facts if fact["concept_local"] == "ioc_header_record"]
    header = _expect_one(headers, f"IOC header {accession}", failures)
    if header is not None:
        meta = _json_mapping(header["typed_dims_json"], f"IOC header {accession}", failures)
        gate = meta.get("entity_gate") if isinstance(meta.get("entity_gate"), dict) else {}
        provenance = (meta.get("parse_provenance")
                      if isinstance(meta.get("parse_provenance"), dict) else {})
        for label, actual, wanted in (
                ("normalised CID", meta.get("pipeline_id_normalised"), entity),
                ("native header CID", gate.get("header_cid"), entity),
                ("requested entity", gate.get("requested_entity"), entity),
                ("entity gate result", gate.get("result"), "accepted"),
                ("source hash", provenance.get("content_sha256"), source_hash),
                ("source bytes", provenance.get("byte_size"), source_bytes),
                ("encoding", provenance.get("encoding"), encoding),
                ("decoded row count", provenance.get("rows_total"), rows_total),
                ("replacement characters", provenance.get("replaced_characters"), 0)):
            if actual != wanted:
                failures.append(f"{accession}: {label}={actual!r}, expected {wanted!r}")
        counts = provenance.get("counts")
        if not isinstance(counts, dict) or sum(
                value for value in counts.values() if isinstance(value, int)) != fact_count:
            failures.append(f"{accession}: parse counts do not equal persisted facts")
        repair = provenance.get("header_repair")
        repair = repair if isinstance(repair, dict) else {}
        expected_storage_state = "blank" if entity in {"C000830", "C001031"} else "stated"
        expected_storage_unit = "" if expected_storage_state == "blank" else "Dth"
        checks["header_units"] = {
            "transport_state": meta.get("uom_transport_state"),
            "transport_unit": meta.get("uom_transport"),
            "storage_state": meta.get("uom_storage_state"),
            "storage_unit": meta.get("uom_storage"),
        }
        if (meta.get("uom_transport_state") != "stated"
                or meta.get("uom_transport") != "Dth"
                or meta.get("uom_storage_state") != expected_storage_state
                or str(meta.get("uom_storage") or "") != expected_storage_unit):
            failures.append(f"{accession}: exact header unit state is not preserved")
        if defect == "shifted_header":
            if (repair.get("applied") is not True
                    or repair.get("transformation") != "drop_empty_field_at_index_1"
                    or repair.get("original_bytes_retained") is not True
                    or "H (header) record only" not in str(repair.get("scope", ""))):
                failures.append(f"{accession}: shifted-header repair provenance is incomplete")
        elif repair.get("applied") not in (False, None):
            failures.append(f"{accession}: an unrequired header repair was recorded")
        if defect == "utf8_bom" and provenance.get("bom") != "UTF-8":
            failures.append(f"{accession}: UTF-8 BOM branch is not evidenced")
        if defect == "utf16_le" and provenance.get("bom") != "UTF-16 LE":
            failures.append(f"{accession}: UTF-16 LE branch is not evidenced")
        if defect == "preamble" and (
                provenance.get("rows_blank") != 5 or (counts or {}).get("?") != 1):
            failures.append(f"{accession}: preamble branch counts are not exact")

    observations = con.execute(
        "SELECT * FROM observations WHERE filing_id=? ORDER BY observation_id",
        (accession,)).fetchall()
    checks["actual_observations"] = len(observations)
    checks["present_numeric_unitless_observations"] = sum(
        observation["availability"] == "present"
        and observation["value_num"] is not None
        and not str(observation["unit"] or "").strip()
        for observation in observations)
    if checks["present_numeric_unitless_observations"]:
        failures.append(f"{accession}: present numeric observations have no supported unit")
    if len(observations) != observation_count:
        failures.append(f"{accession}: {len(observations)} observations, "
                        f"expected {observation_count}")
    for observation in observations:
        if (observation["entity_key"] != entity
                or observation["source_system"] != "eLibrary"
                or observation["version_status"] != version_status):
            failures.append(f"{accession}: observation {observation['observation_id']} "
                            "has wrong owner/source/version")
    lineage_errors, edge_count = _lineage_failures(
        con, observations, require_ioc_populations=bool(observation_count),
        filing_id=accession)
    failures.extend(lineage_errors)
    checks["resolved_lineage_edges"] = edge_count

    if accession == "20250102-5120":
        replacement = _expect_one(con.execute(
            "SELECT * FROM filings WHERE source_system='eLibrary' "
            "AND filing_id='20250114-5047' AND accession_number='20250114-5047'"
        ).fetchall(), "IOC replacement 20250114-5047", failures)
        if filing is not None and replacement is not None:
            if (replacement["entity_key"] != entity
                    or replacement["snapshot_date"] != filing["snapshot_date"]
                    or replacement["is_canonical"] != 1
                    or replacement["version_status"] != "revised"
                    or replacement["supersedes_filing_id"] != accession):
                failures.append(f"{accession}: later revision relationship is not exact")
        replacement_observations = con.execute(
            "SELECT * FROM observations WHERE filing_id='20250114-5047'"
        ).fetchall()
        if len(replacement_observations) != 17:
            failures.append(f"{accession}: later revision has "
                            f"{len(replacement_observations)} observations, expected 17")
        for observation in replacement_observations:
            if (observation["entity_key"] != entity
                    or observation["source_system"] != "eLibrary"
                    or observation["version_status"] != "revised"):
                failures.append(f"{accession}: later-revision observation "
                                f"{observation['observation_id']} has wrong owner/source/version")
        replacement_lineage, _ = _lineage_failures(
            con, replacement_observations, require_ioc_populations=True,
            filing_id="20250114-5047")
        failures.extend(replacement_lineage)
        if con.execute("SELECT COUNT(*) FROM lineage_edges WHERE input_filing_id=?",
                       (accession,)).fetchone()[0]:
            failures.append(f"{accession}: noncanonical occurrence is used by lineage")
        for population in con.execute("SELECT population_id,filing_ids FROM lineage_populations"):
            try:
                population_filings = json.loads(population["filing_ids"])
            except (TypeError, json.JSONDecodeError):
                population_filings = []
            if accession in population_filings:
                failures.append(f"{accession}: noncanonical occurrence appears in population "
                                f"{population['population_id']}")
                break

    counterevidence = (
        f"The exact {accession} occurrence decoded into {len(facts)} persisted source facts "
        f"under native CID {entity}; canonical output count is {len(observations)}."
    )
    if accession == "20250102-5120":
        counterevidence += (
            " Zero observations is the expected result for this parsed original-but-"
            "noncanonical occurrence; 20250114-5047 is the source-supported canonical "
            "revision. This occurrence ordering is reproducible counterevidence to the "
            "audit draft's stale 'revised' label for 20250102-5120."
        )
    return _verification("exact_ioc_occurrence", checks, failures, counterevidence)


def _verify_ioc_access_exception(con: sqlite3.Connection,
                                 exception_id: str) -> Dict[str, Any]:
    accession, entity, replacement_id, snapshot_date, expected_observations = \
        IOC_ACCESS_EXPECTATIONS[exception_id]
    failures: List[str] = []
    checks: Dict[str, Any] = {
        "metadata_only_accession": accession, "entity_key": entity,
        "public_replacement": replacement_id, "snapshot_date": snapshot_date,
        "expected_replacement_observations": expected_observations,
    }
    for table in ("filings", "documents", "observations"):
        count = int(con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE accession_number=?", (accession,)
        ).fetchone()[0])
        checks[f"metadata_occurrence_{table}"] = count
        if count:
            failures.append(f"{accession}: metadata-N occurrence invented {table} rows")
    event = _expect_one(con.execute(
        "SELECT * FROM events WHERE accession_number=? "
        "AND event_type='metadata_declared_not_public'", (accession,)).fetchall(),
        f"metadata-N event {accession}", failures)
    if event is not None:
        text = f"{event['headline']} {event['detail']}".casefold()
        if event["entity_key"] != entity:
            failures.append(f"{accession}: metadata event has wrong entity")
        for phrase in ("no request was made", "no http status observed", "erroneously filed"):
            if phrase not in text:
                failures.append(f"{accession}: metadata event omits {phrase!r}")
        if "http 401" in text or "status 401" in text:
            failures.append(f"{accession}: metadata event invents HTTP 401")

    blockers = con.execute(
        "SELECT * FROM blockers WHERE adapter='ioc' AND scope=?", (f"{entity}:{accession}",)
    ).fetchall()
    blocker = _expect_one(blockers, f"metadata-N blocker {accession}", failures)
    if blocker is not None:
        if blocker["kind"] != "access" or str(blocker["attempts"]) != "0":
            failures.append(f"{accession}: access blocker does not record zero attempts")
        prefix = str(blocker["exact_error"] or "").split("\nRESOLUTION:", 1)[0]
        payload = _json_mapping(prefix, f"metadata-N blocker {accession}", failures)
        for field, wanted in (("metadata_availability_code", "N"),
                              ("http_request_attempted", False),
                              ("http_status_observed", None),
                              ("confidentiality_established", False)):
            if payload.get(field) != wanted:
                failures.append(f"{accession}: blocker {field}={payload.get(field)!r}, "
                                f"expected {wanted!r}")
        text = f"{blocker['summary']} {blocker['exact_error']}".casefold()
        if "http 401" in text or "status 401" in text:
            failures.append(f"{accession}: blocker invents HTTP 401")
        satisfied = payload.get("period_satisfied_by")
        if not isinstance(satisfied, list) or [x.get("filing_id") for x in satisfied
                                               if isinstance(x, dict)] != [replacement_id]:
            failures.append(f"{accession}: replacement identity is absent from blocker evidence")
        elif (satisfied[0].get("is_canonical") != 1
              or satisfied[0].get("snapshot_date") != snapshot_date):
            failures.append(f"{accession}: blocker replacement state is incorrect")
        checks["scope_blocker_resolved"] = bool(blocker["resolved_at"])

    replacement = _expect_one(con.execute(
        "SELECT * FROM filings WHERE source_system='eLibrary' AND filing_id=?",
        (replacement_id,)).fetchall(), f"public replacement {replacement_id}", failures)
    if replacement is not None:
        if (replacement["entity_key"] != entity
                or replacement["snapshot_date"] != snapshot_date
                or replacement["acceptance_status"] != "availCode=P"
                or replacement["is_canonical"] != 1
                or not HEX64.fullmatch(str(replacement["content_hash"] or ""))):
            failures.append(f"{replacement_id}: filing identity/state is incorrect")
    observations = con.execute(
        "SELECT * FROM observations WHERE filing_id=? ORDER BY observation_id",
        (replacement_id,)).fetchall()
    checks["actual_replacement_observations"] = len(observations)
    if len(observations) != expected_observations:
        failures.append(f"{replacement_id}: {len(observations)} observations, "
                        f"expected {expected_observations}")
    for observation in observations:
        if observation["entity_key"] != entity or observation["source_system"] != "eLibrary":
            failures.append(f"{replacement_id}: observation owner/source is incorrect")
    lineage_errors, edge_count = _lineage_failures(
        con, observations, require_ioc_populations=True, filing_id=replacement_id)
    failures.extend(lineage_errors)
    checks["resolved_lineage_edges"] = edge_count
    return _verification(
        "metadata_n_and_public_replacement", checks, failures,
        "The source metadata says availCode N. The captured event and blocker show no "
        "download request and a null HTTP observation; the original HTTP-401/access-"
        "restriction claim is therefore contradicted, while the public replacement is "
        "tested as its own filing occurrence.")


def _quarter_interval(year: int, quarter: str) -> Tuple[str, str]:
    month = (int(quarter[1:]) - 1) * 3 + 1
    start = dt.date(year, month, 1)
    if month == 10:
        end = dt.date(year, 12, 31)
    else:
        end = dt.date(year, month + 3, 1) - dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _verify_form549d_semantic_exception(con: sqlite3.Connection,
                                         exception_id: str) -> Dict[str, Any]:
    entity, year, filing_id, expected_storage_rows, expected_storage_revenue = \
        FORM549D_SEMANTIC_EXPECTATIONS[exception_id]
    failures: List[str] = []
    checks: Dict[str, Any] = {
        "entity_key": entity, "reporting_year": year, "q4_filing_id": filing_id,
        "expected_storage_rows": expected_storage_rows,
        "expected_storage_total_rev": str(expected_storage_revenue),
    }
    filing = _expect_one(con.execute(
        "SELECT * FROM filings WHERE source_system='DataFERC' AND form='Form 549D' "
        "AND entity_key=? AND reporting_year=? AND reporting_period='Q4' "
        "AND is_canonical=1", (entity, year)).fetchall(),
        f"Form 549D canonical Q4 {entity}:{year}", failures)
    if filing is not None:
        if (str(filing["filing_id"]) != filing_id
                or not HEX64.fullmatch(str(filing["content_hash"] or ""))
                or filing["version_status"] not in ("original", "revised")):
            failures.append(f"{entity}:{year}: Q4 filing identity/revision state is incorrect")
        if filing["snapshot_date"] not in (None, ""):
            failures.append(f"{entity}:{year}: periodic Data.FERC filing misuses snapshot_date")

    source_rows = []
    facts = con.execute(
        "SELECT source_fact_id,value_as_filed FROM source_facts "
        "WHERE source_system='DataFERC' AND filing_id=? "
        "AND concept_local='Form549D_ShipperContractRow'", (filing_id,)).fetchall()
    for fact in facts:
        value = _json_mapping(fact["value_as_filed"],
                              f"Form 549D source row {fact['source_fact_id']}", failures)
        if value:
            source_rows.append(value)
            if str(value.get("Filer_CID") or "") != entity:
                failures.append(f"{filing_id}: source row has wrong native Filer_CID")
            if str(value.get("Form549D_ID") or "") != filing_id:
                failures.append(f"{filing_id}: source row has wrong filing occurrence")
            if value.get("Filing_Year__DASH__Quarter") != f"{year}-Q4":
                failures.append(f"{filing_id}: source row has wrong reporting period")
    storage_rows = [row for row in source_rows
                    if str(row.get("Service_Type") or "").strip().casefold() == "storage"]
    transport_rows = [row for row in source_rows
                      if str(row.get("Service_Type") or "").strip().casefold()
                      == "transportation"]
    try:
        storage_revenue = sum((_as_decimal(row.get("Total_Rev"))
                               for row in storage_rows), Decimal(0))
        transport_revenue = sum((_as_decimal(row.get("Total_Rev"))
                                 for row in transport_rows), Decimal(0))
    except ValueError as exc:
        failures.append(f"{filing_id}: invalid Total_Rev: {exc}")
        storage_revenue = transport_revenue = Decimal(0)
    checks.update({"actual_contract_rows": len(source_rows),
                   "actual_storage_rows": len(storage_rows),
                   "actual_storage_total_rev": str(storage_revenue),
                   "transportation_rows": len(transport_rows),
                   "transportation_total_rev": str(transport_revenue)})
    if len(storage_rows) != expected_storage_rows:
        failures.append(f"{entity}:{year}: {len(storage_rows)} Storage rows, "
                        f"expected {expected_storage_rows}")
    if storage_revenue != expected_storage_revenue:
        failures.append(f"{entity}:{year}: Storage Total_Rev {storage_revenue}, "
                        f"expected {expected_storage_revenue}")

    components = _expect_one(con.execute(
        "SELECT * FROM observations WHERE entity_key=? AND reporting_year=? "
        "AND reporting_period='Q4' AND metric_id='i311_revenue_components'",
        (entity, year)).fetchall(), f"549D revenue components {entity}:{year}", failures)
    if components is not None:
        detail = _json_mapping(components["value_text"],
                               f"549D revenue components {entity}:{year}", failures)
        try:
            component_storage = _as_decimal(
                (detail.get("revenue_by_service_type_as_filed") or {}).get("Storage"))
            component_transport = _as_decimal(detail.get("reported_total_transportation"))
        except (AttributeError, ValueError) as exc:
            failures.append(f"{entity}:{year}: invalid component totals: {exc}")
            component_storage = component_transport = Decimal(0)
        if component_storage != storage_revenue or component_transport != transport_revenue:
            failures.append(f"{entity}:{year}: component output differs from source rows")
        if "without applying that exclusion" not in str(detail.get("order_735a_note", "")):
            failures.append(f"{entity}:{year}: Order 735-A qualification is absent")
        if (components["period_basis"] != "annual" or components["filing_id"] != filing_id
                or components["version_status"] == "superseded"):
            failures.append(f"{entity}:{year}: component period/filing/version is incorrect")

    annual = _expect_one(con.execute(
        "SELECT * FROM observations WHERE entity_key=? AND reporting_year=? "
        "AND metric_id='i311_annual_transport_revenue'", (entity, year)).fetchall(),
        f"549D annual revenue {entity}:{year}", failures)
    if annual is not None:
        start, end = _quarter_interval(year, "Q4")
        start = f"{year}-01-01"
        if (annual["period_basis"] != "annual" or annual["reporting_period"] != "Q4"
                or annual["period_start"] != start or annual["period_end"] != end
                or "excluding storage" not in str(annual["scope"] or "").casefold()
                or annual["version_status"] == "superseded"):
            failures.append(f"{entity}:{year}: annual period/scope/version semantics are wrong")
        flags = str(annual["qa_flags"] or "")
        for phrase in ("[ORDER_735A_SCOPE_DIVERGENCE]", "OUT-OF-SCOPE REVENUE",
                       "excluded from this transportation figure",
                       f"Storage ${expected_storage_revenue:,.0f}"):
            if phrase not in flags:
                failures.append(f"{entity}:{year}: annual warning omits {phrase!r}")
        if entity == "C000826" and year == 2025:
            if (annual["availability"] != "interpretation_blocked"
                    or annual["validation"] != "blocked_ambiguity"
                    or annual["value_num"] is not None
                    or "ambiguous contract group" not in str(annual["missing_reason"] or "")
                    or "GRAIN BLOCKED" not in flags):
                failures.append(f"{entity}:{year}: ambiguity gate is not preserved")
        elif not transport_rows:
            if (annual["availability"] != "not_applicable"
                    or annual["validation"] != "pass"
                    or annual["value_num"] is not None
                    or "no transportation-service rows" not in
                    str(annual["missing_reason"] or "")
                    or "NOT substituted" not in str(annual["missing_reason"] or "")):
                failures.append(f"{entity}:{year}: no-transportation state is not explicit")
        else:
            try:
                annual_value = _as_decimal(annual["value_text"])
            except ValueError as exc:
                failures.append(f"{entity}:{year}: annual value is invalid: {exc}")
                annual_value = Decimal(0)
            if (annual["availability"] != "present" or annual["validation"] != "pass"
                    or annual["filing_id"] != filing_id
                    or annual["unit"] != "iso4217:USD"
                    or annual_value != transport_revenue):
                failures.append(f"{entity}:{year}: annual transportation revenue is incorrect")
            populations = con.execute(
                "SELECT * FROM lineage_populations WHERE observation_id=?",
                (annual["observation_id"],)).fetchall()
            population = _expect_one(populations,
                                     f"549D revenue population {entity}:{year}", failures)
            if population is not None:
                try:
                    filing_ids = json.loads(population["filing_ids"])
                    aggregate = _as_decimal(population["aggregate_value"])
                except (TypeError, json.JSONDecodeError, ValueError):
                    filing_ids, aggregate = [], Decimal(0)
                if (population["source_system"] != "DataFERC"
                        or filing_ids != [filing_id]
                        or population["row_count"] != len(transport_rows)
                        or aggregate != transport_revenue
                        or "Transportation" not in population["inclusion_rule"]
                        or "Storage" not in population["exclusion_rule"]):
                    failures.append(f"{entity}:{year}: annual lineage population is incorrect")

    scope_blockers = con.execute(
        "SELECT * FROM blockers WHERE adapter='form549d' AND scope=?",
        (f"{entity}:{year}",)).fetchall()
    scope_blocker = _expect_one(scope_blockers,
                                f"549D semantic blocker {entity}:{year}", failures)
    if scope_blocker is not None:
        error = str(scope_blocker["exact_error"] or "")
        if (f"${expected_storage_revenue:,.0f}" not in error
                or "NEVER consolidated" not in error
                or scope_blocker["human_decision_needed"] != 1):
            failures.append(f"{entity}:{year}: semantic blocker evidence is incomplete")
    return _verification(
        "exact_form549d_service_billing_period_revision", checks, failures,
        "Storage-labelled Total_Rev is preserved as filed but excluded from the "
        "transportation-only metric; the unresolved question is display, not a licence "
        "to relabel the amount as whole-entity storage revenue.")


def _verify_form549d_policy_exception(con: sqlite3.Connection,
                                       exception_id: str) -> Dict[str, Any]:
    failures: List[str] = []
    checks: Dict[str, Any] = {"original_policy_exception": exception_id}
    blocker = _expect_one(con.execute(
        "SELECT * FROM blockers WHERE adapter='form549d' "
        "AND scope='549D annual revenue scope'"
    ).fetchall(), "consolidated 549D policy blocker", failures)
    if blocker is not None:
        error = str(blocker["exact_error"] or "")
        for phrase in ("blk-c29e1a69a09275d1", "blk-df2155105afc957e",
                       '"historical_all_occurrences"', '"rows": 544',
                       '"revenue": "237017275"',
                       '"included_universe_2024_2025"'):
            if phrase not in error:
                failures.append(f"policy blocker omits {phrase!r}")
        if blocker["human_decision_needed"] != 1:
            failures.append("policy blocker does not retain the display decision")
    included_count = 0
    included_revenue = Decimal(0)
    query = (
        "SELECT sf.source_fact_id,sf.value_as_filed FROM source_facts sf JOIN filings f "
        "ON f.source_system=sf.source_system AND f.filing_id=sf.filing_id "
        "WHERE f.source_system='DataFERC' AND f.form='Form 549D' "
        "AND f.reporting_period='Q4' AND f.reporting_year BETWEEN 2024 AND 2025 "
        "AND sf.concept_local='Form549D_ShipperContractRow'")
    for source_fact_id, raw in con.execute(query):
        row = _json_mapping(raw, f"policy source row {source_fact_id}", failures)
        if str(row.get("Service_Type") or "").strip().casefold() != "storage":
            continue
        try:
            amount = _as_decimal(row.get("Total_Rev"))
        except ValueError as exc:
            failures.append(f"policy source row {source_fact_id}: {exc}")
            continue
        if amount:
            included_count += 1
            included_revenue += amount
    checks.update({"included_universe_storage_rows": included_count,
                   "included_universe_storage_total_rev": str(included_revenue)})
    if included_count != 179 or included_revenue != Decimal("39299027"):
        failures.append("included-universe policy recomputation differs from 179 / 39299027")
    for old_id in FORM549D_POLICY_EXCEPTION_IDS:
        if con.execute("SELECT COUNT(*) FROM blockers WHERE blocker_id=?",
                       (old_id,)).fetchone()[0]:
            failures.append(f"duplicate historical policy blocker remains persisted: {old_id}")
    return _verification(
        "form549d_policy_population_and_consolidation", checks, failures,
        "The 544-row historical audit population remains a labelled baseline; this gate "
        "independently recomputes the narrower included-universe 2024-2025 population and "
        "does not reverse-engineer either value as a target.")


def _verify_form549d_credential_exception(con: sqlite3.Connection,
                                           exception_id: str) -> Dict[str, Any]:
    entity = FORM549D_CREDENTIAL_EXPECTATIONS[exception_id]
    failures: List[str] = []
    expected_periods = {(2024, "Q1"), (2024, "Q2"), (2024, "Q3"), (2024, "Q4"),
                        (2025, "Q1"), (2025, "Q2"), (2025, "Q3"), (2025, "Q4"),
                        (2026, "Q1"), (2026, "Q2")}
    filings = con.execute(
        "SELECT * FROM filings WHERE source_system='DataFERC' AND form='Form 549D' "
        "AND entity_key=? AND is_canonical=1", (entity,)).fetchall()
    actual_periods = {(int(row["reporting_year"]), str(row["reporting_period"]))
                      for row in filings}
    if actual_periods != expected_periods or len(filings) != len(expected_periods):
        failures.append(f"{entity}: canonical filing periods differ from exact 2024Q1-2026Q2 set")
    for filing in filings:
        year, quarter = int(filing["reporting_year"]), str(filing["reporting_period"])
        start, end = _quarter_interval(year, quarter)
        if (filing["period_start"] != start or filing["period_end"] != end
                or filing["snapshot_date"] not in (None, "")
                or not HEX64.fullmatch(str(filing["content_hash"] or ""))
                or filing["version_status"] not in ("original", "revised")):
            failures.append(f"{entity}:{year}{quarter}: filing period/snapshot/version is wrong")
        source_rows = con.execute(
            "SELECT value_as_filed FROM source_facts WHERE source_system='DataFERC' "
            "AND filing_id=? AND concept_local='Form549D_ShipperContractRow'",
            (filing["filing_id"],)).fetchall()
        if not source_rows:
            failures.append(f"{entity}:{year}{quarter}: no native contract rows")
        for source_row in source_rows:
            value = _json_mapping(source_row[0], f"{entity}:{year}{quarter} source row", failures)
            if value and (str(value.get("Filer_CID") or "") != entity
                          or value.get("Filing_Year__DASH__Quarter") != f"{year}-{quarter}"):
                failures.append(f"{entity}:{year}{quarter}: native row identity is wrong")
    observations = con.execute(
        "SELECT * FROM observations WHERE entity_key=? AND source_regime='Form 549D' "
        "AND availability='present'", (entity,)).fetchall()
    if not observations:
        failures.append(f"{entity}: no present Form 549D observations")
    unresolved = con.execute(
        "SELECT blocker_id FROM blockers WHERE adapter='form549d' AND scope LIKE ? "
        "AND kind IN ('source','configuration') AND resolved_at IS NULL", (entity + "%",)
    ).fetchall()
    if unresolved:
        failures.append(f"{entity}: unresolved source/configuration blockers remain")
    checks = {"entity_key": entity, "canonical_periods": sorted(actual_periods),
              "canonical_filings": len(filings), "present_observations": len(observations),
              "unresolved_source_or_configuration_blockers": len(unresolved)}
    return _verification(
        "form549d_cached_input_and_failure_classification", checks, failures,
        "Declared cached Data.FERC inputs satisfy the exact periods. This supports the "
        "candidate's offline result and does not retroactively recast a missing credential "
        "as a FERC source failure.")


def _verify_capacity_exception(con: sqlite3.Connection,
                               exception_id: str) -> Dict[str, Any]:
    accession, entity, source_hash, source_bytes, delivery, storage = \
        CAPACITY_EXCEPTION_EXPECTATIONS[exception_id]
    failures: List[str] = []
    filing = _expect_one(con.execute(
        "SELECT * FROM filings WHERE source_system='eLibrary' AND filing_id=?",
        (accession,)).fetchall(), f"capacity filing {accession}", failures)
    if filing is not None and (filing["entity_key"] != entity
                               or filing["form"] != "Form 549B Capacity"
                               or filing["content_hash"] != source_hash
                               or filing["is_canonical"] != 1):
        failures.append(f"{accession}: capacity filing identity is incorrect")
    document = _expect_one(con.execute(
        "SELECT * FROM documents WHERE filing_id=? AND content_hash=?",
        (accession, source_hash)).fetchall(), f"capacity document {accession}", failures)
    if document is not None and (document["byte_size"] != source_bytes
                                 or document["availability"] != "retrieved"
                                 or document["media_type"] != "application/pdf"
                                 or document["text_layer"] != "no"):
        failures.append(f"{accession}: capacity document identity/state is incorrect")
    facts = con.execute(
        "SELECT * FROM source_facts WHERE source_system='eLibrary' AND filing_id=? "
        "AND concept_local='ocr_page_image_row' ORDER BY document_order", (accession,)
    ).fetchall()
    passage = " ".join(str(row["value_as_filed"] or "") for row in facts)
    compact = passage.replace("off system", "off-system")
    for phrase in (str(delivery), "MMcf/day", str(storage), "Bcf",
                   "base gas requirements", "does not include any off-system capacity",
                   "reasonably representative operating assumptions"):
        if phrase not in compact:
            failures.append(f"{accession}: operative OCR passage omits {phrase!r}")
    numeric = con.execute(
        "SELECT * FROM observations WHERE accession_number=? "
        "AND metric_id='cap_reported_capacity' AND value_num IS NOT NULL", (accession,)
    ).fetchall()
    if len(numeric) != 2:
        failures.append(f"{accession}: expected two numeric capacity observations")
    by_unit = {row["unit"]: row for row in numeric}
    if set(by_unit) != {"MMcf/day", "Bcf"}:
        failures.append(f"{accession}: capacity units are not MMcf/day and Bcf")
    fact_text = {row["source_fact_id"]: str(row["value_as_filed"] or "") for row in facts}
    for unit, expected_value in (("MMcf/day", delivery), ("Bcf", storage)):
        observation = by_unit.get(unit)
        if observation is None:
            continue
        try:
            actual_value = _as_decimal(observation["value_text"])
        except ValueError:
            actual_value = Decimal(0)
        flags = str(observation["qa_flags"] or "")
        linked = fact_text.get(observation["source_fact_id"], "")
        if (actual_value != expected_value or observation["entity_key"] != entity
                or observation["filing_id"] != accession
                or (document is not None and observation["document_id"] != document["document_id"])
                or observation["method"] != "document_extracted"
                or observation["validation"] != "pass"
                or not all(phrase in flags for phrase in
                           ("BASE GAS", "OFF-SYSTEM", "OPERATING ASSUMPTIONS",
                            "IMAGE-ONLY SOURCE, OCR EXTRACTED AND REVIEW-CHECKED",
                            "no unit conversion"))
                or not (str(expected_value) in linked or unit in linked)):
            failures.append(f"{accession}: {unit} value/qualification/direct lineage is wrong")
    summary = _expect_one(con.execute(
        "SELECT * FROM observations WHERE accession_number=? "
        "AND metric_id='cap_reported_capacity' AND value_num IS NULL", (accession,)
    ).fetchall(), f"capacity summary {accession}", failures)
    if summary is not None and (summary["value_text"] !=
                                "2 reported figures, no system total stated"
                                or "NO system-wide total" not in str(summary["qa_flags"] or "")):
        failures.append(f"{accession}: no-system-total qualification is absent")
    checks = {"accession_number": accession, "entity_key": entity,
              "source_hash": source_hash, "source_bytes": source_bytes,
              "ocr_rows": len(facts), "numeric_observations": len(numeric)}
    return _verification("exact_capacity_ocr_occurrence", checks, failures)


def _input_records(root: pathlib.Path, con: sqlite3.Connection,
                   draft: Mapping[str, Any],
                   audit_by_accession: Mapping[str, dict]) -> Tuple[List[dict], Dict[str, int]]:
    index_path = root / "source_cache" / "index.json"
    _, index = _load_json(index_path, "source-cache index")
    if not isinstance(index, dict):
        raise FinalRecordsRefused("source-cache index is not an object")
    final = []
    for row in sorted(draft["rows"], key=lambda r: r["input_id"]):
        accession = _require_text(row.get("accession_number"), "input accession")
        if not ACCESSION.fullmatch(accession) or row["input_id"] != "elibrary:" + accession:
            raise FinalRecordsRefused(f"invalid input identity {row.get('input_id')}")
        if row.get("classification") not in {
            "mandatory executable prerequisite",
            "evidence required to reproduce existing assertion",
            "source genuinely unavailable under documented policy",
        }:
            raise FinalRecordsRefused(f"input {row['input_id']} has invalid classification")
        _require_list(row.get("entities"), f"input {row['input_id']}.entities")
        _require_list(row.get("consumers"), f"input {row['input_id']}.consumers")
        attachment = row.get("official_attachment_capture") or {}
        raw_rel = pathlib.Path(_require_text(attachment.get("raw_path"),
                                             f"input {row['input_id']} raw_path"))
        raw_path = _path_within(root / raw_rel, root, f"input {row['input_id']} raw bytes")
        raw_identity = _expected_identity(raw_path, attachment,
                                          f"input {row['input_id']} raw capture")
        raw_identity["path"] = raw_rel.as_posix()
        list_capture = row.get("official_list_capture") or {}
        if list_capture.get("http_status") != 200:
            raise FinalRecordsRefused(f"input {row['input_id']} has no successful list capture")
        cache = row.get("cache_import") or {}
        list_url = _require_text(cache.get("list_cache_url"), "list cache URL")
        attachment_url = _require_text(cache.get("attachment_cache_url"),
                                       "attachment cache URL")
        cached = {}
        for kind, url, expected_sha, expected_bytes in (
            ("list", list_url, list_capture.get("sha256"), list_capture.get("bytes")),
            ("attachment", attachment_url, attachment.get("sha256"), attachment.get("bytes")),
        ):
            key = _sha(url.encode("utf-8"))
            record = index.get(key)
            if not isinstance(record, dict) or record.get("source_url") != url:
                raise FinalRecordsRefused(
                    f"input {row['input_id']} {kind} request is absent from cache index")
            if record.get("content_hash") != expected_sha or record.get("byte_size") != expected_bytes:
                raise FinalRecordsRefused(
                    f"input {row['input_id']} {kind} index identity disagrees with capture")
            cache_rel = pathlib.Path(_require_text(record.get("cache_path"), "cache path"))
            cache_path = _path_within(root / "source_cache" / cache_rel,
                                      root / "source_cache", "cache object")
            expected = {"sha256": expected_sha, "bytes": expected_bytes}
            object_identity = _expected_identity(
                cache_path, expected, f"input {row['input_id']} {kind} cache object")
            object_identity.update({"cache_key": key,
                                    "path": "source_cache/" + cache_rel.as_posix(),
                                    "source_url": url})
            cached[kind] = object_identity
        application = _filing_application(con, accession=accession)
        consumer_text = " ".join(str(value) for value in row["consumers"]).lower()
        required_populations = {"filings"}
        if "document/filing archive" in consumer_text:
            required_populations.add("documents")
        if "observation" in consumer_text or "source-linked" in consumer_text \
                or "ioc adapter" in consumer_text:
            required_populations.add("observations")
        if "source facts/observations/lineage" in consumer_text:
            required_populations.update({"source_facts", "lineage_edges"})
        if "coverage" in consumer_text:
            required_populations.add("coverage_slots")
        missing_populations = sorted(
            name for name in required_populations if application.get(name, 0) <= 0)
        application["required_populations"] = sorted(required_populations)
        application["missing_required_populations"] = missing_populations
        applied = not missing_populations
        if applied:
            disposition = "integrated_in_build_a"
            limitation = (
                "Captured after the independent audit; proves this candidate's reproduction, "
                "not the frozen audited release's prior completeness.")
        else:
            disposition = "captured_and_cached_not_applied"
            limitation = (
                "Official bytes are captured and cache-bound, but this Build A has no filing "
                "plus downstream consumer population for the accession; no completeness claim.")
        final.append({
            "input_id": row["input_id"], "accession_number": accession,
            "adapter": row.get("adapter"), "source_system": row.get("source_system"),
            "classification": row["classification"],
            "classification_basis": row.get("classification_basis"),
            "audit_claim": audit_by_accession[accession], "entities": row["entities"],
            "consumers": row["consumers"],
            "capture": {"list": list_capture, "attachment": attachment,
                        "raw_identity": raw_identity},
            "cache_objects": cached, "build_a_application": application,
            "disposition": disposition, "acceptance_result": (
                "accepted_for_candidate" if applied else "qualified_open_data_application"),
            "residual_limitation": limitation,
            "evidence": row["evidence"],
        })
    counts = dict(collections.Counter(r["disposition"] for r in final))
    return final, counts


def _search_response_accessions(raw: bytes, label: str) -> List[str]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalRecordsRefused(f"{label} is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(value, dict) or value.get("success") is not True \
            or not isinstance(value.get("searchHits"), list):
        raise FinalRecordsRefused(f"{label} is not a successful eLibrary search response")
    accessions = []
    for position, hit in enumerate(value["searchHits"]):
        if not isinstance(hit, dict):
            raise FinalRecordsRefused(f"{label} search hit {position} is not an object")
        # The official response currently spells this field with three s's.
        accession = _require_text(
            hit.get("acesssionNumber"), f"{label} search hit {position} accession")
        if not ACCESSION.fullmatch(accession):
            raise FinalRecordsRefused(f"{label} has malformed accession {accession}")
        accessions.append(accession)
    if value.get("numHits") != len(accessions) or value.get("totalHits") != len(accessions):
        raise FinalRecordsRefused(f"{label} hit counts disagree with its response population")
    if len(accessions) != len(set(accessions)):
        raise FinalRecordsRefused(f"{label} repeats an accession")
    return accessions


def _future_occurrence_population(con: sqlite3.Connection, accession: str) -> Dict[str, int]:
    """Count every persisted consumer in which a future occurrence could leak."""
    checks = {
        "filings": ("SELECT COUNT(*) FROM filings WHERE accession_number=?", (accession,)),
        "filing_dockets": ("SELECT COUNT(*) FROM filing_dockets WHERE filing_id=?", (accession,)),
        "documents": ("SELECT COUNT(*) FROM documents WHERE accession_number=?", (accession,)),
        "source_facts": ("SELECT COUNT(*) FROM source_facts WHERE filing_id=?", (accession,)),
        "observations": ("SELECT COUNT(*) FROM observations WHERE accession_number=?", (accession,)),
        "events": ("SELECT COUNT(*) FROM events WHERE accession_number=? OR filing_id=?",
                   (accession, accession)),
        "lineage_populations": (
            "SELECT COUNT(*) FROM lineage_populations WHERE filing_ids LIKE ?",
            (f'%"{accession}"%',)),
        "run_input_inventory": (
            "SELECT COUNT(*) FROM run_input_inventory WHERE accession_number=? OR filing_id=?",
            (accession, accession)),
    }
    return {name: int(con.execute(sql, params).fetchone()[0])
            for name, (sql, params) in checks.items()}


def _bounded_search_plan_boundary(root: pathlib.Path) -> Dict[str, Any]:
    """Prove repair-diagnostic search responses are not execution inputs.

    A future hit can produce an append-only exclusion warning only when the
    production retrieval actually consumes the response.  The three bounded
    searches captured during repair are deliberately *not* selected by the
    final plan; the versioned proceeding seed is selected instead.  Requiring a
    runtime warning from an unselected diagnostic would manufacture execution
    history.  This boundary makes the distinction machine-checkable.
    """
    _, plan = _load_json(root / "config" / "run_plan.json", "run plan")
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        raise FinalRecordsRefused("run plan has no step input declarations")
    selected: List[str] = []
    current_inputs: List[str] = []
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("inputs"), list):
            continue
        inputs = [str(value) for value in step["inputs"]]
        selected.extend(inputs)
        if step.get("id") == "build_current_universe":
            current_inputs.extend(inputs)
    diagnostic_prefix = "inputs/official_ferc_elibrary_search_recovery_20260909/"
    selected_diagnostics = sorted({value for value in selected
                                   if value.startswith(diagnostic_prefix)})
    seed = "inputs/official_ferc/elibrary/rate_proceedings_v1.csv"
    accepted = not selected_diagnostics and seed in current_inputs
    return {
        "accepted": accepted,
        "selected_diagnostic_search_inputs": selected_diagnostics,
        "selected_proceeding_seed": seed if seed in current_inputs else None,
        "boundary": ("diagnostic response retained as evidence but not consumed by replay; "
                     "the production as-of filter remains exercised by its mapped test"),
    }


def _search_response_input_records(
        root: pathlib.Path, con: sqlite3.Connection, rows: Sequence[Mapping[str, Any]],
        claims_by_id: Mapping[str, dict]) -> List[dict]:
    """Finalize bounded repair-time search responses without calling them filings.

    These three responses explain and reproduce the failed first clean build.
    The corrected candidate uses the separately declared, versioned proceeding
    seed, so they remain hash-bound diagnostic evidence rather than being
    misrepresented as prerequisites selected by the final run.
    """
    _, index = _load_json(root / "source_cache" / "index.json", "source-cache index")
    capture_path = (root / "inputs" /
                    "official_ferc_elibrary_search_recovery_20260909" / "CAPTURE.json")
    _, bundle = _load_json(capture_path, "bounded eLibrary search capture")
    bundle_rows = bundle.get("rows") if isinstance(bundle, dict) else None
    if bundle.get("schema") != "bounded_official_elibrary_search_capture_v1" \
            or not isinstance(bundle_rows, list) or bundle.get("requests_made") != 3:
        raise FinalRecordsRefused("bounded eLibrary search capture has the wrong population")
    bundle_by_docket = {str(item.get("docket") or ""): item for item in bundle_rows
                        if isinstance(item, dict)}
    if set(bundle_by_docket) != {"IS26-587", "RP26-1091", "RP26-981"}:
        raise FinalRecordsRefused("bounded eLibrary search capture docket population changed")
    plan_boundary = _bounded_search_plan_boundary(root)
    if not plan_boundary["accepted"]:
        raise FinalRecordsRefused(
            "bounded repair search responses are selected as execution inputs or the "
            "versioned proceeding seed is not selected")

    final: List[dict] = []
    for row in sorted(rows, key=lambda value: str(value.get("input_id") or "")):
        input_id = _require_text(row.get("input_id"), "search input id")
        docket = _require_text(row.get("docket"), f"{input_id}.docket")
        capture = row.get("official_search_capture") or {}
        request_sha = _require_text(
            capture.get("request_payload_sha256"), f"{input_id}.request_payload_sha256")
        expected_id = f"elibrary-search:{docket}:{request_sha}"
        if input_id != expected_id or not HEX64.fullmatch(request_sha):
            raise FinalRecordsRefused(f"invalid bounded search input identity {input_id}")
        if row.get("classification") != "evidence required to reproduce existing assertion":
            raise FinalRecordsRefused(f"search input {input_id} has the wrong classification")
        source_row = bundle_by_docket.get(docket)
        comparable = {
            "docket": docket,
            "method": "POST",
            "official_url": capture.get("official_url"),
            "cache_url": capture.get("cache_url"),
            "request_payload_sha256": request_sha,
            "request_payload_bytes": capture.get("request_payload_bytes"),
            "response_bytes": capture.get("bytes"),
            "response_sha256": capture.get("sha256"),
            "accessions": capture.get("accessions"),
        }
        source_comparable = {
            "docket": source_row.get("docket") if source_row else None,
            "method": source_row.get("method") if source_row else None,
            "official_url": source_row.get("official_url") if source_row else None,
            "cache_url": source_row.get("cache_url") if source_row else None,
            "request_payload_sha256": (
                source_row.get("request_payload_sha256") if source_row else None),
            "request_payload_bytes": (
                source_row.get("request_payload_bytes") if source_row else None),
            "response_bytes": source_row.get("response_bytes") if source_row else None,
            "response_sha256": source_row.get("response_sha256") if source_row else None,
            "accessions": source_row.get("accessions") if source_row else None,
        }
        if comparable != source_comparable:
            raise FinalRecordsRefused(
                f"search input {input_id} disagrees with the immutable capture ledger")
        if capture.get("http_status_basis") != \
                "bounded_capture_v1_successful_response_contract":
            raise FinalRecordsRefused(
                f"search input {input_id} overstates the v1 capture's HTTP metadata")
        raw_rel = pathlib.Path(_require_text(capture.get("raw_path"),
                                             f"{input_id}.raw_path"))
        raw_path = _path_within(root / raw_rel, root, f"{input_id} raw response")
        raw_identity = _expected_identity(
            raw_path, {"bytes": capture.get("bytes"), "sha256": capture.get("sha256")},
            f"{input_id} raw response")
        raw_identity["path"] = raw_rel.as_posix()
        observed_accessions = _search_response_accessions(
            _read(raw_path, f"{input_id} raw response"), input_id)
        if observed_accessions != capture.get("accessions"):
            raise FinalRecordsRefused(f"search input {input_id} accession order changed")

        cache_url = _require_text(capture.get("cache_url"), f"{input_id}.cache_url")
        cache_key = _sha(cache_url.encode("utf-8"))
        indexed = index.get(cache_key) if isinstance(index, dict) else None
        if not isinstance(indexed, dict) or indexed.get("source_url") != cache_url \
                or indexed.get("content_hash") != capture.get("sha256") \
                or indexed.get("byte_size") != capture.get("bytes"):
            raise FinalRecordsRefused(f"search input {input_id} is absent/mismatched in cache")
        cache_rel = pathlib.Path(_require_text(indexed.get("cache_path"), "cache path"))
        cache_path = _path_within(root / "source_cache" / cache_rel,
                                  root / "source_cache", f"{input_id} cache object")
        cache_identity = _expected_identity(
            cache_path, {"bytes": capture.get("bytes"), "sha256": capture.get("sha256")},
            f"{input_id} cache object")
        cache_identity.update({"path": "source_cache/" + cache_rel.as_posix(),
                               "cache_key": cache_key, "source_url": cache_url})

        expected_occurrences = _require_list(
            row.get("expected_occurrences"), f"{input_id}.expected_occurrences")
        occurrence_checks = []
        failures = []
        for occurrence in expected_occurrences:
            if not isinstance(occurrence, dict):
                raise FinalRecordsRefused(f"{input_id} occurrence is not an object")
            accession = _require_text(occurrence.get("accession_number"),
                                      f"{input_id} occurrence accession")
            relation = occurrence.get("as_of_relation")
            if relation == "on_or_before_as_of":
                entity = _require_text(occurrence.get("entity_key"),
                                       f"{input_id} occurrence entity")
                expected_docket = _require_text(occurrence.get("filing_docket"),
                                                f"{input_id} occurrence docket")
                owners = [str(value[0]) for value in con.execute(
                    "SELECT DISTINCT entity_key FROM filings WHERE accession_number=?",
                    (accession,))]
                dockets = [str(value[0]) for value in con.execute(
                    "SELECT docket FROM filing_dockets WHERE filing_id=? ORDER BY docket",
                    (accession,))]
                documents = int(con.execute(
                    "SELECT COUNT(*) FROM documents WHERE accession_number=?",
                    (accession,)).fetchone()[0])
                accepted = owners == [entity] and expected_docket in dockets and documents > 0
                occurrence_checks.append({
                    "accession_number": accession, "as_of_relation": relation,
                    "owners": owners, "filing_dockets": dockets,
                    "documents": documents, "accepted": accepted})
                if not accepted:
                    failures.append(f"{accession} was not retained under its exact owner/docket")
            elif relation == "after_as_of":
                population = _future_occurrence_population(con, accession)
                warning_rows = int(con.execute(
                    "SELECT COUNT(*) FROM run_log WHERE level='warn' AND message LIKE ?",
                    (f"%{accession}%",)).fetchone()[0])
                exclusion_evidence = ("append_only_runtime_warning"
                                      if warning_rows > 0 else
                                      "diagnostic_response_not_selected_by_run_plan")
                accepted = (not any(population.values())
                            and (warning_rows > 0 or plan_boundary["accepted"]))
                occurrence_checks.append({
                    "accession_number": accession, "as_of_relation": relation,
                    "persisted_population": population,
                    "exclusion_warning_rows": warning_rows,
                    "exclusion_evidence": exclusion_evidence,
                    "accepted": accepted})
                if not accepted:
                    failures.append(
                        f"{accession} leaked after the as-of boundary or lacks a valid "
                        "runtime/non-selected diagnostic boundary")
            else:
                raise FinalRecordsRefused(
                    f"search input {input_id} has invalid as_of_relation {relation!r}")
        if {item["accession_number"] for item in expected_occurrences} != \
                set(observed_accessions):
            raise FinalRecordsRefused(
                f"search input {input_id} occurrence contract differs from response")
        if failures:
            raise FinalRecordsRefused(f"search input {input_id} failed Build A: {failures}")
        selected_checkpoints = [dict(value) for value in con.execute(
            "SELECT adapter,entity_cid,scope_key,state,last_run_id FROM checkpoints "
            "WHERE adapter='elibrary_docs' AND scope_key=? ORDER BY entity_cid",
            ("docket:" + docket,))]
        final.append({
            "input_id": input_id, "input_kind": "elibrary_advanced_search_response",
            "docket": docket, "adapter": row.get("adapter"),
            "source_system": row.get("source_system"),
            "classification": row["classification"],
            "classification_basis": row.get("classification_basis"),
            "audit_claim": claims_by_id[input_id], "entities": row.get("entities") or [],
            "consumers": row.get("consumers") or [],
            "capture": {"request_method": "POST", "request_payload_sha256": request_sha,
                        "http_status": 200,
                        "http_status_basis": capture["http_status_basis"],
                        "raw_identity": raw_identity},
            "cache_objects": {"search_response": cache_identity},
            "build_a_application": {
                "mode": "repair_diagnostic_evidence_not_selected_final_seed_input",
                "run_plan_boundary": plan_boundary,
                "selected_docket_checkpoints": selected_checkpoints,
                "occurrences": occurrence_checks,
                "accepted": True,
            },
            "disposition": "captured_cache_bound_repair_evidence",
            "acceptance_result": "accepted_as_repair_evidence_not_execution_prerequisite",
            "residual_limitation": row.get("residual_limitation"),
            "evidence": row.get("evidence") or [],
        })
    return final


def _dependency_capture_zip_inventory(raw: bytes, label: str) -> Dict[str, Any]:
    """Recompute the safe member/CRC contract for a captured download."""
    if not raw.startswith(b"PK"):
        return {"container": "single_object", "crc_clean": None, "members": []}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = [item for item in archive.infolist() if not item.is_dir()]
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise FinalRecordsRefused(f"{label} contains duplicate ZIP member names")
            members = []
            for item in infos:
                member = pathlib.PurePosixPath(item.filename)
                mode = (item.external_attr >> 16) & 0o170000
                if member.is_absolute() or ".." in member.parts or "\\" in item.filename \
                        or mode == 0o120000:
                    raise FinalRecordsRefused(f"{label} contains unsafe ZIP member {item.filename}")
                archive.read(item)
                members.append({"name": item.filename, "bytes": item.file_size,
                                "crc32": f"{item.CRC:08x}"})
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise FinalRecordsRefused(f"{label} is not a CRC-clean ZIP: {exc}") from None
    return {"container": "zip", "crc_clean": True, "members": members}


def _dependency_capture_input_records(
        root: pathlib.Path, con: sqlite3.Connection, rows: Sequence[Mapping[str, Any]],
        claims_by_id: Mapping[str, dict]) -> List[dict]:
    """Validate the exact cache delta which unblocked the clean v4 replay.

    The capture is a mandatory executable input, not merely a post-hoc log.  We
    retain the pre-capture index so this boundary can prove that the operation
    appended exactly the declared responses without rewriting earlier evidence.
    Every request is rebound to its current content-addressed cache object and
    the four affected production units must have completed in Build A.
    """
    if len(rows) != 1:
        raise FinalRecordsRefused("expected exactly one eLibrary dependency-capture bundle")
    row = rows[0]
    input_id = _require_text(row.get("input_id"), "dependency capture input id")
    if input_id != "elibrary-dependency-capture:build-a-v4:20260910" \
            or row.get("classification") != "mandatory executable prerequisite":
        raise FinalRecordsRefused("eLibrary dependency capture has the wrong identity/classification")

    capture_decl = row.get("capture_manifest") or {}
    capture_rel = pathlib.Path(_require_text(
        capture_decl.get("path"), f"{input_id}.capture_manifest.path"))
    capture_path = _path_within(root / capture_rel, root, "dependency capture manifest")
    capture_raw, capture = _load_json(capture_path, "dependency capture manifest")
    capture_identity = _expected_identity(
        capture_path, capture_decl, "dependency capture manifest")
    capture_identity["path"] = capture_rel.as_posix()
    if not isinstance(capture, dict) \
            or capture.get("schema") != "official_ferc_elibrary_dependency_capture_v1" \
            or capture.get("new_entries") != 11 \
            or capture.get("preexisting_entries_changed") != 0 \
            or capture.get("preexisting_entries_removed") != 0:
        raise FinalRecordsRefused("dependency capture manifest population/state is invalid")

    before_decl = row.get("pre_capture_cache_index") or {}
    before_rel = pathlib.Path(_require_text(
        before_decl.get("path"), f"{input_id}.pre_capture_cache_index.path"))
    before_path = _path_within(root / before_rel, root, "pre-capture cache index")
    before_raw, before = _load_json(before_path, "pre-capture cache index")
    before_identity = _expected_identity(before_path, before_decl, "pre-capture cache index")
    before_identity["path"] = before_rel.as_posix()
    if not isinstance(before, dict) or len(before) != before_decl.get("entries"):
        raise FinalRecordsRefused("pre-capture cache index row count is invalid")

    index_path = root / "source_cache" / "index.json"
    index_raw, index = _load_json(index_path, "candidate source-cache index")
    if not isinstance(index, dict):
        raise FinalRecordsRefused("candidate source-cache index is not an object")
    before_claim = capture.get("source_cache_before") or {}
    after_claim = capture.get("source_cache_after") or {}
    if before_claim != {"entries": len(before), "bytes": len(before_raw),
                        "sha256": _sha(before_raw)} \
            or after_claim != {"entries": len(index), "bytes": len(index_raw),
                               "sha256": _sha(index_raw)}:
        raise FinalRecordsRefused("dependency capture before/after cache identities disagree")
    changed = [key for key, value in before.items() if index.get(key) != value]
    removed = sorted(set(before) - set(index))
    if changed or removed:
        raise FinalRecordsRefused(
            f"dependency capture rewrote prior cache evidence: changed={changed} removed={removed}")

    captured_rows = _require_list(capture.get("rows"), "dependency capture rows")
    if len(captured_rows) != 11 or not all(isinstance(value, dict) for value in captured_rows):
        raise FinalRecordsRefused("dependency capture does not contain exactly 11 response rows")
    by_key = {str(value.get("cache_key") or ""): value for value in captured_rows}
    if len(by_key) != 11 or any(not HEX64.fullmatch(key) for key in by_key):
        raise FinalRecordsRefused("dependency capture cache keys are missing, malformed or duplicated")
    if set(index) - set(before) != set(by_key):
        raise FinalRecordsRefused("dependency capture row keys differ from the actual cache delta")

    declared_required = set(_require_list(
        row.get("required_cache_keys"), f"{input_id}.required_cache_keys"))
    captured_required = set(_require_list(
        capture.get("required_failed_build_keys"),
        "dependency capture required_failed_build_keys"))
    if len(declared_required) != 6 or declared_required != captured_required \
            or any(key not in by_key for key in declared_required):
        raise FinalRecordsRefused("dependency capture required-key population changed")
    mandatory_keys = set(_require_list(
        capture.get("mandatory_clean_retry_keys"),
        "dependency capture mandatory_clean_retry_keys"))
    if mandatory_keys != set(by_key):
        raise FinalRecordsRefused("dependency capture mandatory clean-retry population changed")
    for key, value in by_key.items():
        direct = key in captured_required
        expected_role = "direct_v4_failure" if direct else "transitive_replay_dependency"
        if value.get("mandatory_for_clean_retry") is not True \
                or value.get("required_by_failed_build") is not direct \
                or value.get("dependency_role") != expected_role:
            raise FinalRecordsRefused(f"dependency role is inconsistent for {key}")

    kinds = collections.Counter(str(value.get("kind") or "") for value in captured_rows)
    if kinds != {"elibrary_advanced_search_response": 5,
                 "elibrary_file_list_response": 3,
                 "elibrary_attachment_response": 3}:
        raise FinalRecordsRefused(f"dependency capture response kinds changed: {dict(kinds)}")

    cache_objects: Dict[str, dict] = {}
    downloads: Dict[str, Mapping[str, Any]] = {}
    file_lists: Dict[str, Mapping[str, Any]] = {}
    searches: List[Mapping[str, Any]] = []
    for key, captured in sorted(by_key.items()):
        url = _require_text(captured.get("source_url"), f"capture row {key}.source_url")
        if _sha(url.encode("utf-8")) != key:
            raise FinalRecordsRefused(f"dependency capture URL/key mismatch for {key}")
        indexed = index.get(key)
        response = captured.get("response") or {}
        if not isinstance(indexed, dict) or indexed.get("source_url") != url \
                or indexed.get("content_hash") != response.get("sha256") \
                or indexed.get("byte_size") != response.get("bytes") \
                or indexed.get("source_system") != captured.get("source_system") \
                or indexed.get("media_type") != captured.get("media_type"):
            raise FinalRecordsRefused(f"dependency capture/index mismatch for {key}")
        cache_rel_text = _require_text(indexed.get("cache_path"), f"capture row {key}.cache_path")
        if response.get("cache_path") != "source_cache/" + cache_rel_text:
            raise FinalRecordsRefused(f"dependency capture object path mismatch for {key}")
        cache_rel = pathlib.Path(cache_rel_text)
        cache_path = _path_within(root / "source_cache" / cache_rel,
                                  root / "source_cache", f"capture row {key} object")
        object_identity = _expected_identity(
            cache_path, {"bytes": response.get("bytes"), "sha256": response.get("sha256")},
            f"dependency capture object {key}")
        object_identity.update({"path": "source_cache/" + cache_rel.as_posix(),
                                "cache_key": key, "source_url": url,
                                "kind": captured.get("kind")})
        cache_objects[key] = object_identity

        request = captured.get("request") or {}
        kind = captured.get("kind")
        if kind == "elibrary_advanced_search_response":
            body = request.get("body")
            if captured.get("method") != "POST" or not isinstance(body, dict):
                raise FinalRecordsRefused(f"dependency search {key} has an invalid request")
            request_raw = json.dumps(body, sort_keys=True).encode("utf-8")
            request_hash = _sha(request_raw)
            if request.get("bytes") != len(request_raw) or request.get("sha256") != request_hash \
                    or url != ("https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/"
                               "AdvancedSearch#body=" + request_hash[:32]):
                raise FinalRecordsRefused(f"dependency search request identity changed for {key}")
            observed = _search_response_accessions(
                _read(cache_path, f"dependency search response {key}"), key)
            if observed != captured.get("response_accessions"):
                raise FinalRecordsRefused(f"dependency search accession population changed for {key}")
            searches.append(captured)
        elif kind == "elibrary_file_list_response":
            accession = _require_text(captured.get("accession_number"),
                                      f"dependency file list {key}.accession")
            expected_url = ("https://elibrary.ferc.gov/eLibraryWebAPI/api/File/"
                            "GetFileListFromP8/" + accession)
            if captured.get("method") != "GET" or url != expected_url \
                    or request != {"body": None, "bytes": 0, "sha256": None}:
                raise FinalRecordsRefused(f"dependency file-list request changed for {key}")
            try:
                value = json.loads(_read(cache_path, f"dependency file-list response {key}"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FinalRecordsRefused(
                    f"dependency file-list response {key} is invalid JSON: {exc}") from None
            listed = value.get("DataList") if isinstance(value, dict) else None
            errors = value.get("ErrorList") if isinstance(value, dict) else None
            public_ids = list(dict.fromkeys(str(item.get("ID") or "") for item in (listed or [])
                                            if isinstance(item, dict)
                                            and (item.get("Availability_Mode")
                                                 or item.get("Availability_Code")) == "P"
                                            and item.get("ID")))
            if not isinstance(listed, list) or captured.get("listed_files") != len(listed) \
                    or captured.get("error_list") != (errors or []) \
                    or captured.get("public_attachment_ids") != public_ids:
                raise FinalRecordsRefused(f"dependency file-list content changed for {key}")
            file_lists[accession] = captured
        elif kind == "elibrary_attachment_response":
            accession = _require_text(captured.get("accession_number"),
                                      f"dependency attachment {key}.accession")
            body = request.get("body")
            if captured.get("method") != "POST" or not isinstance(body, dict):
                raise FinalRecordsRefused(f"dependency attachment {key} has an invalid request")
            request_raw = json.dumps(body, sort_keys=True).encode("utf-8")
            request_hash = _sha(request_raw)
            expected_url = ("https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
                            "#body=" + request_hash[:32])
            if request.get("bytes") != len(request_raw) or request.get("sha256") != request_hash \
                    or url != expected_url or body.get("accession") != accession \
                    or body.get("fileidLst") != captured.get("attachment_ids"):
                raise FinalRecordsRefused(f"dependency attachment request changed for {key}")
            inventory = _dependency_capture_zip_inventory(
                _read(cache_path, f"dependency attachment response {key}"), key)
            if inventory != captured.get("response_container"):
                raise FinalRecordsRefused(f"dependency attachment inventory changed for {key}")
            downloads[accession] = captured

    if set(file_lists) != set(downloads):
        raise FinalRecordsRefused("dependency file-list/download accession populations disagree")
    for accession, listed in file_lists.items():
        downloaded = downloads[accession]
        if listed.get("download_cache_key") != downloaded.get("cache_key") \
                or listed.get("public_attachment_ids") != downloaded.get("attachment_ids"):
            raise FinalRecordsRefused(f"dependency download binding changed for {accession}")

    expected_units = _require_list(row.get("expected_units"), f"{input_id}.expected_units")
    declared_pairs = {(str(unit.get("entity_key") or ""), docket)
                      for unit in expected_units if isinstance(unit, dict)
                      for docket in _require_list(unit.get("dockets"), "expected unit dockets")}
    captured_pairs = {(str(item.get("entity_key") or ""), str(item.get("docket") or ""))
                      for item in searches}
    if len(expected_units) != 4 or declared_pairs != captured_pairs:
        raise FinalRecordsRefused("dependency capture expected-unit/docket population changed")

    application_units = []
    application_failures = []
    for unit in expected_units:
        entity = _require_text(unit.get("entity_key"), "dependency capture unit entity")
        checkpoints = [dict(value) for value in con.execute(
            "SELECT c.scope_key,c.state,c.last_error,c.last_run_id,r.status AS run_status,"
            "EXISTS(SELECT 1 FROM unit_commits u WHERE u.run_id=c.last_run_id "
            "AND u.adapter=c.adapter AND u.entity_cid=c.entity_cid "
            "AND u.scope_key=c.scope_key) AS has_commit "
            "FROM checkpoints c LEFT JOIN runs r ON r.run_id=c.last_run_id "
            "WHERE c.adapter='elibrary_docs' AND c.entity_cid=? "
            "ORDER BY c.updated_at DESC,c.scope_key", (entity,))]
        accepted_checkpoints = [value for value in checkpoints
                                if value["state"] == "done"
                                and value["run_status"] == "complete"
                                and int(value["has_commit"] or 0) == 1]
        docket_checks = []
        for docket in _require_list(unit.get("dockets"), f"dependency unit {entity}.dockets"):
            matches = [dict(value) for value in con.execute(
                "SELECT DISTINCT fd.filing_id,fe.entity_key,"
                "COUNT(DISTINCT d.document_id) AS documents "
                "FROM filing_dockets fd JOIN filing_entities fe "
                "ON fe.source_system=fd.source_system AND fe.filing_id=fd.filing_id "
                "LEFT JOIN documents d ON d.source_system=fd.source_system "
                "AND d.filing_id=fd.filing_id "
                "WHERE fd.source_system='eLibrary' AND fe.entity_key=? "
                "AND (UPPER(fd.docket)=? OR UPPER(fd.docket) LIKE ?) "
                "GROUP BY fd.filing_id,fe.entity_key ORDER BY fd.filing_id",
                (entity, str(docket).upper(), str(docket).upper() + "-%"))]
            accepted = bool(matches) and any(int(value["documents"] or 0) > 0
                                             for value in matches)
            docket_checks.append({"docket": docket, "occurrences": matches,
                                  "accepted": accepted})
            if not accepted:
                application_failures.append(f"{entity}/{docket} has no retained document occurrence")
        if not accepted_checkpoints:
            application_failures.append(f"{entity} has no completed committed Build A unit")
        application_units.append({"entity_key": entity, "checkpoints": checkpoints,
                                  "accepted_checkpoints": len(accepted_checkpoints),
                                  "dockets": docket_checks,
                                  "accepted": bool(accepted_checkpoints)
                                              and all(value["accepted"]
                                                      for value in docket_checks)})
    if application_failures:
        raise FinalRecordsRefused(
            "dependency capture was not fully applied in Build A: " +
            "; ".join(application_failures))

    log_decl = row.get("capture_log") or {}
    log_rel = pathlib.Path(_require_text(log_decl.get("path"), f"{input_id}.capture_log.path"))
    log_path = _path_within(root / log_rel, root, "dependency capture log")
    log_identity = _expected_identity(log_path, log_decl, "dependency capture log")
    log_identity["path"] = log_rel.as_posix()
    if (capture.get("capture_log") or {}).get("bytes") != log_identity["bytes"] \
            or (capture.get("capture_log") or {}).get("sha256") != log_identity["sha256"] \
            or (capture.get("capture_log") or {}).get("path") != log_rel.as_posix():
        raise FinalRecordsRefused("dependency capture log identity disagrees with its manifest")

    _, plan = _load_json(root / "config" / "run_plan.json", "run plan")
    required = plan.get("required_inputs") if isinstance(plan, dict) else None
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(required, list) or not isinstance(steps, list):
        raise FinalRecordsRefused("run plan cannot declare the dependency capture")
    declarations = {
        capture_rel.as_posix(): capture_identity,
        before_rel.as_posix(): before_identity,
        log_rel.as_posix(): log_identity,
    }
    declared_rows = {}
    for path, identity in declarations.items():
        matches = [item for item in required if isinstance(item, dict)
                   and item.get("path") == path]
        if len(matches) != 1 \
                or matches[0].get("bytes") not in (None, identity["bytes"]) \
                or matches[0].get("sha256") != identity["sha256"]:
            raise FinalRecordsRefused(
                f"dependency capture input is not hash-declared exactly once: {path}")
        declared_rows[path] = matches[0]
    consuming_steps = [str(step.get("id") or "") for step in steps
                       if isinstance(step, dict)
                       and capture_rel.as_posix() in (step.get("inputs") or [])]
    if not consuming_steps:
        raise FinalRecordsRefused("dependency capture manifest is not consumed by a run-plan step")

    return [{
        "input_id": input_id, "input_kind": "elibrary_dependency_capture_bundle",
        "adapter": row.get("adapter"), "source_system": row.get("source_system"),
        "classification": row.get("classification"),
        "classification_basis": row.get("classification_basis"),
        "audit_claim": claims_by_id[input_id], "entities": row.get("entities") or [],
        "consumers": row.get("consumers") or [],
        "capture": {"manifest": capture_identity, "pre_capture_index": before_identity,
                    "capture_log": log_identity, "cache_before": before_claim,
                    "cache_after": after_claim, "new_entries": len(cache_objects),
                    "required_failed_build_keys": sorted(captured_required),
                    "run_plan_declarations": declared_rows,
                    "run_plan_consuming_steps": consuming_steps},
        "cache_objects": cache_objects,
        "build_a_application": {"units": application_units, "accepted": True},
        "disposition": "integrated_in_build_a",
        "acceptance_result": "accepted_for_candidate",
        "residual_limitation": row.get("residual_limitation"),
        "evidence": row.get("evidence") or [],
    }]


def _additional_input_records(
        root: pathlib.Path, con: sqlite3.Connection, draft: Mapping[str, Any],
        claims_by_id: Mapping[str, dict]) -> Tuple[List[dict], Dict[str, int]]:
    accession_rows = [row for row in draft["rows"]
                      if row.get("input_kind", "accession_package") == "accession_package"]
    search_rows = [row for row in draft["rows"]
                   if row.get("input_kind") == "elibrary_advanced_search_response"]
    dependency_rows = [row for row in draft["rows"]
                       if row.get("input_kind") == "elibrary_dependency_capture_bundle"]
    if len(accession_rows) + len(search_rows) + len(dependency_rows) != len(draft["rows"]):
        raise FinalRecordsRefused("additional input inventory contains an unknown input kind")
    accession_claims = {
        str(row["accession_number"]): claims_by_id[str(row["input_id"])]
        for row in accession_rows}
    accession_doc = dict(draft)
    accession_doc["rows"] = accession_rows
    accession_final, _ = _input_records(root, con, accession_doc, accession_claims)
    for record in accession_final:
        record["input_kind"] = "accession_package"
    search_final = _search_response_input_records(root, con, search_rows, claims_by_id)
    dependency_final = _dependency_capture_input_records(
        root, con, dependency_rows, claims_by_id)
    final = sorted(accession_final + search_final + dependency_final,
                   key=lambda row: row["input_id"])
    return final, dict(collections.Counter(row["disposition"] for row in final))


def _docket_base(value: Any) -> str:
    """Normalise only eLibrary's occurrence suffix, not the docket identity."""
    docket = str(value or "").strip().upper()
    parts = docket.split("-")
    if len(parts) >= 3 and re.fullmatch(r"\d{3}", parts[-1]):
        return "-".join(parts[:-1])
    return docket


def _filing_association_integrity(
        root: pathlib.Path, con: sqlite3.Connection) -> Tuple[Dict[str, Any], List[str]]:
    """Verify fresh-build occurrence relations and their consumer export.

    ``filings.entity_key`` is retained for compatibility, but no final Build A
    may rely on that single value as its semantic relationship.  This gate is
    intentionally independent of adapter code and checks every filing, not only
    the accessions which exposed R28/R29.
    """
    failures: List[str] = []
    required_columns = {
        "source_system", "filing_id", "entity_key", "association_role",
        "facility_key", "evidence_ref",
    }
    columns = set(_table_columns(con, "filing_entities"))
    if not required_columns.issubset(columns):
        return ({"table_columns": sorted(columns)},
                ["filing_entities schema is absent or incomplete"])

    filings = [dict(row) for row in con.execute(
        "SELECT source_system,filing_id,entity_key FROM filings "
        "ORDER BY source_system,filing_id")]
    relations = [dict(row) for row in con.execute(
        "SELECT source_system,filing_id,entity_key,association_role,facility_key,"
        "evidence_ref FROM filing_entities "
        "ORDER BY source_system,filing_id,entity_key")]
    by_filing: Dict[Tuple[str, str], List[dict]] = collections.defaultdict(list)
    invalid_roles = []
    empty_evidence = []
    compatibility_rows = []
    for relation in relations:
        key = (str(relation["source_system"]), str(relation["filing_id"]))
        by_filing[key].append(relation)
        identity = "/".join((*key, str(relation["entity_key"])))
        if relation.get("association_role") not in FILING_ENTITY_ROLES:
            invalid_roles.append(identity)
        if not str(relation.get("evidence_ref") or "").strip():
            empty_evidence.append(identity)
        if relation.get("association_role") == "compatibility_anchor":
            compatibility_rows.append(identity)

    missing_relations = []
    anchor_without_relation = []
    for filing in filings:
        key = (str(filing["source_system"]), str(filing["filing_id"]))
        related = by_filing.get(key, [])
        if not related:
            missing_relations.append("/".join(key))
        elif str(filing["entity_key"]) not in {
                str(row["entity_key"]) for row in related}:
            anchor_without_relation.append("/".join(key))

    cross_rows = [dict(row) for row in con.execute(
        "SELECT o.observation_id,o.entity_key,o.metric_id,o.source_system,o.filing_id,"
        "f.entity_key AS compatibility_entity,fe.association_role,fe.facility_key,"
        "fe.evidence_ref FROM observations o JOIN filings f "
        "ON f.source_system=o.source_system AND f.filing_id=o.filing_id "
        "LEFT JOIN filing_entities fe ON fe.source_system=o.source_system "
        "AND fe.filing_id=o.filing_id AND fe.entity_key=o.entity_key "
        "WHERE o.source_system='eLibrary' AND COALESCE(o.filing_id,'')<>'' "
        "ORDER BY o.observation_id")]
    entity_assets: Dict[str, set] = collections.defaultdict(set)
    asset_groups = {}
    for asset in con.execute(
            "SELECT m.entity_key,m.asset_id,a.group_key FROM asset_entity_map m "
            "JOIN assets a ON a.asset_id=m.asset_id"):
        entity_assets[str(asset[0])].add(str(asset[1]))
        asset_groups[str(asset[1])] = str(asset[2] or "")
    authority_assets: Dict[str, set] = collections.defaultdict(set)
    for authority in con.execute(
            "SELECT docket,asset_id FROM asset_dockets WHERE role=?",
            (LNG_AUTHORITY_ROLE,)):
        authority_assets[_docket_base(authority[0])].add(str(authority[1]))
    filing_dockets: Dict[Tuple[str, str], set] = collections.defaultdict(set)
    for docket in con.execute(
            "SELECT source_system,filing_id,docket FROM filing_dockets"):
        filing_dockets[(str(docket[0]), str(docket[1]))].add(
            _docket_base(docket[2]))
    invalid_cross_observations = []
    for observation in cross_rows:
        relation_role = str(observation.get("association_role") or "")
        if not relation_role or relation_role == "compatibility_anchor":
            invalid_cross_observations.append(str(observation["observation_id"]))
            continue
        if observation["entity_key"] == observation["compatibility_entity"]:
            continue
        if relation_role == "commission_docket_subject":
            continue
        facility_key = str(observation.get("facility_key") or "")
        target_assets = entity_assets.get(str(observation["entity_key"]), set())
        anchor_assets = entity_assets.get(
            str(observation["compatibility_entity"]), set())
        facility_matches_entities = bool(facility_key) and any(
            asset_groups.get(target) == facility_key == asset_groups.get(anchor)
            for target in target_assets for anchor in anchor_assets)
        authority_matches_both = any(
            bool(authority_assets.get(docket, set()) & target_assets)
            and bool(authority_assets.get(docket, set()) & anchor_assets)
            for docket in filing_dockets.get(
                (str(observation["source_system"]), str(observation["filing_id"])), set()))
        metric_ok = (
            (relation_role == "named_filer"
             and observation["metric_id"] in LNG_NAMED_FILER_METRICS)
            or (relation_role == "facility_subject"
                and observation["metric_id"] in LNG_SHARED_METRICS)
        )
        if (relation_role not in {"named_filer", "facility_subject"}
                or not metric_ok or not facility_matches_entities
                or not authority_matches_both):
            invalid_cross_observations.append(str(observation["observation_id"]))

    header, export_rows, _ = _csv_rows(
        root / "exports" / "filing_inventory.csv", "filing association export")
    required_export = {"source_system", "filing_id", "associated_entity_keys",
                       "associated_entity_roles"}
    export_bad = []
    export_by_key: Dict[Tuple[str, str], dict] = {}
    if not required_export.issubset(header):
        failures.append("filing_inventory.csv omits occurrence-association columns")
    else:
        for export in export_rows:
            key = (str(export.get("source_system") or ""),
                   str(export.get("filing_id") or ""))
            if key in export_by_key:
                export_bad.append("/".join(key) + ":duplicate")
            export_by_key[key] = export
        for filing in filings:
            key = (str(filing["source_system"]), str(filing["filing_id"]))
            related = by_filing.get(key, [])
            expected_keys = "|".join(str(row["entity_key"]) for row in related)
            expected_roles = "|".join(
                f"{row['entity_key']}:{row['association_role']}" for row in related)
            exported = export_by_key.get(key)
            if (exported is None
                    or str(exported.get("associated_entity_keys") or "") != expected_keys
                    or str(exported.get("associated_entity_roles") or "") != expected_roles):
                export_bad.append("/".join(key))
        for key in set(export_by_key) - {
                (str(row["source_system"]), str(row["filing_id"])) for row in filings}:
            export_bad.append("/".join(key) + ":export_only")

    if not filings:
        failures.append("Build A contains no filing occurrences")
    if missing_relations:
        failures.append("filings lack occurrence-specific entity associations")
    if anchor_without_relation:
        failures.append("compatibility owners are absent from filing associations")
    if invalid_roles or empty_evidence:
        failures.append("filing associations have invalid roles or empty evidence")
    if compatibility_rows:
        failures.append("fresh Build A retains migration-only compatibility anchors")
    if invalid_cross_observations:
        failures.append("cross-entity document observations lack an appropriate relation")
    if export_bad:
        failures.append("filing inventory dropped or changed entity associations")
    checks = {
        "filings": len(filings), "associations": len(relations),
        "filings_without_association": missing_relations,
        "anchors_without_association": anchor_without_relation,
        "invalid_role_rows": invalid_roles, "empty_evidence_rows": empty_evidence,
        "compatibility_anchor_rows": compatibility_rows,
        "cross_entity_document_observations": sum(
            row["entity_key"] != row["compatibility_entity"] for row in cross_rows),
        "invalid_cross_entity_observations": invalid_cross_observations,
        "filing_inventory_rows": len(export_rows),
        "filing_inventory_association_mismatches": sorted(export_bad),
    }
    return checks, failures


def _lng_routing_build_a_gate(
        root: pathlib.Path, con: sqlite3.Connection,
        association_checks: Mapping[str, Any],
        association_failures: Sequence[str]) -> Dict[str, Any]:
    """Verify the frozen LNG routing input, authority and applied occurrences."""
    failures = list(association_failures)
    seed_rel = pathlib.Path(*LNG_ROUTING_SEED.parts)
    seed_path = root / seed_rel
    seed_identity = _expected_identity(
        seed_path,
        {"bytes": LNG_ROUTING_SEED_BYTES, "sha256": LNG_ROUTING_SEED_SHA256},
        "versioned LNG routing/source index")
    seed_identity["path"] = LNG_ROUTING_SEED.as_posix()
    seed_header, seed_rows, _ = _csv_rows(seed_path, "versioned LNG routing/source index")
    expected_header = [
        "entity", "facility", "assertion_type", "docket", "accession",
        "filed_date", "class_type", "document_title", "retrievable",
        "text_layer", "evidence_note", "confidence",
    ]
    if seed_header != expected_header or len(seed_rows) != 64:
        failures.append("LNG routing/source index is not the exact 64-row schema")
    dockets_from_seed: Dict[str, set] = collections.defaultdict(set)
    for seed_row in seed_rows:
        accession = str(seed_row.get("accession") or "")
        if not ACCESSION.fullmatch(accession):
            failures.append("LNG routing/source index has an invalid accession")
            continue
        for docket in str(seed_row.get("docket") or "").split(","):
            base = _docket_base(docket)
            if base:
                dockets_from_seed[base].add(accession)

    _, plan = _load_json(root / "config" / "run_plan.json", "run plan")
    declared = [item for item in (plan.get("required_inputs") or [])
                if isinstance(item, dict)
                and item.get("path") == LNG_ROUTING_SEED.as_posix()]
    plan_steps = [step for step in (plan.get("steps") or [])
                  if isinstance(step, dict)
                  and LNG_ROUTING_SEED.as_posix() in (step.get("inputs") or [])]
    if (len(declared) != 1
            or declared[0].get("bytes") not in (None, LNG_ROUTING_SEED_BYTES)
            or declared[0].get("sha256") != LNG_ROUTING_SEED_SHA256
            or not plan_steps):
        failures.append("exact LNG routing input is not hash-declared in an executable step")

    authority_rows = [dict(row) for row in con.execute(
        "SELECT asset_id,docket,role,evidence_ref FROM asset_dockets "
        "ORDER BY docket,asset_id")]
    authority_pairs = sorted([
        {"asset_id": str(row["asset_id"]), "docket": str(row["docket"])}
        for row in authority_rows], key=lambda row: (row["asset_id"], row["docket"]))
    authority_digest = _sha(json.dumps(
        authority_pairs, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if len(authority_rows) != 44 or authority_digest != LNG_AUTHORITY_PAIR_SHA256:
        failures.append("asset_dockets is not the exact reviewed 44-row LNG authority map")

    _, universe_rows, _ = _csv_rows(root / "config" / "universe.csv", "frozen universe")
    universe_by_asset = {str(row.get("asset_id") or ""): row for row in universe_rows}
    invalid_authority_evidence = []
    seed_prefix = (
        f"{LNG_ROUTING_SEED.as_posix()}#sha256={LNG_ROUTING_SEED_SHA256}")
    for authority in authority_rows:
        asset_id = str(authority["asset_id"])
        docket = str(authority["docket"])
        evidence = str(authority.get("evidence_ref") or "")
        if authority.get("role") != LNG_AUTHORITY_ROLE:
            invalid_authority_evidence.append(f"{asset_id}/{docket}:role")
            continue
        if docket in dockets_from_seed:
            expected = (f"{seed_prefix};docket={docket};accessions="
                        + ",".join(sorted(dockets_from_seed[docket])))
            if evidence != expected:
                invalid_authority_evidence.append(f"{asset_id}/{docket}:seed-evidence")
        else:
            universe_row = universe_by_asset.get(asset_id) or {}
            expected = f"config/universe.csv#asset_id={asset_id};docket={docket}"
            source_text = " ".join(str(value or "") for value in universe_row.values())
            if docket != "CP15-161" or evidence != expected or docket not in source_text:
                invalid_authority_evidence.append(f"{asset_id}/{docket}:unsupported")
    if invalid_authority_evidence:
        failures.append("LNG asset authority rows have missing or mismatched evidence")

    required_accessions = (
        "20070216-3045", "20160601-4008", "20120416-3033",
        "20141230-3043", "20041221-3094", "20070920-3066",
    )
    occurrence_checks = {}
    for accession in required_accessions:
        filings = [dict(row) for row in con.execute(
            "SELECT source_system,filing_id,entity_key,form,content_hash "
            "FROM filings WHERE source_system='eLibrary' AND filing_id=?",
            (accession,))]
        relations = [dict(row) for row in con.execute(
            "SELECT entity_key,association_role,facility_key,evidence_ref "
            "FROM filing_entities WHERE source_system='eLibrary' AND filing_id=? "
            "ORDER BY entity_key", (accession,))]
        dockets = sorted({_docket_base(row[0]) for row in con.execute(
            "SELECT docket FROM filing_dockets WHERE source_system='eLibrary' "
            "AND filing_id=?", (accession,)) if row[0]})
        documents = [dict(row) for row in con.execute(
            "SELECT document_id,availability,byte_size,content_hash FROM documents "
            "WHERE source_system='eLibrary' AND filing_id=?", (accession,))]
        observations = int(con.execute(
            "SELECT COUNT(*) FROM observations WHERE source_system='eLibrary' "
            "AND filing_id=?", (accession,)).fetchone()[0])
        seed_dockets = sorted(
            docket for docket, accessions in dockets_from_seed.items()
            if accession in accessions)
        valid_documents = [document for document in documents
                           if document.get("availability") == "retrieved"
                           and int(document.get("byte_size") or 0) > 0
                           and HEX64.fullmatch(str(document.get("content_hash") or ""))]
        occurrence_checks[accession] = {
            "filings": filings, "relations": relations, "dockets": dockets,
            "seed_dockets": seed_dockets, "documents": documents,
            "observations": observations,
        }
        if (len(filings) != 1 or filings[0].get("form") != "eLibrary document"
                or not HEX64.fullmatch(str(filings[0].get("content_hash") or ""))
                or not relations
                or any(row.get("association_role") == "compatibility_anchor"
                       or not str(row.get("evidence_ref") or "").strip()
                       for row in relations)
                or not (set(dockets) & set(seed_dockets))
                or not valid_documents or observations <= 0):
            failures.append(f"required LNG occurrence {accession} is not fully applied")

    return {
        "accepted": not failures,
        "checks": {
            "seed": seed_identity, "seed_rows": len(seed_rows),
            "run_plan_entry": declared, "run_plan_consuming_steps": [
                step.get("id") for step in plan_steps],
            "authority_rows": len(authority_rows),
            "authority_pair_sha256": authority_digest,
            "invalid_authority_evidence": invalid_authority_evidence,
            "filing_association_integrity": association_checks,
            "required_occurrences": occurrence_checks,
        },
        "failures": failures,
    }


def _wrong_occurrence_entity_population(
        con: sqlite3.Connection, accession: str, entity: str) -> Dict[str, int]:
    """Count every persisted consumer of a prohibited target/occurrence pair."""
    counts = {
        "observations": int(con.execute(
            "SELECT COUNT(*) FROM observations WHERE entity_key=? "
            "AND accession_number=?", (entity, accession)).fetchone()[0]),
        "filing_entities": int(con.execute(
            "SELECT COUNT(*) FROM filing_entities WHERE source_system='eLibrary' "
            "AND filing_id=? AND entity_key=?", (accession, entity)).fetchone()[0]),
        "run_input_inventory": int(con.execute(
            "SELECT COUNT(*) FROM run_input_inventory WHERE adapter='elibrary_docs' "
            "AND entity_cid=? AND accession_number=?", (entity, accession)).fetchone()[0]),
        "document_facts": int(con.execute(
            "SELECT COUNT(*) FROM document_facts df JOIN documents d "
            "ON d.document_id=df.document_id WHERE df.entity_key=? "
            "AND d.accession_number=?", (entity, accession)).fetchone()[0]),
        "events": int(con.execute(
            "SELECT COUNT(*) FROM events WHERE entity_key=? AND accession_number=?",
            (entity, accession)).fetchone()[0]),
    }
    historical = 0
    for version in con.execute(
            "SELECT row_json FROM observation_versions "
            "WHERE source_system='eLibrary' AND filing_id=?", (accession,)):
        try:
            row = json.loads(str(version[0]))
        except (TypeError, json.JSONDecodeError):
            historical += 1
            continue
        if (str(row.get("entity_key") or "") == entity
                and str(row.get("accession_number") or row.get("filing_id") or "")
                == accession):
            historical += 1
    counts["observation_versions"] = historical
    return counts


def _r29_sibling_contamination_gate(
        root: pathlib.Path, con: sqlite3.Connection,
        association_checks: Mapping[str, Any],
        association_failures: Sequence[str]) -> Dict[str, Any]:
    failures = list(association_failures)
    bad_observation_ids = (
        "obs-1c494d241863969dfac2cf7a59124ec0",
        "obs-552f6fcbe0c3867995236e8b2e34c587",
        "obs-40400bea4371a78fa5cd19b418156b16",
        "obs-6e9019496aaa50e8b034a26141661b9e",
        "obs-5c3c6a3484da21b378828a79eaa333dd",
    )
    current_bad = [row[0] for row in con.execute(
        "SELECT observation_id FROM observations WHERE observation_id IN (?,?,?,?,?) "
        "ORDER BY observation_id", bad_observation_ids)]
    historical_bad = [row[0] for row in con.execute(
        "SELECT DISTINCT observation_id FROM observation_versions "
        "WHERE observation_id IN (?,?,?,?,?) ORDER BY observation_id",
        bad_observation_ids)]
    if current_bad or historical_bad:
        failures.append("known sibling-contaminated observation IDs remain stored")

    prohibited_pairs = (
        ("20240903-5081", "C005518"),
        ("20260522-5206", "C000606"),
        ("20260522-5237", "C000606"),
        ("20240918-5014", "C001049"),
        ("20260414-5238", "C000433"),
    )
    prohibited_population = {
        f"{accession}/{entity}": _wrong_occurrence_entity_population(
            con, accession, entity)
        for accession, entity in prohibited_pairs}
    if any(any(counts.values()) for counts in prohibited_population.values()):
        failures.append("known wrong entity/accession combinations remain persisted")

    # Each rejected sibling occurrence must survive under its actual legal
    # submitter.  This distinguishes a strict actor boundary from a selector
    # which merely rejects every similarly named filing.
    correct_submitters = {
        "20240903-5081": "C010176",
        "20260522-5206": "C008985",
        "20260522-5237": "C000832",
        "20240918-5014": "C000629",
        "20260414-5238": "C000231",
        "20260522-5194": "C000606",
        "20201103-5131": "C004609",
    }
    correct_occurrences = {}
    for accession, entity in correct_submitters.items():
        filings = [dict(row) for row in con.execute(
            "SELECT entity_key,form,content_hash FROM filings "
            "WHERE source_system='eLibrary' AND filing_id=?", (accession,))]
        relations = [dict(row) for row in con.execute(
            "SELECT entity_key,association_role,evidence_ref FROM filing_entities "
            "WHERE source_system='eLibrary' AND filing_id=? ORDER BY entity_key",
            (accession,))]
        documents = int(con.execute(
            "SELECT COUNT(*) FROM documents WHERE source_system='eLibrary' "
            "AND filing_id=?", (accession,)).fetchone()[0])
        correct_occurrences[accession] = {
            "expected_submitter": entity, "filings": filings,
            "relations": relations, "documents": documents,
        }
        matching = [relation for relation in relations
                    if relation.get("entity_key") == entity
                    and relation.get("association_role") == "named_filer"
                    and str(relation.get("evidence_ref") or "").strip()]
        if (len(filings) != 1 or filings[0].get("entity_key") != entity
                or filings[0].get("form") != "eLibrary document"
                or not matching or documents <= 0):
            failures.append(f"correct legal-submitter occurrence {accession} was not preserved")

    required_target_dockets = {
        "C005518": ("IS23-573", "IS25-632"),
        "C000606": ("IS26-291", "IS26-220"),
    }
    target_docket_occurrences = {}
    for entity, required in required_target_dockets.items():
        found = collections.defaultdict(list)
        for row in con.execute(
                "SELECT fe.filing_id,fd.docket FROM filing_entities fe "
                "JOIN filing_dockets fd ON fd.source_system=fe.source_system "
                "AND fd.filing_id=fe.filing_id WHERE fe.source_system='eLibrary' "
                "AND fe.entity_key=? AND fe.association_role='named_filer' "
                "ORDER BY fe.filing_id,fd.docket", (entity,)):
            found[_docket_base(row[1])].append(str(row[0]))
        target_docket_occurrences[entity] = {
            docket: sorted(found.get(docket, [])) for docket in required}
        if any(not found.get(docket) for docket in required):
            failures.append(f"target-owned proceedings are incomplete for {entity}")

    package_observations = int(con.execute(
        "SELECT COUNT(*) FROM observations WHERE entity_key='C000606' "
        "AND accession_number='20260522-5194'").fetchone()[0])
    if package_observations <= 0:
        failures.append("correct ONEOK NGL package 20260522-5194 has no applied observations")

    commission_accession = "20251029-3064"
    commission_filings = [dict(row) for row in con.execute(
        "SELECT entity_key,form FROM filings WHERE source_system='eLibrary' "
        "AND filing_id=?", (commission_accession,))]
    commission_relations = [dict(row) for row in con.execute(
        "SELECT entity_key,association_role,evidence_ref FROM filing_entities "
        "WHERE source_system='eLibrary' AND filing_id=? ORDER BY entity_key",
        (commission_accession,))]
    commission_dockets = sorted({_docket_base(row[0]) for row in con.execute(
        "SELECT docket FROM filing_dockets WHERE source_system='eLibrary' "
        "AND filing_id=?", (commission_accession,))})
    if (len(commission_filings) != 1 or len(commission_relations) != 7
            or len(commission_dockets) != 9
            or any(row.get("association_role") != "commission_docket_subject"
                   or not str(row.get("evidence_ref") or "").strip()
                   for row in commission_relations)):
        failures.append("genuine shared Commission order was not preserved as 7-by-9 relation")

    # Surface the exact export values for the accessions which exposed the
    # defect.  The all-filing association gate above already compares these to
    # the database, so non-empty rows here are hash-bound consumer evidence.
    _, filing_exports, _ = _csv_rows(
        root / "exports" / "filing_inventory.csv", "filing association export")
    export_by_accession = {
        str(row.get("filing_id") or ""): {
            "entity_key": row.get("entity_key"),
            "associated_entity_keys": row.get("associated_entity_keys"),
            "associated_entity_roles": row.get("associated_entity_roles"),
        }
        for row in filing_exports if row.get("source_system") == "eLibrary"}
    relevant_exports = {
        accession: export_by_accession.get(accession)
        for accession in sorted(set(correct_submitters) | {commission_accession})}
    if any(not row or not row.get("associated_entity_keys")
           or not row.get("associated_entity_roles")
           for row in relevant_exports.values()):
        failures.append("filing inventory omits relevant R29 occurrence associations")

    return {
        "accepted": not failures,
        "checks": {
            "known_bad_observation_ids": list(bad_observation_ids),
            "current_bad_observation_ids": current_bad,
            "historical_bad_observation_ids": historical_bad,
            "prohibited_pair_populations": prohibited_population,
            "correct_submitter_occurrences": correct_occurrences,
            "target_docket_occurrences": target_docket_occurrences,
            "correct_oneok_ngl_package_observations": package_observations,
            "shared_commission_order": {
                "accession": commission_accession,
                "filings": commission_filings,
                "relations": commission_relations,
                "dockets": commission_dockets,
            },
            "relevant_filing_inventory_rows": relevant_exports,
            "filing_association_integrity": association_checks,
        },
        "failures": failures,
    }


def _r36_named_lng_filer_gate(
        root: pathlib.Path, con: sqlite3.Connection,
        association_checks: Mapping[str, Any],
        association_failures: Sequence[str]) -> Dict[str, Any]:
    """Bind the repaired named-filer boundary to its exact Build-A outputs."""
    failures = list(association_failures)

    registry_header, registry_rows, _ = _csv_rows(
        root / "config" / "metric_registry.csv", "metric registry for R36")
    if not {"metric_id", "adapter"}.issubset(registry_header):
        failures.append("metric registry omits the metric_id/adapter contract")
        registry_lng = set()
    else:
        registry_lng = {
            str(row.get("metric_id") or "") for row in registry_rows
            if row.get("adapter") == "lng"}
        if registry_lng != LNG_NAMED_FILER_METRICS:
            failures.append("named-filer LNG metric population differs from the fixed registry")

    expected_ids = set(R36_NAMED_FILER_OBSERVATIONS)
    placeholders = ",".join("?" for _ in expected_ids)
    rows = [dict(row) for row in con.execute(
        "SELECT o.observation_id,o.entity_key,o.metric_id,o.source_system,o.filing_id,"
        "f.entity_key AS compatibility_entity,o.version_status,"
        "target.association_role,target.facility_key,"
        "anchor.association_role AS anchor_role,anchor.facility_key AS anchor_facility "
        "FROM observations o JOIN filings f "
        "ON f.source_system=o.source_system AND f.filing_id=o.filing_id "
        "LEFT JOIN filing_entities target ON target.source_system=o.source_system "
        "AND target.filing_id=o.filing_id AND target.entity_key=o.entity_key "
        "LEFT JOIN filing_entities anchor ON anchor.source_system=f.source_system "
        "AND anchor.filing_id=f.filing_id AND anchor.entity_key=f.entity_key "
        f"WHERE o.observation_id IN ({placeholders}) ORDER BY o.observation_id",
        tuple(sorted(expected_ids)))]
    by_id = {str(row["observation_id"]): row for row in rows}
    if len(rows) != len(by_id) or set(by_id) != expected_ids:
        failures.append("the exact eight repaired named-filer observations are not present")

    # Recompute the common reviewed facility authority instead of trusting the
    # coverage artefact's association label.
    entity_assets: Dict[str, Dict[str, str]] = collections.defaultdict(dict)
    for row in con.execute(
            "SELECT m.entity_key,m.asset_id,a.group_key FROM asset_entity_map m "
            "JOIN assets a ON a.asset_id=m.asset_id"):
        entity_assets[str(row[0])][str(row[1])] = str(row[2] or "")
    authority_assets: Dict[str, set] = collections.defaultdict(set)
    for row in con.execute(
            "SELECT docket,asset_id FROM asset_dockets WHERE role=?",
            (LNG_AUTHORITY_ROLE,)):
        authority_assets[_docket_base(row[0])].add(str(row[1]))
    dockets_by_filing: Dict[str, set] = collections.defaultdict(set)
    for row in con.execute(
            "SELECT filing_id,docket FROM filing_dockets "
            "WHERE source_system='eLibrary'"):
        dockets_by_filing[str(row[0])].add(_docket_base(row[1]))

    observed_rows = {}
    for observation_id, expected in R36_NAMED_FILER_OBSERVATIONS.items():
        row = by_id.get(observation_id)
        if row is None:
            continue
        actual = {
            key: row.get(key) for key in (
                "filing_id", "entity_key", "compatibility_entity", "metric_id")}
        actual.update({
            "source_system": row.get("source_system"),
            "association_role": row.get("association_role"),
            "facility_key": row.get("facility_key"),
            "anchor_role": row.get("anchor_role"),
            "anchor_facility": row.get("anchor_facility"),
        })
        expected_tuple = {
            key: expected[key] for key in (
                "filing_id", "entity_key", "compatibility_entity", "metric_id")}
        expected_tuple.update({
            "source_system": "eLibrary", "association_role": "named_filer",
            "facility_key": expected["facility_key"], "anchor_role": "named_filer",
            "anchor_facility": expected["facility_key"],
        })
        if actual != expected_tuple:
            failures.append(f"R36 observation tuple changed for {observation_id}")

        target_assets = entity_assets.get(expected["entity_key"], {})
        anchor_assets = entity_assets.get(expected["compatibility_entity"], {})
        common = []
        for docket in dockets_by_filing.get(expected["filing_id"], set()):
            authorised = authority_assets.get(docket, set())
            if any(left in authorised and right in authorised
                   and target_assets[left]
                   and target_assets[left] == anchor_assets[right]
                   for left in target_assets for right in anchor_assets):
                common.append(docket)
        common = tuple(sorted(set(common)))
        if common != expected["common_dockets"]:
            failures.append(
                f"R36 reviewed common authority changed for {observation_id}")
        actual["common_dockets"] = list(common)
        observed_rows[observation_id] = actual

    expanded_metrics = {
        "lng_liquefaction_capacity", "lng_material_order",
        "lng_status_authorised", "lng_status_requested",
    }
    expanded_placeholders = ",".join("?" for _ in expanded_metrics)
    expanded_ids = {str(row[0]) for row in con.execute(
        "SELECT o.observation_id FROM observations o JOIN filings f "
        "ON f.source_system=o.source_system AND f.filing_id=o.filing_id "
        "WHERE o.source_system='eLibrary' AND o.entity_key<>f.entity_key "
        f"AND o.metric_id IN ({expanded_placeholders})",
        tuple(sorted(expanded_metrics)))}
    if expanded_ids != expected_ids:
        failures.append("the cross-anchor expanded LNG metric population is not exactly eight")

    _, coverage_generation = _load_json(
        root / "verification" / "coverage_generation.json",
        "R36 coverage generation evidence")
    quality = (coverage_generation.get("document_occurrence_quality")
               if isinstance(coverage_generation, dict) else None)
    quality_rows = quality.get("rows") if isinstance(quality, dict) else None
    if not isinstance(quality_rows, list):
        failures.append("coverage generation omits document occurrence quality rows")
        quality_rows = []
    relevant_quality = [row for row in quality_rows
                        if isinstance(row, dict)
                        and row.get("observation_id") in expected_ids]
    quality_by_id = {str(row["observation_id"]): row for row in relevant_quality}
    if len(relevant_quality) != len(quality_by_id) or set(quality_by_id) != expected_ids:
        failures.append("coverage quality does not contain exactly the eight R36 rows")
    quality_extract = {}
    for observation_id, expected in R36_NAMED_FILER_OBSERVATIONS.items():
        row = quality_by_id.get(observation_id)
        if row is None:
            continue
        association = ("reviewed_shared_lng_facility:named_filer:" +
                       ",".join(expected["common_dockets"]))
        expected_quality = {
            "entity_key": expected["entity_key"],
            "metric_id": expected["metric_id"], "source_system": "eLibrary",
            "filing_id": expected["filing_id"],
            "source_health": "ok", "evidence_kind": expected["evidence_kind"],
            "entity_association": association, "entity_association_ok": True,
            "document_filing_association_ok": True,
            "current_version": expected["current_version"],
            "unit_contract_ok": expected["unit_contract_ok"],
            "unit_gate": expected["unit_gate"],
            "included_in_coverage_denominator": False,
            "classification": expected["classification"],
        }
        actual_quality = {key: row.get(key) for key in expected_quality}
        if actual_quality != expected_quality:
            failures.append(f"R36 coverage quality changed for {observation_id}")
        quality_extract[observation_id] = actual_quality

    conditions = " OR ".join("(e.entity_key=? AND e.metric_id=?)"
                             for _ in R36_ROUTE_PAIRS)
    route_params = tuple(value for pair in sorted(R36_ROUTE_PAIRS) for value in pair)
    route_rows = [dict(row) for row in con.execute(
        "SELECT e.slot_id,e.entity_key,e.metric_id,e.denominator_origin,"
        "e.frozen_run_id,m.run_id,m.outcome,m.populated,m.source_matched,m.validated "
        "FROM coverage_expected e JOIN coverage_measured m USING(slot_id) WHERE " +
        conditions + " ORDER BY e.entity_key,e.metric_id", route_params)]
    route_populations = collections.Counter(
        (str(row["entity_key"]), str(row["metric_id"])) for row in route_rows)
    if set(route_populations) != R36_ROUTE_PAIRS \
            or any(count != 1 for count in route_populations.values()):
        failures.append("R36 coverage does not contain exactly five route anchors")
    if any(row.get("denominator_origin") != "document_adapter_anchor"
           or row.get("frozen_run_id") != row.get("run_id") for row in route_rows):
        failures.append("R36 route anchors have a wrong origin or measurement generation")

    _, slot_exports, _ = _csv_rows(
        root / "exports" / "coverage_by_slot.csv", "R36 coverage slot export")
    route_slot_ids = {str(row["slot_id"]) for row in route_rows}
    exported_route_ids = [str(row.get("slot_id") or "") for row in slot_exports
                          if str(row.get("slot_id") or "") in route_slot_ids]
    if len(exported_route_ids) != len(route_slot_ids) \
            or set(exported_route_ids) != route_slot_ids:
        failures.append("coverage_by_slot.csv omits or duplicates an R36 route anchor")

    return {
        "accepted": not failures,
        "checks": {
            "registry_lng_metrics": sorted(registry_lng),
            "exact_observations": observed_rows,
            "expanded_cross_anchor_observation_ids": sorted(expanded_ids),
            "quality_rows": quality_extract,
            "quality_counts": {
                "current_in_window": sum(
                    row.get("classification") == "current_occurrence_quality"
                    for row in relevant_quality),
                "historical_or_noncurrent": sum(
                    row.get("classification") ==
                    "historical_or_noncurrent_occurrence_quality"
                    for row in relevant_quality),
                "filed_occurrences": sum(
                    row.get("evidence_kind") == "filed_occurrence"
                    for row in relevant_quality),
                "indexed_occurrences": sum(
                    row.get("evidence_kind") == "indexed_occurrence"
                    for row in relevant_quality),
            },
            "route_anchors": route_rows,
            "coverage_export_route_slot_ids": sorted(exported_route_ids),
            "filing_association_integrity": dict(association_checks),
        },
        "failures": failures,
    }


def _r37_taxonomy_cache_gate(
        root: pathlib.Path, con: sqlite3.Connection,
        publication: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate the nested taxonomy/cache/plan/replay identity before closure."""
    failures: List[str] = []
    index_path = root / "source_cache" / "index.json"
    pins_path = root / "config" / "taxonomy_pins.json"
    plan_path = root / "config" / "run_plan.json"
    status_path = root / "verification" / "replay_step_status.json"
    coverage_path = root / "verification" / "coverage_generation.json"
    index_raw, index = _load_json(index_path, "R37 source-cache index")
    pins_raw, pins_doc = _load_json(pins_path, "R37 taxonomy pins")
    plan_raw, plan = _load_json(plan_path, "R37 run plan")
    _, status = _load_json(status_path, "R37 replay status")
    coverage_identity = _identity(
        coverage_path, "R37 coverage generation", "verification/coverage_generation.json")
    identities = {
        "source_cache_index": {"path": "source_cache/index.json",
                               "bytes": len(index_raw), "sha256": _sha(index_raw)},
        "taxonomy_pins": {"path": "config/taxonomy_pins.json",
                          "bytes": len(pins_raw), "sha256": _sha(pins_raw)},
        "run_plan": {"path": "config/run_plan.json",
                     "bytes": len(plan_raw), "sha256": _sha(plan_raw)},
        "coverage_generation": coverage_identity,
    }
    if not isinstance(index, dict):
        failures.append("source-cache index is not an object")
        index = {}
    if not isinstance(pins_doc, dict) \
            or pins_doc.get("schema") != "ferc_taxonomy_pins_v1":
        failures.append("taxonomy pins use the wrong schema")
        pins_doc = {}
    if pins_doc.get("coverage_window") != [2024, 2026] \
            or pins_doc.get("as_of") != "2026-09-07":
        failures.append("taxonomy pins use the wrong window or as-of boundary")
    if pins_doc.get("source_cache_index_sha256_at_freeze") != \
            identities["source_cache_index"]["sha256"]:
        failures.append("taxonomy pins are frozen against a different cache index")

    pins = pins_doc.get("pins") if isinstance(pins_doc, dict) else None
    if not isinstance(pins, list):
        pins = []
        failures.append("taxonomy pin population is absent")
    expected_routes = {(form, year) for form in TAXONOMY_PIN_FORMS
                       for year in TAXONOMY_PIN_YEARS}
    actual_routes = [(str(pin.get("form") or ""), pin.get("reporting_year"))
                     for pin in pins if isinstance(pin, dict)]
    if len(actual_routes) != 15 or set(actual_routes) != expected_routes \
            or len(set(actual_routes)) != len(actual_routes):
        failures.append("taxonomy pins are not the exact 15 unique form/year routes")

    pin_checks = []
    valid_pins: Dict[Tuple[str, int], dict] = {}
    for pin in pins:
        if not isinstance(pin, dict):
            failures.append("taxonomy pin row is not an object")
            continue
        form = str(pin.get("form") or "")
        year = pin.get("reporting_year")
        version = str(pin.get("taxonomy_version") or "")
        evidence = str(pin.get("evidence_ref") or "")
        cache_key = str(pin.get("cache_key") or "")
        row_failures = []
        if not isinstance(year, int) or version != f"{year}-04-01":
            row_failures.append("taxonomy version does not match reporting year")
        if "#sha256=" not in evidence:
            row_failures.append("evidence_ref has no content hash")
            url, content_hash = "", ""
        else:
            url, content_hash = evidence.rsplit("#sha256=", 1)
        if not HEX64.fullmatch(content_hash) or cache_key != _sha(url.encode("utf-8")):
            row_failures.append("URL key or content hash is invalid")
        entry = index.get(cache_key) if isinstance(index, dict) else None
        expected_cache_path = f"objects/{content_hash[:2]}/{content_hash}"
        expected_entry = {
            "source_system": "taxonomy", "source_url": url,
            "content_hash": content_hash, "cache_path": expected_cache_path,
        }
        actual_entry = ({key: entry.get(key) for key in expected_entry}
                        if isinstance(entry, dict) else None)
        object_identity = None
        if actual_entry != expected_entry:
            row_failures.append("cache index binding differs")
        elif not isinstance(entry.get("byte_size"), int) or entry["byte_size"] < 1:
            row_failures.append("cache byte size is invalid")
        else:
            object_path = _path_within(
                root / "source_cache" / expected_cache_path,
                root / "source_cache", "R37 taxonomy object")
            try:
                object_identity = _identity(
                    object_path, "R37 taxonomy object",
                    "source_cache/" + expected_cache_path)
            except FinalRecordsRefused as exc:
                row_failures.append(str(exc))
            if object_identity is not None and (
                    object_identity["sha256"] != content_hash
                    or object_identity["bytes"] != entry.get("byte_size")):
                row_failures.append("resident taxonomy object identity differs")
        if row_failures:
            failures.extend(f"{form}/{year}: {message}" for message in row_failures)
        elif isinstance(year, int):
            valid_pins[(form, year)] = {
                "taxonomy_version": version, "url": url,
                "content_hash": content_hash, "cache_key": cache_key,
                "object": object_identity,
            }
        pin_checks.append({"form": form, "reporting_year": year,
                           "taxonomy_version": version, "cache_key": cache_key,
                           "content_hash": content_hash,
                           "object": object_identity, "failures": row_failures})

    required_inputs = plan.get("required_inputs") if isinstance(plan, dict) else None
    plan_steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(required_inputs, list) or not isinstance(plan_steps, list):
        failures.append("run plan has no required-input/step population")
        required_inputs, plan_steps = [], []
    declarations = {}
    for relative, identity in (
            ("source_cache/index.json", identities["source_cache_index"]),
            ("config/taxonomy_pins.json", identities["taxonomy_pins"])):
        matches = [row for row in required_inputs if isinstance(row, dict)
                   and row.get("path") == relative]
        declarations[relative] = matches
        if len(matches) != 1 or matches[0].get("sha256") != identity["sha256"] \
                or matches[0].get("bytes") not in (None, identity["bytes"]):
            failures.append(f"run plan does not hash-declare {relative} exactly")
    coverage_steps = [step for step in plan_steps if isinstance(step, dict)
                      and step.get("id") == "generate_and_measure_coverage"]
    if len(coverage_steps) != 1 or not {
            "source_cache/index.json", "config/taxonomy_pins.json"}.issubset(
                set(coverage_steps[0].get("inputs") or []) if coverage_steps else set()):
        failures.append("coverage step does not consume both cache index and taxonomy pins")

    status_steps = status.get("steps") if isinstance(status, dict) else None
    coverage_status = (status_steps.get("generate_and_measure_coverage")
                       if isinstance(status_steps, dict) else None)
    if not isinstance(status, dict) or status.get("schema") != \
            "ferc_replay_step_status_v2":
        failures.append("replay status uses the wrong schema")
        status = {}
    if status.get("active_plan_sha256") != identities["run_plan"]["sha256"]:
        failures.append("replay status was executed under a different run plan")
    if not isinstance(coverage_status, dict) or coverage_status.get("status") != \
            "complete" or coverage_status.get("exit_code") != 0:
        failures.append("coverage replay step did not complete successfully")
        coverage_status = {}
    status_inputs = ((coverage_status.get("input_identity") or {}).get("inputs")
                     if isinstance(coverage_status, dict) else None)
    if not isinstance(status_inputs, list):
        status_inputs = []
    recorded_inputs = {}
    for relative, identity in (
            ("source_cache/index.json", identities["source_cache_index"]),
            ("config/taxonomy_pins.json", identities["taxonomy_pins"])):
        matches = [row for row in status_inputs if isinstance(row, dict)
                   and row.get("name") == relative]
        recorded_inputs[relative] = matches
        if len(matches) != 1 or matches[0].get("sha256") != identity["sha256"] \
                or matches[0].get("bytes") != identity["bytes"]:
            failures.append(f"coverage replay recorded a different {relative}")
    status_outputs = coverage_status.get("outputs") or []
    coverage_outputs = [row for row in status_outputs if isinstance(row, dict)
                        and row.get("path") == "verification/coverage_generation.json"]
    if len(coverage_outputs) != 1 \
            or coverage_outputs[0].get("sha256") != coverage_identity["sha256"] \
            or coverage_outputs[0].get("bytes") != coverage_identity["bytes"]:
        failures.append("coverage replay status does not bind its generation evidence")
    publication_code = ((publication.get("metadata") or {}).get("code_snapshot")
                        if isinstance(publication, Mapping) else None)
    recorded_code = ((coverage_status.get("input_identity") or {}).get("code_snapshot")
                     if isinstance(coverage_status, dict) else None)
    if publication_code and recorded_code != publication_code:
        failures.append("coverage and published exports use different code snapshots")

    exercised = []
    for route, pin in sorted(valid_pins.items()):
        form, year = route
        filing_count = int(con.execute(
            "SELECT COUNT(*) FROM filings WHERE form=? AND reporting_year=?",
            (form, year)).fetchone()[0])
        if filing_count <= 0:
            continue
        rows = [dict(row) for row in con.execute(
            "SELECT form,taxonomy_version,artefact,url,content_hash,retrieved "
            "FROM taxonomy_sources WHERE form=? AND taxonomy_version=? "
            "AND artefact='entry_point'", (form, pin["taxonomy_version"]))]
        accepted = len(rows) == 1 and rows[0] == {
            "form": form, "taxonomy_version": pin["taxonomy_version"],
            "artefact": "entry_point", "url": pin["url"],
            "content_hash": pin["content_hash"], "retrieved": 1,
        }
        if not accepted:
            failures.append(f"exercised taxonomy route {form}/{year} is not materialized exactly")
        exercised.append({"form": form, "reporting_year": year,
                          "filings": filing_count, "taxonomy_sources": rows,
                          "accepted": accepted})
    if not exercised:
        failures.append("no pinned taxonomy route was exercised by Build A filings")

    coverage_runs = [dict(row) for row in con.execute(
        "SELECT e.frozen_run_id,COUNT(*) AS expected_rows,"
        "COUNT(m.slot_id) AS measured_rows FROM coverage_expected e "
        "LEFT JOIN coverage_measured m USING(slot_id) GROUP BY e.frozen_run_id "
        "ORDER BY e.frozen_run_id")]
    if not coverage_runs or any(row["expected_rows"] != row["measured_rows"]
                                for row in coverage_runs):
        failures.append("coverage database population is absent or incompletely measured")

    return {
        "accepted": not failures,
        "checks": {
            "identities": identities,
            "pin_routes": pin_checks,
            "run_plan_declarations": declarations,
            "coverage_step_inputs": recorded_inputs,
            "coverage_step_status": {
                key: coverage_status.get(key) for key in (
                    "status", "exit_code", "input_digest", "output_digest")},
            "exercised_taxonomy_routes": exercised,
            "coverage_database_generations": coverage_runs,
        },
        "failures": failures,
    }


def _ioc_validation_row(
        root: pathlib.Path, failures: List[str]) -> Dict[str, Any]:
    """Return the one named IOC validation result, requiring an executed pass."""
    validation_path = root / "verification" / "validation_results.json"
    validation_raw, validation = _load_json(
        validation_path,
        "R38/R39 integrated IOC validation")
    results = validation.get("results") if isinstance(validation, dict) else None
    if not isinstance(results, list):
        failures.append("integrated validation omits its result rows")
        return {}
    matches = [dict(row) for row in results if isinstance(row, dict)
               and row.get("check") == IOC_VALIDATION_CHECK]
    if len(matches) != 1:
        failures.append(
            "integrated validation does not contain exactly one named IOC lineage check")
        return {"matches": matches}
    row = matches[0]
    if row.get("status") != "PASS" or row.get("group") != "lineage":
        failures.append("the named IOC lineage validation did not execute and pass")
    detail = str(row.get("detail") or "")
    count_match = re.match(
        r"^([1-9][0-9,]*) present IOC observations pass full persisted redraw\b", detail)
    if not count_match:
        failures.append("IOC validation PASS lacks its nonzero persisted-redraw population")
    else:
        row["reported_present_observation_count"] = int(
            count_match.group(1).replace(",", ""))

    receipt_path = root / "publication_receipt.json"
    receipt_raw, receipt = _load_json(
        receipt_path, "R38/R39 publication receipt")
    _, status = _load_json(
        root / "verification" / "replay_step_status.json",
        "R38/R39 replay status")
    steps = status.get("steps") if isinstance(status, dict) else None
    publish_step = (steps.get("publish_consumer_generation")
                    if isinstance(steps, dict) else None)
    validate_step = (steps.get("validate_integrated_generation")
                     if isinstance(steps, dict) else None)
    boundary_failures = []
    if not isinstance(status, dict) \
            or status.get("schema") != "ferc_replay_step_status_v2":
        boundary_failures.append("replay status uses the wrong schema")
    if not isinstance(publish_step, dict) \
            or publish_step.get("status") != "complete" \
            or publish_step.get("exit_code") != 0 \
            or not str(publish_step.get("output_digest") or ""):
        boundary_failures.append("publication step is not an executed success")
        publish_step = {}
    if not isinstance(validate_step, dict) \
            or validate_step.get("status") != "complete" \
            or validate_step.get("exit_code") != 0:
        boundary_failures.append("validation step is not an executed success")
        validate_step = {}

    receipt_identity = {"path": "publication_receipt.json", "bytes": len(receipt_raw),
                        "sha256": _sha(receipt_raw)}
    validation_identity = {
        "path": "verification/validation_results.json", "bytes": len(validation_raw),
        "sha256": _sha(validation_raw)}
    validation_inputs = ((validate_step.get("input_identity") or {}).get("inputs")
                         if validate_step else None)
    receipt_matches = [item for item in (validation_inputs or [])
                       if isinstance(item, dict)
                       and item.get("path") == receipt_identity["path"]]
    if len(receipt_matches) != 1 \
            or receipt_matches[0].get("bytes") != receipt_identity["bytes"] \
            or receipt_matches[0].get("sha256") != receipt_identity["sha256"]:
        boundary_failures.append("validation step did not consume this publication receipt")

    validation_outputs = validate_step.get("outputs") if validate_step else None
    output_matches = [item for item in (validation_outputs or [])
                      if isinstance(item, dict)
                      and item.get("path") == validation_identity["path"]]
    if len(output_matches) != 1 \
            or output_matches[0].get("bytes") != validation_identity["bytes"] \
            or output_matches[0].get("sha256") != validation_identity["sha256"]:
        boundary_failures.append("validation step did not produce this validation result")

    dependencies = ((validate_step.get("input_identity") or {}).get("dependencies")
                    if validate_step else None)
    if not isinstance(dependencies, dict) \
            or dependencies.get("publish_consumer_generation") != \
            publish_step.get("output_digest"):
        boundary_failures.append("validation step is not bound to the publication output")
    receipt_code = ((receipt.get("metadata") or {}).get("code_snapshot")
                    if isinstance(receipt, dict) else None)
    validation_code = ((validate_step.get("input_identity") or {}).get("code_snapshot")
                       if validate_step else None)
    if not receipt_code or validation_code != receipt_code:
        boundary_failures.append("validation and publication use different code snapshots")
    if boundary_failures:
        failures.extend(boundary_failures)
    row["execution_boundary"] = {
        "accepted": not boundary_failures,
        "publication_receipt": receipt_identity,
        "validation_results": validation_identity,
        "publication_output_digest": publish_step.get("output_digest"),
        "validation_input_digest": validate_step.get("input_digest"),
        "code_snapshot": validation_code,
        "failures": boundary_failures,
    }
    return row


def _structured_ioc_population_purpose(note: Any) -> str:
    try:
        parsed = json.loads(str(note or ""))
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict) \
            or parsed.get("schema") != "ioc_population_note_v1" \
            or not isinstance(parsed.get("purpose"), str):
        return ""
    return parsed["purpose"].strip()


def _r38_ioc_lineage_gate(
        root: pathlib.Path, con: sqlite3.Connection) -> Dict[str, Any]:
    """Bind the IOC-only validator and three-character code to data and exports."""
    failures: List[str] = []
    validation_row = _ioc_validation_row(root, failures)

    registry_header, registry_rows, _ = _csv_rows(
        root / "config" / "metric_registry.csv", "R38 metric registry")
    if not {"metric_id", "adapter"}.issubset(registry_header):
        failures.append("metric registry omits the metric_id/adapter contract")
        ioc_metrics: set = set()
    else:
        ioc_metrics = {str(row.get("metric_id") or "") for row in registry_rows
                       if row.get("adapter") == "ioc"}
        if not ioc_metrics or "ioc_points" not in ioc_metrics:
            failures.append("the exact IOC adapter metric population is absent or malformed")

    population_counts = {"total": 0, "ioc": 0, "foreign": 0,
                         "ioc_observations": 0, "present_ioc_observations": 0}
    if ioc_metrics:
        marks = ",".join("?" for _ in ioc_metrics)
        total = int(con.execute("SELECT COUNT(*) FROM lineage_populations").fetchone()[0])
        row = con.execute(
            "SELECT COUNT(*) AS populations,COUNT(DISTINCT p.observation_id) AS observations,"
            "COUNT(DISTINCT CASE WHEN o.availability='present' THEN p.observation_id END) "
            "AS present_observations FROM lineage_populations p JOIN observations o "
            "ON o.observation_id=p.observation_id "
            f"WHERE o.metric_id IN ({marks})", tuple(sorted(ioc_metrics))).fetchone()
        present_ioc_rows = int(con.execute(
            f"SELECT COUNT(*) FROM observations WHERE metric_id IN ({marks}) "
            "AND availability='present'", tuple(sorted(ioc_metrics))).fetchone()[0])
        population_counts = {
            "total": total, "ioc": int(row[0] or 0), "foreign": total - int(row[0] or 0),
            "ioc_observations": int(row[1] or 0),
            "present_ioc_population_observations": int(row[2] or 0),
            "present_ioc_observations": present_ioc_rows,
        }
        if population_counts["ioc"] <= 0 \
                or population_counts["present_ioc_observations"] <= 0:
            failures.append("Build A has no nonzero IOC population/redraw boundary")
        if population_counts["foreign"] <= 0:
            failures.append("Build A does not exercise the foreign-population exclusion boundary")
        if validation_row.get("reported_present_observation_count") != present_ioc_rows:
            failures.append(
                "IOC validation's reported present count differs from the exact adapter metrics")

    observations = [dict(row) for row in con.execute(
        "SELECT observation_id,entity_key,metric_id,source_system,filing_id,"
        "accession_number,scope,unit,value_text,availability,validation,candidate_count "
        "FROM observations WHERE observation_id=?", (R38_OBSERVATION_ID,))]
    expected_observation = {
        "observation_id": R38_OBSERVATION_ID, "entity_key": "C000255",
        "metric_id": "ioc_points", "source_system": "eLibrary",
        "filing_id": R38_FILING_ID, "accession_number": R38_FILING_ID,
        "scope": ("location detail only | point code 130 "
                  "(code not in the Form 549B manual's table)"),
        "unit": "codes", "value_text": "130 (code not in the Form 549B manual's table)",
        "availability": "present", "validation": "scope_incompatible",
        "candidate_count": 1,
    }
    if len(observations) != 1 or observations[0] != expected_observation:
        failures.append("the exact three-character IOC observation changed or is absent")

    populations = [dict(row) for row in con.execute(
        "SELECT population_id,observation_id,source_system,source_table,filing_ids,"
        "inclusion_rule,exclusion_rule,row_count,candidate_count,excluded_count,"
        "member_key,member_digest,members_sample,note FROM lineage_populations "
        "WHERE population_id=?", (R38_POPULATION_ID,))]
    if len(populations) != 1:
        failures.append("the exact three-character IOC population is absent")
    else:
        population = populations[0]
        expected_population = {
            "population_id": R38_POPULATION_ID,
            "observation_id": R38_OBSERVATION_ID,
            "source_system": "eLibrary", "source_table": "source_facts",
            "filing_ids": json.dumps([R38_FILING_ID]),
            "row_count": 1, "candidate_count": 2017, "excluded_count": 2016,
            "member_key": "filing_id:source_fact_id",
            "member_digest": R38_MEMBER_DIGEST,
            "members_sample": json.dumps([f"{R38_FILING_ID}:{R38_SOURCE_FACT_ID}"]),
        }
        if {key: population.get(key) for key in expected_population} != expected_population \
                or not str(population.get("inclusion_rule") or "").strip() \
                or not str(population.get("exclusion_rule") or "").strip() \
                or _structured_ioc_population_purpose(population.get("note")) != \
                "point_records::130":
            failures.append("the exact three-character IOC population no longer redraws exactly")

    facts = [dict(row) for row in con.execute(
        "SELECT source_system,filing_id,source_fact_id,concept_qname,context_id,"
        "typed_dims_json,value_as_filed FROM source_facts WHERE source_system='eLibrary' "
        "AND filing_id=? AND source_fact_id=?",
        (R38_FILING_ID, R38_SOURCE_FACT_ID))]
    parsed_fact: Dict[str, Any] = {}
    if len(facts) != 1:
        failures.append("the exact point-code 130 source fact is absent")
    else:
        try:
            parsed_fact = json.loads(facts[0].get("typed_dims_json") or "{}")
        except json.JSONDecodeError:
            parsed_fact = {}
        if facts[0].get("concept_qname") != "ferc549b:P" \
                or facts[0].get("context_id") != "contract0000" \
                or parsed_fact.get("point_code") != "130" \
                or not str(facts[0].get("value_as_filed") or "").startswith("P\t130\t"):
            failures.append("the source fact does not preserve the exact filed 130 code")

    edges = [dict(row) for row in con.execute(
        "SELECT input_order,input_role,input_source_system,input_filing_id,"
        "input_source_fact_id,input_population_id,input_concept,input_value,input_unit "
        "FROM lineage_edges WHERE observation_id=? ORDER BY input_order",
        (R38_OBSERVATION_ID,))]
    if len(edges) != 2 \
            or [row.get("input_role") for row in edges] != ["population", "contributing_row"] \
            or any(row.get("input_population_id") != R38_POPULATION_ID for row in edges) \
            or edges[0].get("input_filing_id") != R38_FILING_ID \
            or edges[1].get("input_filing_id") != R38_FILING_ID \
            or edges[1].get("input_source_fact_id") != R38_SOURCE_FACT_ID \
            or edges[1].get("input_value") != "130":
        failures.append("the exact 130 population/contributing-row lineage pair changed")

    _, observation_exports, _ = _csv_rows(
        root / "exports" / "canonical_observations.csv", "R38 observation export")
    exported_observations = [row for row in observation_exports
                             if row.get("observation_id") == R38_OBSERVATION_ID]
    if len(exported_observations) != 1:
        failures.append("canonical_observations.csv omits or duplicates the 130 observation")
    else:
        exported = exported_observations[0]
        export_expected = {
            "entity_key": "C000255", "metric_id": "ioc_points",
            "source_system": "eLibrary", "filing_id": R38_FILING_ID,
            "scope": expected_observation["scope"], "unit": "codes",
            "value": expected_observation["value_text"], "availability": "present",
            "validation": "scope_incompatible", "candidate_count": "1",
        }
        if {key: exported.get(key) for key in export_expected} != export_expected:
            failures.append("the exported three-character IOC observation differs from data")

    _, lineage_exports, _ = _csv_rows(
        root / "exports" / "lineage_edges.csv", "R38 lineage export")
    exported_edges = [row for row in lineage_exports
                      if row.get("observation_id") == R38_OBSERVATION_ID]
    if len(exported_edges) != 2 \
            or [row.get("input_role") for row in exported_edges] != \
            ["population", "contributing_row"] \
            or any(row.get("input_population_id") != R38_POPULATION_ID
                   for row in exported_edges) \
            or exported_edges[1].get("input_source_fact_id") != R38_SOURCE_FACT_ID:
        failures.append("lineage_edges.csv does not preserve the exact 130 lineage pair")

    return {
        "accepted": not failures,
        "checks": {
            "validation": validation_row,
            "ioc_metrics": sorted(ioc_metrics),
            "population_boundary": population_counts,
            "observation": observations,
            "population": populations,
            "source_fact": facts,
            "parsed_point_code": parsed_fact.get("point_code"),
            "lineage_edges": edges,
            "observation_export": exported_observations,
            "lineage_export": exported_edges,
        },
        "failures": failures,
    }


def _r39_blank_ioc_point_code_gate(
        root: pathlib.Path, con: sqlite3.Connection) -> Dict[str, Any]:
    """Retain blank source rows without promoting an empty point-code value."""
    failures: List[str] = []
    validation_row = _ioc_validation_row(root, failures)

    source_facts = [dict(row) for row in con.execute(
        "SELECT source_system,filing_id,source_fact_id,concept_qname,typed_dims_json,"
        "value_as_filed FROM source_facts WHERE source_system='eLibrary' AND filing_id=? "
        "AND concept_qname='ferc549b:P' ORDER BY source_fact_id", (R39_FILING_ID,))]
    blank_facts = []
    malformed_facts = []
    for fact in source_facts:
        try:
            typed = json.loads(fact.get("typed_dims_json") or "{}")
        except json.JSONDecodeError:
            malformed_facts.append(fact.get("source_fact_id"))
            continue
        if not str(typed.get("point_code") or "").strip():
            blank_facts.append({"source_fact_id": fact.get("source_fact_id"),
                                "typed_dims": typed,
                                "value_as_filed": fact.get("value_as_filed")})
    blank_ids = {str(row.get("source_fact_id")) for row in blank_facts}
    source_members = [f"{R39_FILING_ID}:{row['source_fact_id']}"
                      for row in source_facts]
    source_member_digest = _sha("\n".join(sorted(source_members)).encode("utf-8"))
    if len(source_facts) != 1821 or malformed_facts or blank_ids != R39_BLANK_FACT_IDS:
        failures.append("the filing's 1,821 P rows and exact two blank-code facts were not retained")
    for row in blank_facts:
        typed = row["typed_dims"]
        if typed.get("point_code") != "" \
                or str(row.get("value_as_filed") or "").split("\t", 2)[:2] != ["P", ""]:
            failures.append("a blank item-yh source row was normalised to a nonblank code")

    old_observations = int(con.execute(
        "SELECT COUNT(*) FROM observations WHERE observation_id=?",
        (R39_INVALID_OBSERVATION_ID,)).fetchone()[0])
    old_populations = int(con.execute(
        "SELECT COUNT(*) FROM lineage_populations WHERE population_id=?",
        (R39_INVALID_POPULATION_ID,)).fetchone()[0])
    old_edges = int(con.execute(
        "SELECT COUNT(*) FROM lineage_edges WHERE observation_id=?",
        (R39_INVALID_OBSERVATION_ID,)).fetchone()[0])
    if old_observations or old_populations or old_edges:
        failures.append("the known blank-code observation/population/edges still exist")

    point_populations = [dict(row) for row in con.execute(
        "SELECT p.population_id,p.observation_id,p.note,p.inclusion_rule,p.filing_ids,"
        "p.row_count AS population_row_count,"
        "p.candidate_count AS population_candidate_count,o.scope,o.value_text,"
        "o.availability,o.candidate_count AS observation_candidate_count,"
        "o.filing_id,o.unit,o.missing_reason "
        "FROM lineage_populations p JOIN observations o "
        "ON o.observation_id=p.observation_id WHERE o.metric_id='ioc_points' "
        "ORDER BY p.population_id")]
    purposes = {str(row["population_id"]):
                _structured_ioc_population_purpose(row.get("note"))
                for row in point_populations}
    missing_purpose_populations = [row for row in point_populations
                                   if not purposes[str(row["population_id"])] ]
    if missing_purpose_populations:
        failures.append("present IOC point populations lack a structured purpose")
    promoted_blank_populations = [row for row in point_populations
                                  if row.get("availability") == "present"
                                  and purposes[str(row["population_id"])] ==
                                  "point_records::"]
    for row in con.execute(
            "SELECT DISTINCT p.population_id,p.observation_id,e.input_filing_id,"
            "e.input_source_fact_id,s.typed_dims_json FROM lineage_populations p "
            "JOIN observations o ON o.observation_id=p.observation_id "
            "JOIN lineage_edges e ON e.observation_id=p.observation_id "
            "AND e.input_population_id=p.population_id "
            "JOIN source_facts s ON s.source_system=e.input_source_system "
            "AND s.filing_id=e.input_filing_id "
            "AND s.source_fact_id=e.input_source_fact_id "
            "WHERE o.metric_id='ioc_points' AND o.availability='present' "
            "AND e.input_role='contributing_row' ORDER BY p.population_id"):
        item = dict(row)
        try:
            typed = json.loads(item.get("typed_dims_json") or "{}")
        except json.JSONDecodeError:
            typed = {}
        purpose = purposes.get(str(item["population_id"]), "")
        if not str(typed.get("point_code") or "").strip() \
                and purpose != "point_records":
            promoted_blank_populations.append(item)
    if promoted_blank_populations:
        failures.append("a present IOC per-code population still has a blank item-yh code")

    file_heads = [row for row in point_populations
                  if purposes[str(row["population_id"])] == "point_records"]
    heads_by_filing: Dict[str, List[dict]] = collections.defaultdict(list)
    file_head_validation = []
    for row in file_heads:
        heads_by_filing[str(row.get("filing_id") or "")].append(row)
        filing_id = str(row.get("filing_id") or "")
        source_rows = [dict(value) for value in con.execute(
            "SELECT source_fact_id,typed_dims_json FROM source_facts "
            "WHERE source_system='eLibrary' AND filing_id=? "
            "AND concept_qname='ferc549b:P' ORDER BY source_fact_id", (filing_id,))]
        counts: Dict[str, int] = {}
        malformed = []
        for source_row in source_rows:
            try:
                typed = json.loads(source_row.get("typed_dims_json") or "{}")
            except json.JSONDecodeError:
                malformed.append(source_row.get("source_fact_id"))
                continue
            code = str(typed.get("point_code") or "").strip()
            if code:
                counts[code] = counts.get(code, 0) + 1
        census = ", ".join(
            f"{code}={count}" for code, count in
            sorted(counts.items(), key=lambda item: -item[1])[:12])
        differences = []
        try:
            declared_filings = json.loads(row.get("filing_ids") or "[]")
        except json.JSONDecodeError:
            declared_filings = []
        if declared_filings != [filing_id]:
            differences.append("filing_occurrence")
        if malformed:
            differences.append("typed_dimensions")
        if row.get("population_row_count") != len(source_rows) \
                or row.get("population_candidate_count") != len(source_rows) \
                or row.get("observation_candidate_count") != len(source_rows):
            differences.append("candidate_count")
        if row.get("scope") != "location detail only":
            differences.append("scope")
        if census:
            if row.get("availability") != "present":
                differences.append("availability")
            if row.get("value_text") != census:
                differences.append("value_text")
            if row.get("unit") != "codes":
                differences.append("unit")
        else:
            if row.get("availability") != "source_blank":
                differences.append("availability")
            if row.get("value_text") not in (None, ""):
                differences.append("value_text")
            if row.get("unit") not in (None, ""):
                differences.append("unit")
            if not str(row.get("missing_reason") or "").strip():
                differences.append("missing_reason")
        file_head_validation.append({
            "population_id": row["population_id"], "filing_id": filing_id,
            "source_fact_count": len(source_rows), "census": census,
            "malformed_source_fact_ids": malformed, "differences": differences,
        })
        if differences:
            failures.append(
                f"file-level IOC point census {row['population_id']} differs from source facts")
    duplicate_file_heads = {filing_id: [row["population_id"] for row in rows]
                            for filing_id, rows in heads_by_filing.items()
                            if not filing_id or len(rows) != 1}
    if duplicate_file_heads:
        failures.append("IOC filing occurrences do not have exactly one file-level point census")

    heads = [dict(row) for row in con.execute(
        "SELECT observation_id,entity_key,metric_id,source_system,filing_id,scope,unit,"
        "value_text,availability,validation,candidate_count,qa_flags FROM observations "
        "WHERE observation_id=?", (R39_HEAD_OBSERVATION_ID,))]
    warning_fragments = (
        "2 P record(s) have blank item yh",
        "remain in source_facts and the file-level population",
        "not promoted to a present point-code observation",
        "every P record is retained in source_facts",
    )
    if len(heads) != 1:
        failures.append("the filing-level point census head is absent")
    else:
        head = heads[0]
        expected_head = {
            "observation_id": R39_HEAD_OBSERVATION_ID, "entity_key": "C000654",
            "metric_id": "ioc_points", "source_system": "eLibrary",
            "filing_id": R39_FILING_ID, "scope": "location detail only",
            "unit": "codes", "value_text": "S9=1086, S8=580, WR=153",
            "availability": "present", "validation": "scope_incompatible",
            "candidate_count": 1821,
        }
        if {key: head.get(key) for key in expected_head} != expected_head:
            failures.append("the filing-level point census value/count/scope changed")
        if any(fragment not in str(head.get("qa_flags") or "")
               for fragment in warning_fragments):
            failures.append("the filing-level point census omits its blank-code warning")

    head_populations = [dict(row) for row in con.execute(
        "SELECT population_id,observation_id,source_system,source_table,filing_ids,"
        "inclusion_rule,exclusion_rule,row_count,candidate_count,excluded_count,"
        "member_key,member_digest,note FROM lineage_populations WHERE population_id=?",
        (R39_HEAD_POPULATION_ID,))]
    expected_head_population = {
        "population_id": R39_HEAD_POPULATION_ID,
        "observation_id": R39_HEAD_OBSERVATION_ID,
        "source_system": "eLibrary", "source_table": "source_facts",
        "filing_ids": json.dumps([R39_FILING_ID]),
        "inclusion_rule": ("every P (point) record of this filing occurrence, whatever "
                           "its NAESB point code"),
        "exclusion_rule": ("no P record is dropped; D, A, F, H and unclassified rows are "
                           "not point records and never contribute"),
        "row_count": 1821, "candidate_count": 1821, "excluded_count": 0,
        "member_key": "filing_id:source_fact_id",
        "member_digest": source_member_digest,
    }
    if len(head_populations) != 1 \
            or {key: head_populations[0].get(key) for key in expected_head_population} != \
            expected_head_population \
            or _structured_ioc_population_purpose(head_populations[0].get("note")) != \
            "point_records":
        failures.append("the filing-level population no longer retains all 1,821 P rows")

    head_edges = [dict(row) for row in con.execute(
        "SELECT input_order,input_role,input_filing_id,input_population_id "
        "FROM lineage_edges WHERE observation_id=? ORDER BY input_order",
        (R39_HEAD_OBSERVATION_ID,))]
    if len(head_edges) != 1 or head_edges[0] != {
            "input_order": 1, "input_role": "population",
            "input_filing_id": R39_FILING_ID,
            "input_population_id": R39_HEAD_POPULATION_ID}:
        failures.append("the filing-level population edge changed or is absent")

    _, observation_exports, _ = _csv_rows(
        root / "exports" / "canonical_observations.csv", "R39 observation export")
    old_observation_exports = [row for row in observation_exports
                               if row.get("observation_id") == R39_INVALID_OBSERVATION_ID]
    head_exports = [row for row in observation_exports
                    if row.get("observation_id") == R39_HEAD_OBSERVATION_ID]
    if old_observation_exports:
        failures.append("canonical_observations.csv still exports the blank-code observation")
    if len(head_exports) != 1:
        failures.append("canonical_observations.csv omits or duplicates the point census head")
    else:
        exported_head = head_exports[0]
        if exported_head.get("value") != "S9=1086, S8=580, WR=153" \
                or exported_head.get("candidate_count") != "1821" \
                or any(fragment not in str(exported_head.get("qa_flags") or "")
                       for fragment in warning_fragments):
            failures.append("the exported point census value/count/warning differs from data")

    _, lineage_exports, _ = _csv_rows(
        root / "exports" / "lineage_edges.csv", "R39 lineage export")
    old_lineage_exports = [row for row in lineage_exports
                           if row.get("observation_id") == R39_INVALID_OBSERVATION_ID]
    head_lineage_exports = [row for row in lineage_exports
                            if row.get("observation_id") == R39_HEAD_OBSERVATION_ID]
    if old_lineage_exports:
        failures.append("lineage_edges.csv still exports blank-code lineage")
    if len(head_lineage_exports) != 1 \
            or head_lineage_exports[0].get("input_role") != "population" \
            or head_lineage_exports[0].get("input_population_id") != R39_HEAD_POPULATION_ID:
        failures.append("lineage_edges.csv omits the complete filing-level point population")

    return {
        "accepted": not failures,
        "checks": {
            "validation": validation_row,
            "filing_point_fact_count": len(source_facts),
            "filing_point_member_digest": source_member_digest,
            "blank_source_facts": blank_facts,
            "malformed_source_fact_ids": malformed_facts,
            "removed_invalid_state": {
                "observations": old_observations,
                "populations": old_populations,
                "edges": old_edges,
            },
            "promoted_blank_populations": promoted_blank_populations,
            "missing_structured_purpose_populations": missing_purpose_populations,
            "file_head_validation": file_head_validation,
            "duplicate_file_heads": duplicate_file_heads,
            "head_observation": heads,
            "head_population": head_populations,
            "head_edges": head_edges,
            "old_observation_exports": old_observation_exports,
            "head_observation_export": head_exports,
            "old_lineage_exports": old_lineage_exports,
            "head_lineage_export": head_lineage_exports,
        },
        "failures": failures,
    }


def _r40_zero_point_census_gate(
        root: pathlib.Path, con: sqlite3.Connection) -> Dict[str, Any]:
    """Bind the zero-P-row repair to the 11 exact Fayetteville occurrences.

    A known census of zero source P rows belongs in ``candidate_count=0`` even
    though the metric value remains ``source_blank``.  Zero here describes the
    filing's row population, never a claim that the physical pipeline has zero
    points.  The file-head observation, empty population, edge and consumer
    export must all preserve that distinction.
    """
    failures: List[str] = []
    validation_row = _ioc_validation_row(root, failures)
    _, observation_exports, _ = _csv_rows(
        root / "exports" / "canonical_observations.csv", "R40 observation export")
    _, lineage_exports, _ = _csv_rows(
        root / "exports" / "lineage_edges.csv", "R40 lineage export")
    expected_empty_digest = hashlib.sha256(b"").hexdigest()
    occurrence_checks = []

    for accession, (observation_id, population_id) in sorted(
            R40_ZERO_POINT_ROWS.items()):
        filings = [dict(row) for row in con.execute(
            "SELECT entity_key,form,accession_number,is_canonical,version_status "
            "FROM filings WHERE source_system='eLibrary' AND filing_id=?",
            (accession,))]
        facts = [dict(row) for row in con.execute(
            "SELECT source_fact_id,concept_local FROM source_facts "
            "WHERE source_system='eLibrary' AND filing_id=? ORDER BY source_fact_id",
            (accession,))]
        observations = [dict(row) for row in con.execute(
            "SELECT observation_id,entity_key,metric_id,source_system,filing_id,"
            "accession_number,scope,unit,value_text,value_num,availability,selector,"
            "candidate_count,missing_reason FROM observations WHERE observation_id=?",
            (observation_id,))]
        populations = [dict(row) for row in con.execute(
            "SELECT population_id,observation_id,source_system,source_table,filing_ids,"
            "row_count,candidate_count,excluded_count,member_key,member_digest,"
            "members_sample,aggregate_value,aggregate_unit,empty_reason,note "
            "FROM lineage_populations WHERE population_id=?", (population_id,))]
        edges = [dict(row) for row in con.execute(
            "SELECT input_order,input_role,input_source_system,input_filing_id,"
            "input_source_fact_id,input_value,input_unit,input_population_id "
            "FROM lineage_edges WHERE observation_id=? ORDER BY input_order",
            (observation_id,))]
        exported_observations = [row for row in observation_exports
                                 if row.get("observation_id") == observation_id]
        exported_edges = [row for row in lineage_exports
                          if row.get("observation_id") == observation_id]

        occurrence_failures = []
        if len(filings) != 1 or filings[0] != {
                "entity_key": R40_ENTITY_KEY, "form": "Form 549B IOC",
                "accession_number": accession, "is_canonical": 1,
                "version_status": "original"}:
            occurrence_failures.append("filing occurrence/owner/version is not exact")
        if facts != [{"source_fact_id": "H00001",
                      "concept_local": "ioc_header_record"}]:
            occurrence_failures.append("source occurrence is not exactly H=1/D=0/P=0")
        expected_observation = {
            "observation_id": observation_id, "entity_key": R40_ENTITY_KEY,
            "metric_id": "ioc_points", "source_system": "eLibrary",
            "filing_id": accession, "accession_number": accession,
            "scope": "location detail only", "unit": None, "value_text": None,
            "value_num": None, "availability": "source_blank",
            "selector": "ioc_p_records", "candidate_count": 0,
            "missing_reason": "this snapshot carries no P (point) records",
        }
        if len(observations) != 1 or observations[0] != expected_observation:
            occurrence_failures.append(
                "source-blank observation does not carry the exact zero-row census")
        expected_population = {
            "population_id": population_id, "observation_id": observation_id,
            "source_system": "eLibrary", "source_table": "source_facts",
            "filing_ids": json.dumps([accession]), "row_count": 0,
            "candidate_count": 0, "excluded_count": 0,
            "member_key": "filing_id:source_fact_id",
            "member_digest": expected_empty_digest, "members_sample": "[]",
            "aggregate_value": None, "aggregate_unit": None,
        }
        if len(populations) != 1:
            occurrence_failures.append("zero-row point population is absent or duplicated")
        else:
            population = populations[0]
            if {key: population.get(key) for key in expected_population} != \
                    expected_population:
                occurrence_failures.append("zero-row point population identity/counts differ")
            if _structured_ioc_population_purpose(population.get("note")) != \
                    "point_records":
                occurrence_failures.append("zero-row population lacks structured purpose")
            if not str(population.get("empty_reason") or "").strip():
                occurrence_failures.append("zero-row population lacks its source-empty reason")
        if len(edges) != 1 or edges[0] != {
                "input_order": 1, "input_role": "population",
                "input_source_system": "eLibrary", "input_filing_id": accession,
                "input_source_fact_id": None, "input_value": "0",
                "input_unit": None, "input_population_id": population_id}:
            occurrence_failures.append("zero-row population edge is absent or changed")
        if len(exported_observations) != 1:
            occurrence_failures.append("canonical observation export is absent or duplicated")
        else:
            exported = exported_observations[0]
            if exported.get("availability") != "source_blank" \
                    or exported.get("candidate_count") != "0" \
                    or exported.get("value") not in (None, "") \
                    or exported.get("unit") not in (None, "") \
                    or exported.get("missing_reason") != \
                    "this snapshot carries no P (point) records":
                occurrence_failures.append(
                    "canonical observation export loses the zero-row/source-blank state")
        if len(exported_edges) != 1 \
                or exported_edges[0].get("input_role") != "population" \
                or exported_edges[0].get("input_filing_id") != accession \
                or exported_edges[0].get("input_population_id") != population_id:
            occurrence_failures.append("lineage export loses the zero-row population edge")
        if occurrence_failures:
            failures.append(f"{accession}: " + "; ".join(occurrence_failures))
        occurrence_checks.append({
            "accession_number": accession,
            "expected_observation_id": observation_id,
            "expected_population_id": population_id,
            "source_fact_ids": [row["source_fact_id"] for row in facts],
            "observation": observations,
            "population": populations,
            "edges": edges,
            "observation_exports": exported_observations,
            "lineage_exports": exported_edges,
            "accepted": not occurrence_failures,
            "failures": occurrence_failures,
        })

    return {
        "accepted": not failures,
        "checks": {
            "validation": validation_row,
            "entity_key": R40_ENTITY_KEY,
            "expected_occurrences": len(R40_ZERO_POINT_ROWS),
            "occurrences": occurrence_checks,
            "semantic_boundary": (
                "candidate_count=0 is the known filing P-row census; availability "
                "remains source_blank and no physical zero-point claim is made"),
        },
        "failures": failures,
    }


def _r44_csv_control_portability_gate(
        root: pathlib.Path, con: sqlite3.Connection) -> Dict[str, Any]:
    """Bind the extracted-text repair to stored rows and consumer exports.

    Official source bytes remain immutable in the content-addressed cache.  The
    normalised text stored in SQLite and the CSV representation may not contain
    binary C0 codes that the declared CPython 3.9 ``csv`` reader rejects.
    """
    forbidden = set(range(0x00, 0x09)) | {0x0B, 0x0C} | set(range(0x0E, 0x20))
    failures: List[str] = []
    stored_bad: List[dict] = []
    table_counts: Dict[str, int] = {}
    for table, identity in (("observations", "observation_id"),
                            ("document_facts", "document_fact_id")):
        columns = [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')
                   if str(row[2] or "").upper() == "TEXT"]
        if not columns or identity not in columns:
            failures.append(f"{table} text schema is unavailable")
            continue
        rows = con.execute(
            f'SELECT {",".join(chr(34) + c + chr(34) for c in columns)} '
            f'FROM "{table}" ORDER BY "{identity}"').fetchall()
        table_counts[table] = len(rows)
        for row in rows:
            values = dict(zip(columns, row))
            for column, value in values.items():
                if not isinstance(value, str):
                    continue
                controls = sorted({ord(ch) for ch in value if ord(ch) in forbidden})
                if controls:
                    stored_bad.append({
                        "table": table, "identity": values.get(identity),
                        "column": column,
                        "controls": [f"U+{code:04X}" for code in controls],
                    })
                    if len(stored_bad) >= 20:
                        break
            if len(stored_bad) >= 20:
                break
    if stored_bad:
        failures.append("normalised SQLite text still contains binary C0 controls")

    export_checks = []
    for filename, table in (("canonical_observations.csv", "observations"),
                            ("document_facts.csv", "document_facts")):
        path = root / "exports" / filename
        raw = path.read_bytes() if path.is_file() else b""
        controls = sorted({value for value in raw if value in forbidden})
        if controls:
            failures.append(f"{filename} contains binary C0 controls")
        try:
            _, rows, _ = _csv_rows(path, f"R44 {filename}")
        except FinalRecordsRefused as exc:
            rows = []
            failures.append(str(exc))
        if len(rows) != table_counts.get(table, -1):
            failures.append(
                f"{filename} row count {len(rows)} differs from {table} "
                f"{table_counts.get(table, 'unavailable')}")
        export_checks.append({
            "path": f"exports/{filename}", "bytes": len(raw),
            "row_count": len(rows), "database_row_count": table_counts.get(table),
            "binary_c0_controls": [f"U+{value:04X}" for value in controls],
            "sha256": _sha(raw) if raw else None,
        })

    return {
        "accepted": not failures,
        "checks": {"database_row_counts": table_counts,
                   "stored_control_cells": stored_bad,
                   "exports": export_checks,
                   "raw_source_boundary": (
                       "official source bytes remain unchanged in source_cache; only the "
                       "normalised extracted-text and consumer layers are repaired")},
        "failures": failures,
    }


def _capacity_semantic_gate_status(
        root: pathlib.Path, con: sqlite3.Connection) -> Dict[str, Any]:
    """Keep known-source interpretation failures out of applicability unknown."""
    accessions = ("20250225-5101", "20250227-5034", "20260202-5047")
    marks = ",".join("?" for _ in accessions)
    rows = [dict(value) for value in con.execute(
        f"SELECT observation_id,accession_number,availability,validation,value_num,scope "
        f"FROM observations WHERE accession_number IN ({marks}) "
        "AND metric_id='cap_reported_capacity' "
        "AND source_regime='Form 549B Capacity' "
        "ORDER BY accession_number,observation_id", accessions)]
    gated_null = [row for row in rows
                  if row["validation"] == "blocked_ambiguity"
                  and row["value_num"] is None]
    arlington = [row for row in rows if row["accession_number"] == "20250225-5101"]
    arlington_facts = [dict(value) for value in con.execute(
        "SELECT document_fact_id,value_num,confidence,review_state FROM document_facts "
        "WHERE filing_id='20250225-5101' ORDER BY document_fact_id")]
    measured = [dict(value) for value in con.execute(
        "SELECT e.slot_id,o.accession_number,m.outcome,o.observation_id "
        "FROM coverage_measured m JOIN coverage_expected e ON e.slot_id=m.slot_id "
        "JOIN observations o ON o.observation_id=m.observation_id "
        "WHERE o.accession_number IN ('20250225-5101','20260202-5047') "
        "AND o.availability='interpretation_blocked' ORDER BY o.accession_number")]
    valid_control = int(con.execute(
        "SELECT COUNT(*) FROM observations WHERE source_regime="
        "'Form 549B Capacity' AND availability='present' "
        "AND validation='pass' AND value_num IS NOT NULL").fetchone()[0])

    _, observation_exports, _ = _csv_rows(
        root / "exports" / "canonical_observations.csv",
        "R44 capacity-semantic observation export")
    exported_by_id = {str(row.get("observation_id") or ""): row
                      for row in observation_exports}
    export_failures = []
    for row in rows:
        exported = exported_by_id.get(row["observation_id"])
        if not exported:
            export_failures.append(f"missing observation {row['observation_id']}")
        elif (exported.get("availability") != row["availability"]
              or exported.get("validation") != row["validation"]):
            export_failures.append(
                f"status mismatch for observation {row['observation_id']}")
    _, coverage_exports, _ = _csv_rows(
        root / "exports" / "coverage_by_slot.csv",
        "R44 capacity-semantic coverage export")
    coverage_by_slot = {str(row.get("slot_id") or ""): row for row in coverage_exports}
    for row in measured:
        exported = coverage_by_slot.get(row["slot_id"])
        if not exported or exported.get("outcome") != row["outcome"] \
                or exported.get("observation_id") != row["observation_id"]:
            export_failures.append(f"coverage export mismatch for slot {row['slot_id']}")

    failures: List[str] = []
    if len(rows) != 34:
        failures.append(f"affected capacity population is {len(rows)}, expected 34")
    if len(gated_null) != 22:
        failures.append(f"blocked/null affected population is {len(gated_null)}, expected 22")
    if any(row["availability"] != "interpretation_blocked" for row in gated_null):
        failures.append("a blocked/null capacity row is not interpretation_blocked")
    if any(row["availability"] == "unverified_availability" for row in rows):
        failures.append("an affected complete-source row remains availability unknown")
    if len(arlington) != 19 or any(
            row["availability"] != "interpretation_blocked"
            or row["validation"] != "blocked_ambiguity"
            or row["value_num"] is not None for row in arlington):
        failures.append("Arlington one-head-plus-18-detail population is not fully gated")
    if len(arlington_facts) != 18 or any(
            row["value_num"] is not None for row in arlington_facts):
        failures.append("Arlington document-fact population was lost or a value was exposed")
    measured_pairs = {(row["accession_number"], row["outcome"]) for row in measured}
    if measured_pairs != {
            ("20250225-5101", "interpretation_blocked"),
            ("20260202-5047", "interpretation_blocked")}:
        failures.append("the two matched capacity slots have the wrong semantic outcome")
    if valid_control <= 0:
        failures.append("no clean numeric present/pass capacity control remains")
    failures.extend(export_failures)
    return {
        "accepted": not failures,
        "checks": {
            "affected_accessions": list(accessions),
            "affected_observations": len(rows),
            "blocked_null_observations": len(gated_null),
            "arlington_observations": len(arlington),
            "arlington_document_facts": len(arlington_facts),
            "matched_coverage": measured,
            "clean_numeric_present_pass_controls": valid_control,
            "export_failures": export_failures,
        },
        "failures": failures,
    }


def _w5_horizon_attached_unit_counterevidence_gate(
        root: pathlib.Path, con: sqlite3.Connection) -> Dict[str, Any]:
    """Distinguish two exact supported facts from a stale-fact fossil.

    This check deliberately does not call the production span matcher.  It
    independently binds the two occurrence-specific rows, their source
    document identities and their consumer exports to the exact filed token
    that exposed the earlier validator false negative.
    """
    _, exported_rows, _ = _csv_rows(
        root / "exports" / "document_facts.csv", "W5 document-fact export")
    exported_by_id: Dict[str, List[dict]] = {}
    for exported in exported_rows:
        exported_by_id.setdefault(str(exported.get("document_fact_id") or ""), []).append(
            exported)
    token = re.compile(r"(?<![\w,.])380,000MMBTU/d(?![\w])", re.IGNORECASE)
    failures: List[str] = []
    checks = []
    for fact_id, accession in sorted(W5_HORIZON_ATTACHED_UNIT_FACTS.items()):
        rows = [dict(row) for row in con.execute(
            "SELECT f.document_fact_id,f.document_id,f.source_system,f.filing_id,"
            "f.entity_key,f.assertion_type,f.value_text,f.unit,f.verbatim_span,"
            "f.content_hash,d.accession_number,d.content_hash AS document_content_hash "
            "FROM document_facts f JOIN documents d ON d.document_id=f.document_id "
            "WHERE f.document_fact_id=?", (fact_id,))]
        row_failures: List[str] = []
        if len(rows) != 1:
            row_failures.append("fact is absent or duplicated")
            row = {}
        else:
            row = rows[0]
            expected = {
                "document_fact_id": fact_id,
                "source_system": "eLibrary",
                "filing_id": accession,
                "entity_key": "C000226",
                "value_text": "380,000",
                "unit": "MMBTU/day",
                "accession_number": accession,
            }
            if {key: row.get(key) for key in expected} != expected:
                row_failures.append("occurrence/entity/value/unit identity differs")
            if not token.search(str(row.get("verbatim_span") or "")):
                row_failures.append("operative span lacks the exact attached-unit token")
            content_hash = str(row.get("content_hash") or "")
            if not HEX64.fullmatch(content_hash) \
                    or content_hash != row.get("document_content_hash"):
                row_failures.append("fact/document source-content identity is unresolved")
        exports = exported_by_id.get(fact_id, [])
        if len(exports) != 1:
            row_failures.append("consumer export is absent or duplicated")
        elif row:
            exported = exports[0]
            for key in ("document_fact_id", "document_id", "source_system", "filing_id",
                        "entity_key", "assertion_type", "value_text", "unit",
                        "verbatim_span", "content_hash"):
                if str(exported.get(key) or "") != str(row.get(key) or ""):
                    row_failures.append(f"consumer export differs on {key}")
                    break
        if row_failures:
            failures.append(f"{fact_id}/{accession}: " + "; ".join(row_failures))
        checks.append({
            "document_fact_id": fact_id,
            "accession_number": accession,
            "database_rows": rows,
            "export_rows": exports,
            "accepted": not row_failures,
            "failures": row_failures,
        })
    if len({row.get("filing_id") for check in checks
            for row in check["database_rows"]}) != 2:
        failures.append("the counterevidence does not retain two distinct occurrences")
    return {
        "accepted": not failures,
        "checks": {
            "population": len(W5_HORIZON_ATTACHED_UNIT_FACTS),
            "facts": checks,
            "independent_token_boundary": token.pattern,
            "claim_boundary": (
                "these two rows rebut the v9 fossil classification; the separate "
                "regeneration mutation test remains required to prove stale-fact retirement"),
        },
        "failures": failures,
    }


def _related_repair_records(
        root: pathlib.Path, con: sqlite3.Connection, draft: Mapping[str, Any],
        tests: Mapping[str, Any],
        publication: Mapping[str, Any], additional_inputs: Sequence[dict],
        snapshot_evidence: Optional[Mapping[str, Any]] = None) -> List[dict]:
    """Finalize repair-time defects against observed tests and Build-A state."""
    additional_by_id = {row["input_id"]: row for row in additional_inputs}
    association_checks, association_failures = _filing_association_integrity(root, con)
    final: List[dict] = []
    for row in sorted(draft["rows"], key=lambda value: value["issue_id"]):
        issue_id = row["issue_id"]
        target_tests = list(tests["issue_test_evidence"].get(issue_id) or [])
        if set(target_tests) != set(row["target_test_ids"]):
            raise FinalRecordsRefused(
                f"related finding {issue_id} final test map differs from its targeted tests")
        build_a_gate: Dict[str, Any] = {"accepted": True}
        if issue_id == "R23-CAPACITY-FILER-IDENTITY-COLLISION":
            accession = "20260226-5162"
            expected_entity = "C001088"
            conflicting_entity = "C001087"
            expected_hash = \
                "983e6a1c12a2f15934d99a2149c7a18e3dbbedf145f0848b9844dc4467dac5eb"
            expected_bytes = 173287
            input_record = additional_by_id.get("elibrary:" + accession)
            if not input_record or input_record.get("acceptance_result") != \
                    "accepted_for_candidate":
                raise FinalRecordsRefused(
                    f"related finding {issue_id} lacks its applied additional input")
            filing_owners = [str(value[0]) for value in con.execute(
                "SELECT DISTINCT entity_key FROM filings WHERE accession_number=? "
                "ORDER BY entity_key", (accession,))]
            observation_owners = [str(value[0]) for value in con.execute(
                "SELECT DISTINCT entity_key FROM observations WHERE accession_number=? "
                "ORDER BY entity_key", (accession,))]
            population_rows = [dict(value) for value in con.execute(
                "SELECT p.population_id,p.observation_id,o.entity_key,p.filing_ids "
                "FROM lineage_populations p JOIN observations o "
                "ON o.observation_id=p.observation_id "
                "WHERE o.accession_number=? ORDER BY p.population_id", (accession,))]
            filing_rows = [dict(value) for value in con.execute(
                "SELECT source_system,filing_id,entity_key,form,content_hash,is_canonical,"
                "version_status FROM filings WHERE accession_number=?", (accession,))]
            document_rows = [dict(value) for value in con.execute(
                "SELECT source_system,filing_id,accession_number,byte_size,content_hash,"
                "availability FROM documents WHERE accession_number=?", (accession,))]
            population_bad = []
            for population in population_rows:
                try:
                    filing_ids = json.loads(str(population.get("filing_ids") or "[]"))
                except json.JSONDecodeError:
                    filing_ids = []
                if population.get("entity_key") != expected_entity \
                        or accession not in filing_ids:
                    population_bad.append(population["population_id"])
            checks = {
                "accession_number": accession,
                "expected_entity": expected_entity,
                "conflicting_entity": conflicting_entity,
                "filing_owners": filing_owners,
                "filings": filing_rows,
                "observation_owners": observation_owners,
                "observations": int(con.execute(
                    "SELECT COUNT(*) FROM observations WHERE accession_number=?",
                    (accession,)).fetchone()[0]),
                "source_facts": int(con.execute(
                    "SELECT COUNT(*) FROM source_facts WHERE source_system='eLibrary' "
                    "AND filing_id=?", (accession,)).fetchone()[0]),
                "documents": document_rows,
                "lineage_populations": len(population_rows),
                "invalid_population_ids": population_bad,
                "additional_input_acceptance": input_record["acceptance_result"],
            }
            failures = []
            if filing_owners != [expected_entity]:
                failures.append("filing owner is not exactly C001088")
            if len(filing_rows) != 1 or filing_rows[0] != {
                    "source_system": "eLibrary", "filing_id": accession,
                    "entity_key": expected_entity, "form": "Form 549B Capacity",
                    "content_hash": expected_hash, "is_canonical": 1,
                    "version_status": "original"}:
                failures.append("filing occurrence identity/version/hash is not exact")
            if observation_owners != [expected_entity] or checks["observations"] <= 0:
                failures.append("observation owner/population is not exactly C001088")
            if checks["source_facts"] <= 0 or len(document_rows) != 1:
                failures.append("source facts or document occurrence is absent")
            elif document_rows[0] != {
                    "source_system": "eLibrary", "filing_id": accession,
                    "accession_number": accession, "byte_size": expected_bytes,
                    "content_hash": expected_hash, "availability": "retrieved"}:
                failures.append("document occurrence bytes/hash/owner is not exact")
            if not population_rows or population_bad:
                failures.append("occurrence-specific capacity population is absent or misowned")
            build_a_gate = {"accepted": not failures, "checks": checks,
                            "failures": failures}
            if failures:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: {failures}")
        elif issue_id == "R25-ELIBRARY-AS-OF-FUTURE-OCCURRENCE-LEAK":
            prior = "20260825-5125"
            future = "20260909-5053"
            owners = [str(value[0]) for value in con.execute(
                "SELECT DISTINCT entity_key FROM filings WHERE accession_number=?",
                (prior,))]
            dockets = [str(value[0]) for value in con.execute(
                "SELECT docket FROM filing_dockets WHERE filing_id=? ORDER BY docket",
                (prior,))]
            future_population = _future_occurrence_population(con, future)
            warning_rows = int(con.execute(
                "SELECT COUNT(*) FROM run_log WHERE level='warn' "
                "AND message LIKE '%excluded%' AND message LIKE ? AND message LIKE ?",
                (f"%{future}%", "%2026-09-07%")).fetchone()[0])
            search_id = (
                "elibrary-search:RP26-1091:"
                "2280cc641139baafaf01d5f47c1cf7b404c6e2938c8dbe7fd84f98eda7205005")
            input_record = additional_by_id.get(search_id)
            input_application = ((input_record or {}).get("build_a_application") or {})
            future_checks = [item for item in input_application.get("occurrences", [])
                             if isinstance(item, dict)
                             and item.get("accession_number") == future]
            diagnostic_boundary = input_application.get("run_plan_boundary") or {}
            failures = []
            if owners != ["C000654"] or "RP26-1091-000" not in dockets:
                failures.append("valid pre-as-of Transco occurrence was not retained exactly")
            if any(future_population.values()):
                failures.append("post-as-of Transco occurrence leaked into stored state")
            if not input_record or input_record.get("acceptance_result") != \
                    "accepted_as_repair_evidence_not_execution_prerequisite":
                failures.append("exact official search response is not hash-bound evidence")
            if len(future_checks) != 1 or future_checks[0].get("accepted") is not True:
                failures.append("post-as-of diagnostic occurrence boundary is not accepted")
            elif warning_rows <= 0 and not (
                    future_checks[0].get("exclusion_evidence") ==
                    "diagnostic_response_not_selected_by_run_plan"
                    and diagnostic_boundary.get("accepted") is True):
                failures.append(
                    "post-as-of occurrence has neither an append-only runtime warning "
                    "nor a proven non-selected diagnostic boundary")
            build_a_gate = {
                "accepted": not failures,
                "checks": {"as_of": "2026-09-07", "retained_accession": prior,
                           "retained_owners": owners, "retained_dockets": dockets,
                           "excluded_accession": future,
                           "excluded_persisted_population": future_population,
                           "exclusion_warning_rows": warning_rows,
                           "exclusion_evidence": (future_checks[0]
                                                  if len(future_checks) == 1 else None),
                           "run_plan_boundary": diagnostic_boundary,
                           "search_input_acceptance": (
                               input_record.get("acceptance_result")
                               if input_record else None)},
                "failures": failures,
            }
            if failures:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: {failures}")
        elif issue_id == "R26-ELIBRARY-HIDDEN-PROCEEDING-SEED":
            seed_rel = pathlib.Path(
                "inputs/official_ferc/elibrary/rate_proceedings_v1.csv")
            seed_expected = {
                "bytes": 25939,
                "sha256": "b4e767d3906ed096a5404aeff8db6c1f3504994c2b71db8fac5e36d1baff6032",
            }
            seed_identity = _expected_identity(
                root / seed_rel, seed_expected, "versioned eLibrary proceeding seed")
            seed_identity["path"] = seed_rel.as_posix()
            _, plan = _load_json(root / "config" / "run_plan.json", "run plan")
            declared = [item for item in (plan.get("required_inputs") or [])
                        if isinstance(item, dict) and item.get("path") == seed_rel.as_posix()]
            exact_plan_input = (len(declared) == 1
                                and declared[0].get("bytes") in (
                                    None, seed_expected["bytes"])
                                and declared[0].get("sha256") == seed_expected["sha256"])
            log_expectations = {
                "C000654": ["RP25-1189", "RP24-1035", "RP18-1126"],
                "C001049": ["IS26-546", "IS24-804", "IS24-810"],
            }
            log_rows = {}
            for entity, dockets_expected in log_expectations.items():
                hits = [str(value[0]) for value in con.execute(
                    "SELECT message FROM run_log WHERE adapter='elibrary_docs' "
                    "AND entity_cid=? AND message LIKE '%proceedings reported%' "
                    "ORDER BY run_id,seq", (entity,))]
                log_rows[entity] = hits
                if not any(all(docket in message for docket in dockets_expected)
                           for message in hits):
                    exact_plan_input = False
            failures = [] if exact_plan_input else [
                "versioned seed is not hash-declared or curated proceedings were not used"]
            build_a_gate = {
                "accepted": not failures,
                "checks": {"seed": seed_identity, "run_plan_entry": declared,
                           "curated_proceeding_logs": log_rows},
                "failures": failures,
            }
            if failures:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: {failures}")
        elif issue_id == "R27-ELIBRARY-RUN-INPUT-IDENTITY":
            inventory_row = con.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN source_system='eLibrary' THEN 0 ELSE 1 END) AS bad_source,"
                "SUM(CASE WHEN COALESCE(filing_id,'')='' THEN 1 ELSE 0 END) AS blank_filing,"
                "SUM(CASE WHEN COALESCE(accession_number,'')='' THEN 1 ELSE 0 END) AS blank_accession "
                "FROM run_input_inventory WHERE adapter='elibrary_docs'").fetchone()
            counts = {"total": int(inventory_row[0] or 0),
                      "bad_source": int(inventory_row[1] or 0),
                      "blank_filing": int(inventory_row[2] or 0),
                      "blank_accession": int(inventory_row[3] or 0)}
            unjoined = int(con.execute(
                "SELECT COUNT(*) FROM run_input_inventory i LEFT JOIN filings f "
                "ON f.source_system=i.source_system AND f.filing_id=i.filing_id "
                "WHERE i.adapter='elibrary_docs' AND f.filing_id IS NULL").fetchone()[0])
            failures = []
            if counts["total"] <= 0:
                failures.append("no eLibrary per-run input inventory was produced")
            if any(counts[key] for key in ("bad_source", "blank_filing", "blank_accession")):
                failures.append("eLibrary per-run input identity remains blank or mislabelled")
            if unjoined:
                failures.append("eLibrary per-run input rows do not resolve to filings")
            build_a_gate = {
                "accepted": not failures,
                "checks": {"run_input_inventory": counts,
                           "rows_without_filing_occurrence": unjoined},
                "failures": failures,
            }
            if failures:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: {failures}")
        elif issue_id == "R28-LNG-ROUTING-AUTHORITY-ASSOCIATIONS":
            build_a_gate = _lng_routing_build_a_gate(
                root, con, association_checks, association_failures)
            if build_a_gate["failures"]:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: "
                    f"{build_a_gate['failures']}")
        elif issue_id == "R29-ELIBRARY-SIBLING-FILER-CONTAMINATION":
            build_a_gate = _r29_sibling_contamination_gate(
                root, con, association_checks, association_failures)
            if build_a_gate["failures"]:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: "
                    f"{build_a_gate['failures']}")
        elif issue_id == "R30-RETRIEVAL-UNIT-PARTIAL-ROW-LEAKAGE":
            unfinished_runs = [dict(value) for value in con.execute(
                "SELECT run_id,status,finished_at FROM runs "
                "WHERE status!='complete' OR finished_at IS NULL ORDER BY run_id")]
            in_progress_checkpoints = [dict(value) for value in con.execute(
                "SELECT adapter,entity_cid,scope_key,last_run_id FROM checkpoints "
                "WHERE state='in_progress' ORDER BY adapter,entity_cid,scope_key")]
            latest_in_progress_status = [dict(value) for value in con.execute(
                "WITH latest AS ("
                " SELECT run_id,adapter,entity_cid,scope_key,MAX(seq) AS max_seq"
                " FROM run_unit_status GROUP BY run_id,adapter,entity_cid,scope_key)"
                " SELECT s.run_id,s.adapter,s.entity_cid,s.scope_key,s.seq,s.detail"
                " FROM run_unit_status s JOIN latest l"
                " ON l.run_id=s.run_id AND l.adapter=s.adapter"
                " AND l.entity_cid=s.entity_cid AND l.scope_key=s.scope_key"
                " AND l.max_seq=s.seq WHERE s.state='in_progress'"
                " ORDER BY s.run_id,s.adapter,s.entity_cid,s.scope_key")]
            orphan_inventory = int(con.execute(
                "SELECT COUNT(*) FROM run_input_inventory i LEFT JOIN runs r"
                " ON r.run_id=i.run_id WHERE r.run_id IS NULL").fetchone()[0])
            failures = []
            if unfinished_runs:
                failures.append("unfinished run rows remain at the Build-A boundary")
            if in_progress_checkpoints:
                failures.append("in-progress checkpoint rows remain at the Build-A boundary")
            if latest_in_progress_status:
                failures.append("latest per-run unit status remains in-progress")
            if orphan_inventory:
                failures.append("per-run input inventory does not resolve to a run")
            build_a_gate = {
                "accepted": not failures,
                "checks": {
                    "unfinished_runs": unfinished_runs,
                    "in_progress_checkpoints": in_progress_checkpoints,
                    "latest_in_progress_unit_status": latest_in_progress_status,
                    "orphan_input_inventory_rows": orphan_inventory,
                    "unit_commit_rows": int(con.execute(
                        "SELECT COUNT(*) FROM unit_commits").fetchone()[0]),
                    "boundary": (
                        "raw evidence, complete input inventory, expected rows, canonical "
                        "swap and unit marker share the tested unit transaction; failed "
                        "attempt inventories intentionally roll back"),
                },
                "failures": failures,
            }
            if failures:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: {failures}")
        elif issue_id == "R31-REDUNDANT-INPUT-SNAPSHOT-PACKAGING-MISMATCH":
            if snapshot_evidence is None:
                alias_verification = _verified_redundant_input_alias(root)
                candidate_snapshots = _candidate_snapshots(
                    root, alias_verification=alias_verification)
            else:
                alias_verification = snapshot_evidence.get("redundant_input_alias")
                candidate_snapshots = snapshot_evidence.get("snapshots")
                if not isinstance(alias_verification, Mapping) \
                        or not isinstance(candidate_snapshots, Mapping):
                    raise FinalRecordsRefused(
                        f"related finding {issue_id} lacks canonical snapshot evidence")
            candidate_input_snapshot = candidate_snapshots.get("input_snapshot")
            publication_metadata = publication.get("metadata")
            published_input_snapshot = (
                publication_metadata.get("input_snapshot")
                if isinstance(publication_metadata, Mapping) else None)
            failures = []
            if not isinstance(candidate_input_snapshot, str) \
                    or not HEX64.fullmatch(candidate_input_snapshot):
                failures.append("canonical candidate input snapshot is invalid")
            if published_input_snapshot is not None \
                    and published_input_snapshot != candidate_input_snapshot:
                failures.append(
                    "published generation input snapshot differs from canonical candidate")
            checks = {
                "candidate_input_snapshot": candidate_input_snapshot,
                "publication_input_snapshot": published_input_snapshot,
                "alias_path": REDUNDANT_INPUT_ALIAS["alias_path"],
                "alias_present": bool(alias_verification.get("alias_present")),
                "alias_identity": alias_verification.get("alias_identity"),
                "canonical_identity": alias_verification.get("canonical_identity"),
                "cache_index_key": alias_verification.get("cache_index_key"),
                "cache_index_binding": alias_verification.get("cache_index_binding"),
                "results_identity": alias_verification.get("results_identity"),
                "ledger_identity": alias_verification.get("ledger_identity"),
                "canonicalisation": (
                    "the exact verified capture alias alone is omitted from input identity; "
                    "the canonical cache object and both retained provenance records remain "
                    "mandatory"),
            }
            build_a_gate = {
                "accepted": not failures,
                "checks": checks,
                "failures": failures,
            }
            if failures:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: {failures}")
        elif issue_id == "R33-UNDECLARED-ELIBRARY-REPLAY-DEPENDENCIES":
            input_id = "elibrary-dependency-capture:build-a-v4:20260910"
            input_record = additional_by_id.get(input_id)
            failures = []
            if not input_record or input_record.get("acceptance_result") != \
                    "accepted_for_candidate":
                failures.append("mandatory eLibrary dependency bundle is not accepted")
            application = (input_record or {}).get("build_a_application") or {}
            capture = (input_record or {}).get("capture") or {}
            units = application.get("units") if isinstance(application, Mapping) else None
            if capture.get("new_entries") != 11 \
                    or len(capture.get("required_failed_build_keys") or []) != 6:
                failures.append("dependency capture does not retain its 11/6 delta boundary")
            if not isinstance(units, list) or len(units) != 4 \
                    or not all(isinstance(unit, dict) and unit.get("accepted") is True
                               for unit in units):
                failures.append("the four affected eLibrary units did not all finish Build A")
            build_a_gate = {
                "accepted": not failures,
                "checks": {"additional_input_id": input_id,
                           "acceptance_result": (input_record or {}).get("acceptance_result"),
                           "capture": capture, "application_units": units or []},
                "failures": failures,
            }
            if failures:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: {failures}")
        elif issue_id == "R36-NAMED-LNG-FILER-COVERAGE-GATE":
            try:
                build_a_gate = _r36_named_lng_filer_gate(
                    root, con, association_checks, association_failures)
            except FinalRecordsRefused as exc:
                build_a_gate = {"accepted": False, "checks": {},
                                "failures": [str(exc)]}
            if build_a_gate["failures"]:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: "
                    f"{build_a_gate['failures']}")
        elif issue_id == "R37-STALE-TAXONOMY-CACHE-FREEZE":
            try:
                build_a_gate = _r37_taxonomy_cache_gate(root, con, publication)
            except FinalRecordsRefused as exc:
                build_a_gate = {"accepted": False, "checks": {},
                                "failures": [str(exc)]}
            if build_a_gate["failures"]:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: "
                    f"{build_a_gate['failures']}")
        elif issue_id == "R38-IOC-LINEAGE-VALIDATOR-SCOPE-AND-EXTENDED-POINT-CODE":
            try:
                build_a_gate = _r38_ioc_lineage_gate(root, con)
            except FinalRecordsRefused as exc:
                build_a_gate = {"accepted": False, "checks": {},
                                "failures": [str(exc)]}
            if build_a_gate["failures"]:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: "
                    f"{build_a_gate['failures']}")
        elif issue_id == "R39-BLANK-IOC-POINT-CODE-PROMOTION":
            try:
                build_a_gate = _r39_blank_ioc_point_code_gate(root, con)
            except FinalRecordsRefused as exc:
                build_a_gate = {"accepted": False, "checks": {},
                                "failures": [str(exc)]}
            if build_a_gate["failures"]:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: "
                    f"{build_a_gate['failures']}")
        elif issue_id == "R40-IOC-ZERO-POINT-CENSUS-CANDIDATE-COUNT":
            try:
                build_a_gate = _r40_zero_point_census_gate(root, con)
            except FinalRecordsRefused as exc:
                build_a_gate = {"accepted": False, "checks": {},
                                "failures": [str(exc)]}
            if build_a_gate["failures"]:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: "
                    f"{build_a_gate['failures']}")
        elif issue_id == "R41-IOC-INTEGRATION-FIXTURE-FK-LIFECYCLE":
            build_a_gate = {
                "accepted": True,
                "boundary": "test_wiring_and_disposable_lifecycle_not_database_state",
                "checks": {
                    "production_foreign_keys_retained": True,
                    "real_byte_fixture_explicitly_offline": True,
                    "setup_failure_cleanup_required": True,
                },
                "failures": [],
            }
        elif issue_id == "R42-A13-LAST-GOOD-ATTEMPT-RECEIPT-ASSERTION":
            build_a_gate = {
                "accepted": True,
                "boundary": "acceptance_assertion_not_publication_data_state",
                "checks": {
                    "last_good_receipt_semantics_preserved": True,
                    "failed_attempt_has_distinct_sidecar": True,
                },
                "failures": [],
            }
        elif issue_id == "R43-FINAL-LEDGER-SUBSET-HIDDEN-W5-DEPENDENCY":
            build_a_gate = {
                "accepted": True,
                "boundary": "test_wiring_and_scoped_finalizer_dependency",
                "checks": {
                    "current_production_signature_exercised": True,
                    "unrelated_subset_uses_isolated_in_memory_database": True,
                    "w5_gate_runs_only_when_w5_is_selected": True,
                },
                "failures": [],
            }
        elif issue_id == "R44-CPYTHON39-CSV-C0-CONTROL-PORTABILITY":
            portable_text = _r44_csv_control_portability_gate(root, con)
            semantic_status = _capacity_semantic_gate_status(root, con)
            failures = [f"portable_text: {failure}"
                        for failure in portable_text["failures"]]
            failures.extend(f"capacity_semantic_status: {failure}"
                            for failure in semantic_status["failures"])
            build_a_gate = {
                "accepted": not failures,
                "checks": {
                    "portable_text": portable_text["checks"],
                    "capacity_semantic_status": semantic_status["checks"],
                },
                "failures": failures,
            }
            if build_a_gate["failures"]:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: "
                    f"{build_a_gate['failures']}")
        elif issue_id == "R53-SHARED-OCCURRENCE-LISTING-TIMESTAMP-CONVERGENCE":
            inconsistent = [dict(value) for value in con.execute(
                "SELECT d.document_id,d.retrieved_at AS document_retrieved_at,"
                "f.retrieved_at AS filing_retrieved_at "
                "FROM documents d JOIN filings f ON "
                "f.source_system=d.source_system AND f.filing_id=d.filing_id "
                "WHERE d.source_system='eLibrary' "
                "AND d.document_id=(d.source_system||'|'||d.filing_id||'|listing') "
                "AND COALESCE(d.attachment_id,'')='' "
                "AND COALESCE(d.content_hash,'')='' "
                "AND f.retrieved_at IS NOT NULL "
                "AND COALESCE(d.retrieved_at,'')<>f.retrieved_at "
                "ORDER BY d.document_id")]
            affected_id = "eLibrary|20241121-3047|listing"
            affected = [dict(value) for value in con.execute(
                "SELECT d.document_id,d.retrieved_at AS document_retrieved_at,"
                "d.availability,f.retrieved_at AS filing_retrieved_at "
                "FROM documents d JOIN filings f ON "
                "f.source_system=d.source_system AND f.filing_id=d.filing_id "
                "WHERE d.document_id=?", (affected_id,))]
            attachment_times = [str(value[0]) for value in con.execute(
                "SELECT retrieved_at FROM documents WHERE source_system='eLibrary' "
                "AND filing_id='20241121-3047' AND COALESCE(attachment_id,'')<>'' "
                "AND availability='retrieved' AND retrieved_at IS NOT NULL "
                "ORDER BY document_id")]
            failures = []
            if inconsistent:
                failures.append("listing placeholders do not match their occurrence capture")
            if len(affected) != 1 \
                    or affected[0]["availability"] != "not_retrieved" \
                    or not affected[0]["document_retrieved_at"] \
                    or affected[0]["document_retrieved_at"] != \
                    affected[0]["filing_retrieved_at"]:
                failures.append("the exact v21 shared listing occurrence is not repaired")
            if affected and affected[0]["document_retrieved_at"] not in attachment_times:
                failures.append("the shared listing does not agree with its retrieved attachment")
            build_a_gate = {
                "accepted": not failures,
                "checks": {
                    "inconsistent_listing_placeholders": inconsistent,
                    "affected_listing": affected,
                    "retrieved_attachment_times": attachment_times,
                },
                "failures": failures,
            }
            if failures:
                raise FinalRecordsRefused(
                    f"related finding {issue_id} failed Build A: {failures}")
        disposition = {
            "R41-IOC-INTEGRATION-FIXTURE-FK-LIFECYCLE":
                "test_fixture_repaired_and_observed_passing",
            "R42-A13-LAST-GOOD-ATTEMPT-RECEIPT-ASSERTION":
                "acceptance_assertion_repaired_and_observed_passing",
            "R43-FINAL-LEDGER-SUBSET-HIDDEN-W5-DEPENDENCY":
                "finalizer_test_wiring_and_dependency_scope_repaired",
            "R44-CPYTHON39-CSV-C0-CONTROL-PORTABILITY":
                "stored_text_normalised_and_capacity_semantic_gates_verified",
            "R45-BUILD-B-PENDING-INSTRUCTION-CORRUPTS-EXTRACTION":
                "deferred_build_b_mandatory_and_immutable_extraction_documented",
            "R46-CANDIDATE-MATERIALIZER-RETAINS-HIDDEN-SCRATCH":
                "candidate_materializer_excludes_scratch_and_refuses_reuse",
            "R47-FORM549D-HIDDEN-CANDIDATE-WRITE":
                "form549d_resolution_routed_to_declared_output_root",
            "R48-BUILD-B-REPLAY-LEDGER-OBJECT-ORDER":
                "replay_order_bound_to_explicit_step_ordinals",
            "R49-BUILD-B-ACCEPTANCE-MIRROR-MISSING-DEPENDENCY":
                "acceptance_mirror_includes_declared_regression_dependency",
            "R50-ELIBRARY-CAPTURE-TIME-DETERMINISM":
                "cache_bound_source_times_applied_and_exact",
            "R51-RUN-ENVELOPE-PUBLICATION-DETERMINISM":
                "lifecycle_times_narrowly_normalised_and_exports_stably_ordered",
            "R52-BUILD-B-REFUSAL-COMPARISON-EVIDENCE":
                "comparison_evidence_recorded_before_fail_closed_gate",
            "R53-SHARED-OCCURRENCE-LISTING-TIMESTAMP-CONVERGENCE":
                "shared_listing_capture_converges_without_enriching_attachments",
            "R54-FULL-RELEASE-INSTRUCTION-DEFER-GATE":
                "full_release_instructions_match_the_deferred_build_b_gate",
            "R55-PLACEHOLDER-MARKER-DOMAIN-TERM-COLLISION":
                "draft_marker_gate_distinguishes_domain_specific_language",
        }.get(issue_id, "fixed_and_verified_in_build_a_candidate")
        final.append({
            "issue_id": issue_id,
            "source_row_type": "related_repair_finding",
            "original_issue_ids": row.get("original_issue_ids") or [],
            "severity": row.get("severity"),
            "audit_claim": {
                "status": "not_in_final_independent_audit",
                "relationship": row["relationship_to_final_audit"],
                "repair_discovery": row.get("repair_discovery"),
            },
            "affected_component_or_data": row.get("affected_component_or_data") or [],
            "root_cause": row["root_cause"],
            "targeted_test": row["targeted_test"],
            "targeted_test_status": "observed_passing_in_hash_bound_candidate_log",
            "disposition": disposition,
            "acceptance_result": "accepted_at_build_a_boundary",
            "closure_status": "accepted_candidate_not_release_closed",
            "gates": {
                "code": "bound_to_publication_code_snapshot",
                "data": ("verified_at_read_only_build_a_input_boundary"
                         if issue_id ==
                         "R31-REDUNDANT-INPUT-SNAPSHOT-PACKAGING-MISMATCH"
                         else "verified_in_read_only_build_a_database"
                         if issue_id in {
                             "R23-CAPACITY-FILER-IDENTITY-COLLISION",
                             "R25-ELIBRARY-AS-OF-FUTURE-OCCURRENCE-LEAK",
                             "R26-ELIBRARY-HIDDEN-PROCEEDING-SEED",
                             "R27-ELIBRARY-RUN-INPUT-IDENTITY",
                             "R28-LNG-ROUTING-AUTHORITY-ASSOCIATIONS",
                             "R29-ELIBRARY-SIBLING-FILER-CONTAMINATION",
                             "R30-RETRIEVAL-UNIT-PARTIAL-ROW-LEAKAGE",
                             "R33-UNDECLARED-ELIBRARY-REPLAY-DEPENDENCIES",
                             "R36-NAMED-LNG-FILER-COVERAGE-GATE",
                             "R37-STALE-TAXONOMY-CACHE-FREEZE",
                             "R38-IOC-LINEAGE-VALIDATOR-SCOPE-AND-EXTENDED-POINT-CODE",
                             "R39-BLANK-IOC-POINT-CODE-PROMOTION",
                             "R40-IOC-ZERO-POINT-CENSUS-CANDIDATE-COUNT",
                             "R44-CPYTHON39-CSV-C0-CONTROL-PORTABILITY",
                             "R50-ELIBRARY-CAPTURE-TIME-DETERMINISM",
                             "R51-RUN-ENVELOPE-PUBLICATION-DETERMINISM",
                             "R53-SHARED-OCCURRENCE-LISTING-TIMESTAMP-CONVERGENCE",
                         } else "not_applicable"),
                "exports": ("matched_to_published_generation_and_database"
                            if issue_id in {
                                "R23-CAPACITY-FILER-IDENTITY-COLLISION",
                                "R25-ELIBRARY-AS-OF-FUTURE-OCCURRENCE-LEAK",
                                "R26-ELIBRARY-HIDDEN-PROCEEDING-SEED",
                                "R28-LNG-ROUTING-AUTHORITY-ASSOCIATIONS",
                                "R29-ELIBRARY-SIBLING-FILER-CONTAMINATION",
                                "R30-RETRIEVAL-UNIT-PARTIAL-ROW-LEAKAGE",
                                "R33-UNDECLARED-ELIBRARY-REPLAY-DEPENDENCIES",
                                "R36-NAMED-LNG-FILER-COVERAGE-GATE",
                                "R37-STALE-TAXONOMY-CACHE-FREEZE",
                                "R38-IOC-LINEAGE-VALIDATOR-SCOPE-AND-EXTENDED-POINT-CODE",
                                "R39-BLANK-IOC-POINT-CODE-PROMOTION",
                                "R40-IOC-ZERO-POINT-CENSUS-CANDIDATE-COUNT",
                                "R44-CPYTHON39-CSV-C0-CONTROL-PORTABILITY",
                                "R50-ELIBRARY-CAPTURE-TIME-DETERMINISM",
                                "R51-RUN-ENVELOPE-PUBLICATION-DETERMINISM",
                            } else "not_applicable"),
                "tests": "mapped_to_observed_successful_verbose_test_ids",
                "shipping": "outside_build_a_record_boundary",
            },
            "evidence": {
                "candidate_paths": row.get("evidence") or [],
                "publication_generation_id": publication["generation_id"],
                "executed_test_ids": target_tests,
                "build_a_gate": build_a_gate,
            },
            "residual_limitation": row.get("residual_limitation"),
        })
    return final


def _exception_records(con: sqlite3.Connection, draft: Mapping[str, Any],
                       a17: Mapping[str, Any],
                       audit_by_id: Mapping[str, dict]) -> Tuple[List[dict], Dict[str, int]]:
    draft_rows = list(draft["rows"])
    draft_ids = {str(row.get("original_exception_id") or "") for row in draft_rows}
    strict_overlap = draft_ids & STRICT_EXCEPTION_IDS
    if strict_overlap and draft_ids != STRICT_EXCEPTION_IDS:
        raise FinalRecordsRefused(
            "the fixed 34-exception population may not be partially replaced or mixed")
    strict_population = draft_ids == STRICT_EXCEPTION_IDS
    final = []
    for row in sorted(draft_rows, key=lambda r: r["original_exception_id"]):
        exc_id = row["original_exception_id"]
        occ = row.get("occurrence_or_obligation") or {}
        adapter = _require_text(occ.get("adapter"), f"exception {exc_id}.adapter")
        kind = _require_text(occ.get("kind"), f"exception {exc_id}.kind")
        scope = _require_text(occ.get("scope"), f"exception {exc_id}.scope")
        accession = occ.get("accession_number")
        entity, year = _parse_scope(scope)
        audited = row.get("current_audited_output") or {}
        replacement = audited.get("replacement_or_candidate_accession")
        primary = _filing_application(con, accession=accession, entity=entity, year=year,
                                      regime_hint="549" if adapter == "form549d" else None)
        replacement_application = (_filing_application(con, accession=replacement, entity=entity)
                                   if replacement else None)
        effective = replacement_application if replacement_application and \
            replacement_application["filings"] else primary
        historical_blocker = con.execute(
            "SELECT blocker_id,adapter,scope,kind,summary,attempts,exact_error,"
            "human_decision_needed,resolved_at FROM blockers WHERE blocker_id=?",
            (exc_id,)).fetchone()
        blocker_record = dict(historical_blocker) if historical_blocker else None
        matching_blockers, unresolved_blockers = _matching_blockers(
            con, exc_id, adapter, scope)

        action: str
        residual: str
        exact_verification: Optional[Dict[str, Any]] = None
        if strict_population and exc_id in CAPACITY_EXCEPTION_EXPECTATIONS:
            exact_verification = _verify_capacity_exception(con, exc_id)
            expected_accession = CAPACITY_EXCEPTION_EXPECTATIONS[exc_id][0]
            if (adapter != "capacity" or kind != "source"
                    or accession != expected_accession
                    or scope != "capacity:" + expected_accession):
                exact_verification["failures"].append(
                    "draft occurrence identity differs from the fixed capacity exception")
                exact_verification["accepted"] = False
            supported = exact_verification["accepted"] and accession in a17["accessions"]
            if supported:
                disposition = "safely_gated_hash_bound_offline_ocr"
                action = ("Rendered the exact cached image-only PDF with mandatory pdftoppm, "
                          "ran the local Vision OCR production path, required its values, "
                          "units, subjects and caveats to agree with the hash-bound independent "
                          "manual review, and checked direct value-row lineage in Build A.")
                residual = ("The automatic claim is limited to the two exact source/render "
                            "identities and the tested local OCR runtime; changed or unseen image "
                            "formats remain fail-closed.")
            else:
                disposition = "open_capability_or_application_gap"
                action = "Recorded the exact image-source gap without claiming automatic extraction."
                residual = "The A17 gate or an exact Build A value/unit/qualifier/lineage check failed."
        elif strict_population and exc_id in IOC_ACCESS_EXPECTATIONS:
            exact_verification = _verify_ioc_access_exception(con, exc_id)
            expected_accession, expected_entity, expected_replacement, _, _ = \
                IOC_ACCESS_EXPECTATIONS[exc_id]
            if (adapter != "ioc" or kind != "access" or accession != expected_accession
                    or scope != f"{expected_entity}:{expected_accession}"
                    or replacement != expected_replacement):
                exact_verification["failures"].append(
                    "draft occurrence/replacement differs from the fixed IOC access exception")
                exact_verification["accepted"] = False
            supported = exact_verification["accepted"]
            if supported:
                disposition = "resolved_with_public_replacement"
                action = (f"Retained metadata-only occurrence {accession} without an invented "
                          f"HTTP status and applied public replacement {replacement} with its "
                          "own filing occurrence, exact native owner and source-linked observations.")
                residual = ("The metadata-only source occurrence is preserved; replacement "
                            "evidence does not retroactively make it a downloadable filing.")
            else:
                disposition = "open_public_replacement_application"
                action = ("Preserved metadata status separately from HTTP observations and "
                          "recorded the public replacement candidate without claiming application.")
                residual = "One or more exact metadata/replacement/application checks failed."
        elif strict_population and exc_id in IOC_EXCEPTION_EXPECTATIONS:
            exact_verification = _verify_ioc_exception(con, exc_id)
            expected_accession = IOC_EXCEPTION_EXPECTATIONS[exc_id][0]
            if (adapter != "ioc" or kind != "source"
                    or (accession is not None and accession != expected_accession)):
                exact_verification["failures"].append(
                    "draft occurrence identity differs from the fixed IOC parser exception")
                exact_verification["accepted"] = False
            supported = exact_verification["accepted"]
            if supported:
                disposition = "resolved_parser_replay_verified"
                action = ("Replayed the exact IOC occurrence through the bounded native-header "
                          "parser; verified source bytes, parse branch/counts, native CID ownership, "
                          "canonical/revision state and resolvable occurrence-specific lineage.")
                residual = ("Resolution is scoped to the captured official bytes and current declared "
                            "format branches; unseen future formats remain fail-closed source states.")
            else:
                disposition = "open_ioc_replay_gap"
                action = "Recorded the exact IOC occurrence without inferring successful ingestion."
                residual = "One or more exact parse/ownership/version/lineage checks failed."
        elif strict_population and exc_id in FORM549D_SEMANTIC_EXPECTATIONS:
            exact_verification = _verify_form549d_semantic_exception(con, exc_id)
            expected_entity, expected_year = FORM549D_SEMANTIC_EXPECTATIONS[exc_id][:2]
            if (adapter != "form549d" or kind != "semantic"
                    or entity != expected_entity or year != expected_year):
                exact_verification["failures"].append(
                    "draft occurrence differs from the fixed Form 549D filer-year exception")
                exact_verification["accepted"] = False
            supported = exact_verification["accepted"]
            if supported:
                disposition = "safely_gated_semantic_source_qualification"
                action = ("Replayed the exact Form 549D filer-year and independently checked its "
                          "native service rows, billing amounts, period/version identity, "
                          "transportation-only output, ambiguity state and lineage.")
                residual = ("Why the filer populated out-of-scope storage columns is not established; "
                            "the values remain review evidence rather than transportation revenue.")
            else:
                disposition = "open_549d_semantic_application"
                action = "Retained the semantic exception; no value was reclassified as accepted revenue."
                residual = "One or more exact filer-year service/billing/period/revision checks failed."
        elif strict_population and exc_id in FORM549D_POLICY_EXCEPTION_IDS:
            exact_verification = _verify_form549d_policy_exception(con, exc_id)
            if (adapter != "form549d" or kind != "semantic"
                    or scope != "549D annual revenue scope"):
                exact_verification["failures"].append(
                    "draft occurrence differs from the fixed Form 549D policy exception")
                exact_verification["accepted"] = False
            supported = exact_verification["accepted"]
            if supported:
                disposition = "safely_gated_semantic_source_qualification"
                action = ("Consolidated the two duplicate policy links without deleting either "
                          "historical exception; independently recomputed the included-universe "
                          "Storage-row population and retained the 544-row audit population as a "
                          "separately labelled historical baseline.")
                residual = ("The source-defined transportation exclusion is applied; display of "
                            "the as-filed out-of-scope amounts remains an explicit review decision.")
            else:
                disposition = "open_549d_semantic_application"
                action = "Retained the duplicate policy links and did not claim consolidation."
                residual = "The policy population/consolidation gate failed."
        elif strict_population and exc_id in FORM549D_CREDENTIAL_EXPECTATIONS:
            exact_verification = _verify_form549d_credential_exception(con, exc_id)
            expected_entity = FORM549D_CREDENTIAL_EXPECTATIONS[exc_id]
            if (adapter != "form549d" or kind != "source" or scope != expected_entity):
                exact_verification["failures"].append(
                    "draft occurrence differs from the fixed Form 549D source exception")
                exact_verification["accepted"] = False
            supported = exact_verification["accepted"]
            if supported:
                disposition = "resolved_failure_classification_verified"
                action = ("Verified the exact cached quarterly input set, native filing periods "
                          "and present downstream observations, with no unresolved source or "
                          "configuration blocker misreported as a FERC access failure.")
                residual = "No live-source conclusion is inferred beyond the declared cached inputs."
            else:
                disposition = "open_source_or_configuration_limit"
                action = "Recorded the entity-level source/configuration outcome without a false success."
                residual = "The exact cached-period/application gate failed."
        elif adapter == "capacity":
            supported = accession in a17["accessions"] and (
                primary["documents"] > 0 or primary["observations"] > 0)
            if supported:
                disposition = "safely_gated_hash_bound_offline_ocr"
                action = ("Rendered the exact cached image-only PDF with mandatory pdftoppm, "
                          "ran the local Vision OCR production path, and required its values, "
                          "units, subjects and caveats to agree with the hash-bound independent "
                          "manual review.")
                residual = ("The automatic claim is limited to the two exact source/render "
                            "identities and the tested local OCR runtime; changed or unseen image "
                            "formats remain fail-closed.")
            else:
                disposition = "open_capability_or_application_gap"
                action = "Recorded the exact image-source gap without claiming automatic extraction."
                residual = "The A17 gate or corresponding Build A document/observation is absent."
        elif adapter == "ioc" and kind == "access":
            supported = bool(replacement and replacement_application
                             and replacement_application["filings"] > 0
                             and replacement_application["observations"] > 0)
            if supported:
                disposition = "resolved_with_public_replacement"
                action = (f"Retained metadata-only occurrence {accession} without an invented "
                          f"HTTP status and applied public replacement {replacement} with its "
                          "own filing occurrence and observations.")
                residual = ("The metadata-only source occurrence is preserved; replacement "
                            "evidence does not retroactively make it a downloadable filing.")
            else:
                disposition = "open_public_replacement_application"
                action = ("Preserved metadata status separately from HTTP observations and "
                          "recorded the public replacement candidate without claiming application.")
                residual = "No applied public replacement filing plus observations exists in Build A."
        elif adapter == "ioc":
            supported = primary["filings"] > 0 and primary["observations"] > 0
            if not accession and entity:
                supported = primary["observations"] > 0
            if supported:
                disposition = "resolved_parser_replay_verified"
                action = ("Replayed the exact IOC occurrence/entity through the bounded native-header "
                          "parser and retained filing/observation occurrence identity.")
                residual = ("Resolution is scoped to the captured official bytes and current declared "
                            "format branches; unseen future formats remain fail-closed source states.")
            else:
                disposition = "open_ioc_replay_gap"
                action = "Recorded the exact IOC occurrence without inferring successful ingestion."
                residual = "Build A has no matching filing and observation population."
        elif adapter == "form549d" and kind == "semantic":
            supported = primary["observations"] > 0
            if not entity:  # duplicate policy-level rows cover the four filer-year records.
                policy_scopes = {(str(r[0]), int(r[1])) for r in con.execute(
                    "SELECT entity_key,reporting_year FROM observations "
                    "WHERE entity_key IN ('C000826','C001773') "
                    "AND reporting_year IN (2024,2025) AND source_regime LIKE '%549%' "
                    "GROUP BY entity_key,reporting_year")}
                supported = policy_scopes == {
                    ("C000826", 2024), ("C000826", 2025),
                    ("C001773", 2024), ("C001773", 2025)}
            if supported:
                disposition = "safely_gated_semantic_source_qualification"
                action = ("Replayed scoped Form 549D rows, preserved storage-labelled source amounts, "
                          "kept them outside transportation revenue, and retained the policy/scope "
                          "qualification for this individual exception.")
                residual = ("Why the filer populated out-of-scope storage columns is not established; "
                            "the values remain review evidence rather than transportation revenue.")
            else:
                disposition = "open_549d_semantic_application"
                action = "Retained the semantic exception; no value was reclassified as accepted revenue."
                residual = "The exact filer-year/policy population is absent from Build A."
        elif adapter == "form549d":
            supported = primary["filings"] > 0 or primary["observations"] > 0
            if supported:
                disposition = "resolved_failure_classification_verified"
                action = ("Replayed the entity through the distinct cached/offline failure classes and "
                          "retained its actual Build A filing/observation state.")
                residual = "No source-access conclusion is inferred beyond the captured request state."
            else:
                disposition = "open_source_or_configuration_limit"
                action = "Recorded the entity-level source/configuration outcome without a false success."
                residual = "Build A contains no matching filing or observation population."
        else:
            raise FinalRecordsRefused(f"exception {exc_id} has unsupported adapter/kind")

        if ((disposition.startswith("resolved_") or disposition.startswith("safely_gated_"))
                and unresolved_blockers):
            disposition = "data_repaired_but_persisted_blocker_open"
            residual = ("Matching Build A data exists, but the database still carries an "
                        "unresolved blocker for the historical ID or the same adapter/scope; "
                        "it is not represented as closed.")
            action += " The unresolved persisted blocker is retained in the final record."
        accepted = disposition.startswith("resolved_") or disposition.startswith("safely_gated_")
        evidence = {
            "draft_evidence": row["evidence"],
            "database": {"primary": primary, "replacement": replacement_application,
                         "historical_blocker": blocker_record,
                         "matching_scope_blockers": matching_blockers,
                         "unresolved_scope_blockers": unresolved_blockers,
                         "exact_exception_gate": exact_verification},
            "a17_gate": a17["identity"] if adapter == "capacity" else None,
        }
        if not evidence["draft_evidence"]:
            raise FinalRecordsRefused(f"exception {exc_id} has empty evidence")
        final.append({
            "original_exception_id": exc_id,
            "related_original_issue_ids": row.get("related_original_issue_ids") or [],
            "related_final_finding_ids": row.get("related_final_finding_ids") or [],
            "occurrence_or_obligation": occ, "audit_claim": audit_by_id[exc_id],
            "root_cause": _require_text(row.get("root_cause"),
                                        f"exception {exc_id}.root_cause"),
            "action_taken": action, "current_output": {
                "primary": primary, "replacement_accession": replacement,
                "replacement": replacement_application, "persisted_blocker": blocker_record,
                "matching_scope_blockers": matching_blockers,
                "exact_exception_gate": exact_verification},
            "disposition": disposition,
            "acceptance_result": "accepted" if accepted else "qualified_open",
            "obligation_effect": (occ.get("obligation_level_effect")
                                  or "Retained as an individually identified source/semantic state; "
                                     "not deleted from the obligation population."),
            "residual_limitation": residual, "evidence": evidence,
        })
    counts = dict(collections.Counter(r["disposition"] for r in final))
    return final, counts


def _repair_ledger_records(root: pathlib.Path, con: sqlite3.Connection,
                           draft: Mapping[str, Any], test_runs: Mapping[str, Any],
                           db_checks: Mapping[str, Any], publication: Mapping[str, Any],
                           validation: Mapping[str, Any], input_records: Sequence[dict],
                           exception_records: Sequence[dict], a17: Mapping[str, Any],
                           audit_by_id: Mapping[str, dict]) -> List[dict]:
    open_inputs = [r for r in input_records if r["acceptance_result"] != "accepted_for_candidate"]
    open_exceptions = [r for r in exception_records if r["acceptance_result"] != "accepted"]
    affected_open_issue_ids = set()
    for row in open_exceptions:
        affected_open_issue_ids.update(row.get("related_original_issue_ids") or [])
    if open_inputs:
        affected_open_issue_ids.update({"A16", "A20"})
    exception_by_id = {row["original_exception_id"]: row for row in exception_records}

    def exact_exception_gate(exception_ids: Iterable[str]) -> bool:
        selected = [exception_by_id.get(exception_id) for exception_id in exception_ids]
        return bool(selected) and all(
            item is not None
            and (item.get("current_output") or {}).get("exact_exception_gate", {}).get("accepted")
            and item.get("acceptance_result") == "accepted"
            for item in selected)

    access_gate = exact_exception_gate(IOC_ACCESS_EXPECTATIONS)
    credential_gate = exact_exception_gate(FORM549D_CREDENTIAL_EXPECTATIONS)
    ioc_format_gate = exact_exception_gate(IOC_EXCEPTION_EXPECTATIONS)
    capacity_gate = exact_exception_gate(CAPACITY_EXCEPTION_EXPECTATIONS)
    readiness = db_checks.get("field_readiness") or {}
    all_readiness = readiness.get("all_fields") or {}
    headline_readiness = readiness.get("headline_fields") or {}
    readiness_gate = (
        int(all_readiness.get("rows", -1)) == db_checks["table_counts"]["field_status"]
        and 0 <= int(all_readiness.get("validated_in_template", -1))
        <= int(all_readiness.get("has_data_in_template", -1))
        <= int(all_readiness.get("adapter_implemented", -1))
        <= int(all_readiness.get("rows", -1))
        and 0 <= int(headline_readiness.get("validated_in_template", -1))
        <= int(headline_readiness.get("has_data_in_template", -1))
        <= int(headline_readiness.get("adapter_implemented", -1))
        <= int(headline_readiness.get("rows", -1)))
    # The exact Horizon evidence is required only when this draft contains the
    # W5 disposition.  Keeping the gate lazy lets narrowly scoped ledger tests
    # exercise unrelated A15/readiness logic against an isolated in-memory
    # database; a partial invocation must not acquire hidden dependencies on a
    # populated project database or its exports.
    has_w5 = any(row.get("issue_id") == "W5-FOSSIL"
                 for row in draft.get("rows", []))
    w5_counterevidence: Optional[Dict[str, Any]] = None
    if has_w5:
        w5_counterevidence = _w5_horizon_attached_unit_counterevidence_gate(root, con)
        if w5_counterevidence["failures"]:
            raise FinalRecordsRefused(
                "W5-FOSSIL exact attached-unit counterevidence failed Build A: "
                f"{w5_counterevidence['failures']}")
    final = []
    for row in sorted(draft["rows"], key=lambda r: r["issue_id"]):
        issue_id = row["issue_id"]
        related = set(row.get("original_issue_ids") or []) | {issue_id}
        targeted_test = _require_text(row.get("targeted_test"),
                                      f"repair ledger {issue_id}.targeted_test")
        dynamic_gate: Optional[Dict[str, Any]] = None
        if row.get("current_disposition") == "contradicted_with_evidence":
            disposition = "contradicted_with_reproducible_counterevidence"
            acceptance = "accepted_counterevidence_at_build_a_boundary"
            residual = ("The original claim remains preserved; candidate acceptance relies on the "
                        "hash-bound Build A database, exports, validation and test runs named here.")
        elif issue_id == "A15" and access_gate:
            disposition = "fixed_with_reproducible_metadata_http_counterevidence"
            acceptance = "accepted_at_build_a_boundary"
            residual = ("All five metadata-N occurrences retain zero attempted requests and null "
                        "HTTP observations, and each public same-period replacement—including "
                        "Gulfstream 20250401-5098—passes its exact Build A occurrence, ownership, "
                        "observation and lineage gate. This does not change the frozen audit state.")
            targeted_test = ("For all five fixed metadata-N IDs, require zero filing/document/"
                             "observation rows for the metadata occurrence, no request and null HTTP "
                             "evidence, plus the exact public replacement's canonical CID/period, "
                             "observation count and resolvable lineage.")
            dynamic_gate = {"exception_ids": sorted(IOC_ACCESS_EXPECTATIONS),
                            "all_exact_gates_accepted": True}
        elif issue_id == "A16-CREDENTIAL" and credential_gate:
            disposition = "fixed_and_verified_cached_failure_classification"
            acceptance = "accepted_at_build_a_boundary"
            residual = ("Both originally misclassified entities have the exact declared cached "
                        "2024Q1-2026Q2 filing periods, correct periodic/revision semantics and "
                        "present downstream observations, with no unresolved source/configuration "
                        "blocker. No broader live-network conclusion is inferred.")
            targeted_test = ("For C000435 and C004698, require the exact canonical 2024Q1-2026Q2 "
                             "cached filing set, native rows and present observations; reject "
                             "periodic filings with snapshot timestamps or unresolved source/"
                             "configuration blockers.")
            dynamic_gate = {"exception_ids": sorted(FORM549D_CREDENTIAL_EXPECTATIONS),
                            "all_exact_gates_accepted": True}
        elif issue_id in {"W2-EXTRA-02", "W2-EXTRA-03"} and ioc_format_gate:
            dispositions = {
                "W2-EXTRA-02": "fixed_and_verified_exact_ioc_header_unit_population",
                "W2-EXTRA-03": "resolved_by_same_population_reconciliation",
            }
            disposition = dispositions[issue_id]
            acceptance = "accepted_at_build_a_boundary"
            ioc_gates = [(exception_by_id[exception_id]["current_output"]
                          ["exact_exception_gate"])
                         for exception_id in sorted(IOC_EXCEPTION_EXPECTATIONS)]
            storage_blank = sum(gate["checks"].get("header_units", {}).get("storage_state")
                                == "blank" for gate in ioc_gates)
            numeric_unitless = sum(int(gate["checks"].get(
                "present_numeric_unitless_observations", 0)) for gate in ioc_gates)
            total_observations = sum(int(gate["checks"].get("actual_observations", 0))
                                     for gate in ioc_gates)
            dynamic_gate = {
                "population": "the exact 19 original IOC parser exception occurrences",
                "occurrences": len(ioc_gates),
                "storage_header_unit_blank_occurrences": storage_blank,
                "present_numeric_unitless_observations": numeric_unitless,
                "total_canonical_observations": total_observations,
                "all_exact_parse_owner_lineage_gates_accepted": True,
            }
            residual = ("The original differently scoped 7/158 narrative remains preserved but is "
                        "not reused as an acceptance target. Both header-unit and emitted numeric-"
                        "unit counts are recomputed over the same enumerated 19-occurrence Build A "
                        "population; blank storage units remain explicit rather than defaulted.")
            targeted_test = ("Over the exact 19 IOC parser exceptions, verify native CID, guarded "
                             "format branch, exact source/fact/observation counts, header unit state, "
                             "no present numeric unitless output and fully resolving lineage; report "
                             "both unit counts from that same population.")
        elif issue_id == "R20-FIELD-READINESS-NOT-DATA-COMPLETENESS" and readiness_gate:
            disposition = "fixed_with_build_a_readiness_dimensions"
            acceptance = "accepted_at_build_a_boundary"
            dynamic_gate = {
                "audited_frozen_baseline": {
                    "all_fields": {"rows": 168, "has_data_in_template": 151,
                                   "validated_in_template": 140},
                    "headline_fields": {"rows": 44, "has_data_in_template": 43,
                                        "validated_in_template": 40},
                },
                "build_a": readiness,
                "bridge": {
                    "all_data_change": int(all_readiness["has_data_in_template"]) - 151,
                    "all_validated_change": int(all_readiness["validated_in_template"]) - 140,
                    "headline_data_change": int(headline_readiness["has_data_in_template"]) - 43,
                    "headline_validated_change":
                        int(headline_readiness["validated_in_template"]) - 40,
                },
            }
            residual = ("The audited frozen counts remain a baseline, not a target. The final record "
                        "reports Build A's actual adapter/data/validation populations and the exact "
                        "bridge; an implemented adapter still does not imply data or validation.")
            targeted_test = ("Recompute field and headline adapter/data/validation counts from the "
                             "Build A field_status rows, require their monotonic relationship and "
                             "published-export equality, and report the before/after bridge without "
                             "forcing audited baseline counts.")
        elif issue_id == "W5-FOSSIL":
            if w5_counterevidence is None:  # defensive; has_w5 makes this unreachable
                raise FinalRecordsRefused(
                    "W5-FOSSIL was selected without its exact counterevidence gate")
            disposition = "fixed_and_verified_with_bounded_counterevidence"
            acceptance = "accepted_at_build_a_boundary"
            dynamic_gate = w5_counterevidence
            residual = (
                "The original qualified fossil-risk claim remains preserved. The two exact "
                "Horizon rows rebut their v9 classification as fossils because each current "
                "occurrence and export carries 380,000 immediately followed by the recognised "
                "filed unit MMBTU/d. That bounded counterevidence is paired with the separately "
                "executed changed/identical-regeneration mutation test; it is not generalized "
                "from value equality.")
        elif related & affected_open_issue_ids:
            disposition = "implemented_with_explicit_open_data_or_source_limit"
            acceptance = "qualified_at_build_a_boundary"
            residual = ("The control is implemented and tested, but one or more individually listed "
                        "inputs/exceptions has no accepted Build A consumer application.")
        elif issue_id == "A17" and capacity_gate:
            disposition = "safely_gated_hash_bound_offline_ocr"
            acceptance = "accepted_with_capability_boundary_at_build_a"
            residual = ("Automatic local Vision OCR is accepted only for the two exact source and "
                        "render identities and must agree with the independent manual review.")
        elif row.get("current_disposition") == "still_open" \
                or "open" in str(row.get("current_disposition") or "").lower() \
                or "partial" in str(row.get("current_disposition") or "").lower():
            disposition = "implementation_claim_remains_open_pending_specific_evidence"
            acceptance = "qualified_at_build_a_boundary"
            residual = ("The implementation draft still marks this finding open/partial. The "
                        "generator preserves that state until the owning repair supplies the "
                        "specific code, data/export and executed-test evidence needed to revise it.")
        else:
            disposition = "fixed_and_verified_in_build_a_candidate"
            acceptance = "accepted_at_build_a_boundary"
            residual = ("Release shipping, clean-extraction Build B and local promotion are later "
                        "boundaries and are not certified by this pre-package record.")
        gates = {
            "code": "bound_to_publication_code_snapshot",
            "data": "verified_in_read_only_build_a_database",
            "exports": "matched_to_published_generation_and_database",
            "tests": "mapped_to_observed_successful_verbose_test_ids",
            "shipping": "outside_build_a_record_boundary",
        }
        final.append({
            "issue_id": issue_id, "source_row_type": row.get("source_row_type"),
            "original_issue_ids": row.get("original_issue_ids") or [],
            "severity": row.get("severity"), "audit_claim": audit_by_id[issue_id],
            "affected_component_or_data": row.get("affected_component_or_data") or [],
            "root_cause": _require_text(row.get("root_cause"),
                                        f"repair ledger {issue_id}.root_cause"),
            "targeted_test": targeted_test,
            "targeted_test_status": "acceptance_specification_label_not_an_executed_test_id",
            "disposition": disposition, "acceptance_result": acceptance,
            "gates": gates, "closure_status": (
                "qualified_open_limit" if "qualified" in acceptance
                else "accepted_candidate_not_release_closed"),
            "evidence": {
                "audit": (row.get("evidence") or {}).get("audit", []),
                "candidate_paths": (row.get("evidence") or {}).get("candidate", []),
                "build_a_database": db_checks["identity"],
                "publication_generation_id": publication["generation_id"],
                "validation": validation["identity"],
                "executed_test_ids": test_runs["issue_test_evidence"][issue_id],
                "a17_gate": a17["identity"] if issue_id == "A17" else None,
                "dynamic_acceptance_gate": dynamic_gate,
                "repair_counterevidence": row.get("repair_counterevidence"),
            },
            "residual_limitation": residual,
            "audit_claim_preserved": row.get("audit_claim_preserved") is True,
        })
    return final


def _check_operational_placeholders(ledger: Sequence[dict], exceptions: Sequence[dict],
                                    inputs: Sequence[dict]) -> None:
    fields = []
    for row in ledger:
        fields.extend((f"ledger {row['issue_id']} disposition", row["disposition"],
                       f"ledger {row['issue_id']} acceptance", row["acceptance_result"],
                       f"ledger {row['issue_id']} closure", row["closure_status"]))
        for key, value in row["gates"].items():
            fields.extend((f"ledger {row['issue_id']} gate {key}", value))
    for row in exceptions:
        fields.extend((f"exception {row['original_exception_id']} disposition", row["disposition"],
                       f"exception {row['original_exception_id']} action", row["action_taken"],
                       f"exception {row['original_exception_id']} effect", row["obligation_effect"]))
    for row in inputs:
        fields.extend((f"input {row['input_id']} disposition", row["disposition"],
                       f"input {row['input_id']} acceptance", row["acceptance_result"]))
    for label, value in zip(fields[0::2], fields[1::2]):
        if not str(value or "").strip() or PLACEHOLDER.search(str(value)):
            raise FinalRecordsRefused(f"{label} is empty or unresolved placeholder text: {value!r}")


def _csv_bytes(rows: Sequence[dict], columns: Sequence[str]) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=list(columns), lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        encoded = {}
        for col in columns:
            value = row.get(col)
            if isinstance(value, (dict, list)):
                encoded[col] = json.dumps(value, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":"))
            elif value is None:
                encoded[col] = ""
            else:
                encoded[col] = value
        writer.writerow(encoded)
    return out.getvalue().encode("utf-8")


def _pct(numerator: int, denominator: int) -> str:
    return "n/a" if not denominator else f"{100.0 * numerator / denominator:.4f}%"


def _additional_inputs_have_limits(rows: Sequence[Mapping[str, Any]]) -> bool:
    """Return true only for a genuine additional-input acceptance gap."""
    return any(row.get("acceptance_result") not in ADDITIONAL_INPUT_ACCEPTANCE_RESULTS
               for row in rows)


def _report(generated_at: str, db_checks: Mapping[str, Any],
            publication: Mapping[str, Any], validation: Mapping[str, Any],
            coverage: Mapping[str, Any], baseline: Mapping[str, Any],
            fields: Mapping[str, Any], ledger: Sequence[dict], exceptions: Sequence[dict],
            inputs: Sequence[dict], tests: Mapping[str, Any],
            related: Sequence[dict] = (), additional_inputs: Sequence[dict] = ()) -> bytes:
    baseline_headline = (baseline.get("coverage_headline") or {})
    issue_counts = collections.Counter(r["disposition"] for r in ledger)
    exception_counts = collections.Counter(r["acceptance_result"] for r in exceptions)
    input_counts = collections.Counter(r["acceptance_result"] for r in inputs)
    additional_input_counts = collections.Counter(
        r["acceptance_result"] for r in additional_inputs)
    additional_applied_kinds = collections.Counter(
        r.get("input_kind") for r in additional_inputs
        if r.get("acceptance_result") == "accepted_for_candidate")
    field_outcomes = dict(collections.Counter(
        r[0] for r in []))  # populated below from the database extract in evidence index
    lines = [
        "# Integrated operating-assets repair report",
        "",
        f"Record boundary: Build A candidate generated at `{generated_at}`. This is an "
        "implementation-owner self-test record, not a new independent audit and not a "
        "post-package receipt.",
        "",
        "## Outcome",
        "",
        "The Build A database passed SQLite integrity and foreign-key checks, the consumer "
        "exports matched one hash-bound published generation, integrated validation had no "
        "failures or errors, and every final-audit issue plus all 34 exception IDs was mapped "
        "to successful non-zero test evidence. Open source/application states remain explicit.",
        "",
        "| Verdict area | Build A verdict | Basis |",
        "|---|---:|---|",
        "| Runtime safety | accepted for candidate | Successful complete suite(s), validation, "
        "no running run/checkpoint state |",
        "| Semantic validity | accepted or explicitly gated | Database/export agreement, "
        "coverage recomputation, seven annotations and four Fayetteville warnings |",
        f"| Source completeness | {'accepted for declared executable inputs' if not input_counts.get('qualified_open_data_application') and not additional_input_counts.get('qualified_open_data_application') else 'qualified'} | "
        f"{input_counts.get('accepted_for_candidate', 0)} of 13 recovered inputs applied; "
        f"{input_counts.get('qualified_open_data_application', 0)} remain explicit; "
        f"{additional_applied_kinds.get('accession_package', 0)} repair-discovered "
        "accession package applied; "
        f"{additional_applied_kinds.get('elibrary_dependency_capture_bundle', 0)} mandatory "
        "eLibrary dependency bundle applied; "
        f"{additional_input_counts.get('accepted_as_repair_evidence_not_execution_prerequisite', 0)} "
        "captured search responses accepted as diagnostic evidence rather than final-plan "
        f"execution prerequisites; {additional_input_counts.get('qualified_open_data_application', 0)} "
        "remain qualified |",
        "| Offline reproduction | not tested at this boundary | Clean-extraction Build B is a "
        "post-package gate |",
        "| Release integrity | not tested at this boundary | Archive/receipt do not yet exist |",
        "| Local integration | not promoted by this generator | Promotion requires a separate "
        "active-writer check and rollback boundary |",
        "",
        "## Exact database and publication state",
        "",
        f"- Database: `{db_checks['identity']['bytes']}` bytes, SHA-256 "
        f"`{db_checks['identity']['sha256']}`.",
        f"- Publication generation: `{publication['generation_id']}`; code snapshot "
        f"`{publication['metadata'].get('code_snapshot')}`; input snapshot "
        f"`{publication['metadata'].get('input_snapshot')}`; database semantic identity "
        f"`{publication['metadata'].get('database_identity')}`.",
        f"- Integrated validation: {validation['summary']['PASS']} pass, "
        f"{validation['summary']['FAIL']} fail, {validation['summary']['ERROR']} error, "
        f"{validation['summary']['SKIPPED']} skipped.",
        "",
        "## Coverage bridge",
        "",
        "| Measure | Audited frozen baseline | Build A candidate | Change |",
        "|---|---:|---:|---:|",
        f"| All slots | {baseline_headline.get('total', 'n/a')} | {coverage['expected_total']} | "
        f"{coverage['expected_total'] - int(baseline_headline.get('total', 0)):+d} |",
        f"| Core slots | {baseline_headline.get('core', 'n/a')} | {coverage['expected_core']} | "
        f"{coverage['expected_core'] - int(baseline_headline.get('core', 0)):+d} |",
        f"| Core populated | {baseline_headline.get('core_populated', 'n/a')} | "
        f"{coverage['core_populated']} | "
        f"{coverage['core_populated'] - int(baseline_headline.get('core_populated', 0)):+d} |",
        f"| Core validated | {baseline_headline.get('core_validated', 'n/a')} | "
        f"{coverage['core_validated']} | "
        f"{coverage['core_validated'] - int(baseline_headline.get('core_validated', 0)):+d} |",
        "",
        f"Full-plan core populated coverage is {coverage['core_populated']} / "
        f"{coverage['expected_core']} = {_pct(coverage['core_populated'], coverage['expected_core'])}; "
        f"validated coverage is {coverage['core_validated']} / {coverage['expected_core']} = "
        f"{_pct(coverage['core_validated'], coverage['expected_core'])}. Due-to-date core "
        f"populated coverage is {coverage['core_due_populated']} / "
        f"{coverage['expected_core_due']} = "
        f"{_pct(coverage['core_due_populated'], coverage['expected_core_due'])}; validated is "
        f"{coverage['core_due_validated']} / {coverage['expected_core_due']} = "
        f"{_pct(coverage['core_due_validated'], coverage['expected_core_due'])}. "
        f"Excluded future slots: {coverage['expected_core'] - coverage['expected_core_due']}.",
        "",
        "## Field reconciliation",
        "",
        f"The generated crosswalk accounts for {fields['audited_rows']} original audit rows "
        f"through {fields['field_crosswalk_rows']} mapping rows and exactly "
        f"{fields['target_keys']} current template/field targets. The requirements crosswalk "
        f"contains {fields['requirements_rows']} source rows. No lost or partially-lost "
        "disposition is accepted.",
        "",
        "## Findings, exceptions and inputs",
        "",
        f"- Repair ledger: {len(ledger)} audited rows plus {len(related)} related "
        f"repair-time findings ({len(ledger) + len(related)} total). Audited-row dispositions: "
        f"`{json.dumps(dict(sorted(issue_counts.items())), sort_keys=True)}`.",
        f"- Original exceptions: {len(exceptions)} unique rows; "
        f"{exception_counts.get('accepted', 0)} accepted as resolved or safely gated and "
        f"{exception_counts.get('qualified_open', 0)} retained as explicit open limitations.",
        f"- Final input inventory: {len(inputs)} unique official-FERC request identities; "
        f"{input_counts.get('accepted_for_candidate', 0)} applied and "
        f"{input_counts.get('qualified_open_data_application', 0)} captured but unapplied.",
        f"- Additional repair-time input inventory: {len(additional_inputs)} separately "
        f"labelled identities; {additional_applied_kinds.get('accession_package', 0)} "
        "accession package applied to Build A, "
        f"{additional_applied_kinds.get('elibrary_dependency_capture_bundle', 0)} mandatory "
        "eLibrary dependency bundle applied to Build A, "
        f"{additional_input_counts.get('accepted_as_repair_evidence_not_execution_prerequisite', 0)} "
        "bounded search responses retained as exact diagnostic evidence but not selected by "
        f"the versioned proceeding seed, and "
        f"{additional_input_counts.get('qualified_open_data_application', 0)} remain qualified.",
        "",
        "A safely gated item is limited to its demonstrated capability. In particular, the "
        "A17 gate binds two exact image-only FERC PDFs and page renders, runs the local Vision "
        "OCR production path, and requires the extracted values, units, subjects and caveats "
        "to agree with the preserved independent manual review.",
        "",
        "## Test execution",
        "",
        "| Run | Role | Interpreter | Executed | Pass | Fail | Error | Skip | Xfail | Exit |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in tests["runs"]:
        c = run["counts"]
        lines.append(
            f"| {run['run_id']} | {run['acceptance_role']} | {run['interpreter']} | "
            f"{c['executed']} | {c['pass']} | {c['fail']} | {c['error']} | "
            f"{c['skip']} | {c['xfail']} | {run['exit_code']} |")
    lines += [
        "",
        "Counts are per execution and are not added into a purported unique-test total. "
        "Each command, interpreter, exit code, full-log identity and skip/xfail "
        "classification is in `FINAL_TEST_EVIDENCE_INDEX.json`.",
        "",
        "## Remaining boundaries",
        "",
        "This record does not certify archive construction, clean-extraction Build B, semantic "
        "A/B/package equality, or local promotion. Those later operations must consume these "
        "unchanged candidate bytes and issue external hash-bound receipts; changing the payload "
        "requires regeneration.",
        "",
    ]
    del field_outcomes
    return ("\n".join(lines)).encode("utf-8")


def _atomic_publish(output_dir: pathlib.Path, files: Mapping[str, bytes],
                    generated_at: str) -> Dict[str, Any]:
    if set(files) != set(FINAL_FILES):
        raise FinalRecordsRefused("internal final-record file set is incomplete")
    identities = {name: {"bytes": len(raw), "sha256": _sha(raw)}
                  for name, raw in sorted(files.items())}
    generation_id = _sha(json.dumps(
        {"kind": "final_implementation_records", "files": identities},
        sort_keys=True, separators=(",", ":")).encode("utf-8"))
    generation_rel = pathlib.PurePosixPath(".final_records") / generation_id
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink():
        raise FinalRecordsRefused("final-record output directory may not be a symlink")
    collection_root = output_dir / ".final_records"
    if collection_root.is_symlink():
        raise FinalRecordsRefused("final-record generation root may not be a symlink")
    collection_root.mkdir(parents=True, exist_ok=True)
    collection_root = _path_within(collection_root, output_dir,
                                   "final-record generation root")
    generation = _path_within(collection_root / generation_id, collection_root,
                              "final-record generation")
    if generation.exists():
        if generation.is_symlink() or not generation.is_dir():
            raise FinalRecordsRefused("existing final-record generation is not a directory")
        for name, expected in identities.items():
            _expected_identity(generation / name, expected, f"existing final record {name}")
    else:
        parent = generation.parent
        temp = pathlib.Path(tempfile.mkdtemp(prefix=".final-records-", dir=str(parent)))
        _path_within(temp, collection_root, "temporary final-record generation")
        try:
            for name, raw in files.items():
                path = temp / name
                with path.open("wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            for name, expected in identities.items():
                _expected_identity(temp / name, expected, f"staged final record {name}")
            os.replace(str(temp), str(generation))
        finally:
            if temp.exists():
                for path in sorted(temp.rglob("*"), reverse=True):
                    if path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                temp.rmdir()

    receipt = {
        "schema": "ferc-final-records-receipt-v1", "status": "published",
        "generated_at": generated_at, "kind": "final_implementation_records",
        "generation_id": generation_id, "generation_path": generation_rel.as_posix(),
        "files": identities,
    }
    receipt_raw = _canonical_json(receipt)
    receipt_path = output_dir / RECEIPT_NAME
    previous: Dict[pathlib.Path, Optional[bytes]] = {
        receipt_path: receipt_path.read_bytes() if receipt_path.is_file() else None}
    staged: Dict[pathlib.Path, pathlib.Path] = {}
    try:
        for name, raw in files.items():
            target = output_dir / name
            previous[target] = target.read_bytes() if target.is_file() else None
            temp_target = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            with temp_target.open("wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            staged[target] = temp_target
        for target, temp_target in staged.items():
            os.replace(str(temp_target), str(target))
        for name, expected in identities.items():
            _expected_identity(output_dir / name, expected, f"published final record {name}")
        temp_receipt = receipt_path.with_name(
            f".{receipt_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        with temp_receipt.open("wb") as handle:
            handle.write(receipt_raw)
            handle.flush()
            os.fsync(handle.fileno())
        staged[receipt_path] = temp_receipt
        os.replace(str(temp_receipt), str(receipt_path))
    except BaseException:
        for target, raw in previous.items():
            try:
                if raw is None:
                    if target.exists():
                        target.unlink()
                else:
                    restore = target.with_name(
                        f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.restore")
                    restore.write_bytes(raw)
                    os.replace(str(restore), str(target))
            except OSError:
                pass
        raise
    finally:
        for temp_target in staged.values():
            if temp_target.exists():
                temp_target.unlink()

    receipt["receipt_identity"] = {
        "path": RECEIPT_NAME, "bytes": len(receipt_raw), "sha256": _sha(receipt_raw)}
    return receipt


def _iso_timestamp(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FinalRecordsRefused(f"invalid generated-at timestamp: {value}") from None
    if parsed.tzinfo is None:
        raise FinalRecordsRefused("generated-at must include an explicit timezone")
    return parsed.isoformat(timespec="seconds")


def build(args: argparse.Namespace) -> Dict[str, Any]:
    root = pathlib.Path(args.root).resolve()
    db_path = pathlib.Path(args.db).resolve()
    output_arg = pathlib.Path(args.output_dir)
    if output_arg.is_symlink():
        raise FinalRecordsRefused("final-record output directory may not be a symlink")
    output_dir = output_arg.resolve()
    logs_dir = pathlib.Path(args.logs_dir).resolve()
    audit_dir = pathlib.Path(args.audit_dir).resolve()
    if not root.is_dir() or root.is_symlink():
        raise FinalRecordsRefused(f"candidate root is absent or a symlink: {root}")
    if not audit_dir.is_dir() or audit_dir.is_symlink():
        raise FinalRecordsRefused(f"audit directory is absent or a symlink: {audit_dir}")
    _path_within(db_path, root, "Build A database")
    _path_within(output_dir, root, "final-record output directory")
    _path_within(logs_dir, root, "test logs directory")

    candidate_paths = {
        "repair-ledger draft": pathlib.Path(args.ledger_draft).resolve(),
        "exception draft": pathlib.Path(args.exception_draft).resolve(),
        "input draft": pathlib.Path(args.input_draft).resolve(),
        "related-finding draft": pathlib.Path(args.related_findings_draft).resolve(),
        "additional-input draft": pathlib.Path(args.additional_inputs_draft).resolve(),
        "test-run manifest": pathlib.Path(args.test_run_manifest).resolve(),
        "validation results": pathlib.Path(args.validation).resolve(),
        "publication receipt": pathlib.Path(args.publication_receipt).resolve(),
    }
    for label, path in candidate_paths.items():
        _path_within(path, root, label)

    drafts = _load_and_validate_drafts(
        root, audit_dir, candidate_paths["repair-ledger draft"],
        candidate_paths["exception draft"], candidate_paths["input draft"])
    supplemental = _load_and_validate_supplemental_drafts(
        root, audit_dir, candidate_paths["related-finding draft"],
        candidate_paths["additional-input draft"])
    audit = _load_audit_population(audit_dir, drafts)
    issue_overlap = (drafts["ids"]["repair ledger"]
                     & supplemental["ids"]["related findings"])
    input_overlap = (drafts["ids"]["input inventory"]
                     & supplemental["ids"]["additional inputs"])
    if issue_overlap or input_overlap:
        raise FinalRecordsRefused(
            "supplemental populations overlap the completed audit populations: "
            f"issues={sorted(issue_overlap)}, inputs={sorted(input_overlap)}")
    run_plan = _verify_run_plan_inputs(root)
    generated_at = _iso_timestamp(args.generated_at or run_plan["plan"].get("built_at", ""))

    con = _open_database(db_path)
    try:
        db_checks = _database_checks(con, db_path, root)
        db_checks["semantic_identity"] = _database_semantic_identity(con)
        redundant_input_alias = _verified_redundant_input_alias(root)
        snapshots = _candidate_snapshots(
            root, alias_verification=redundant_input_alias)
        db_checks["recomputed_snapshots"] = snapshots
        publication = _verify_manifest_generation(
            root, candidate_paths["publication receipt"], root / "exports",
            "consumer_exports")
        for field in ("code_snapshot", "input_snapshot", "database_identity"):
            if not isinstance(publication["metadata"].get(field), str) \
                    or not HEX64.fullmatch(publication["metadata"][field]):
                raise FinalRecordsRefused(f"publication metadata lacks valid {field}")
        for field in ("code_snapshot", "input_snapshot"):
            if publication["metadata"][field] != snapshots[field]:
                raise FinalRecordsRefused(
                    f"publication {field} does not match the current candidate tree")
        if publication["metadata"]["database_identity"] != db_checks["semantic_identity"]:
            raise FinalRecordsRefused(
                "publication receipt database identity does not match the current Build A "
                "semantic record population")
        pub_row = con.execute(
            "SELECT generation_id,code_snapshot,input_snapshot,database_identity,status,"
            "manifest_json "
            "FROM publication_generations WHERE generation_id=?",
            (publication["generation_id"],)).fetchone()
        if not pub_row or tuple(pub_row[:5]) != (
                publication["generation_id"], publication["metadata"]["code_snapshot"],
                publication["metadata"]["input_snapshot"],
                publication["metadata"]["database_identity"], "published"):
            raise FinalRecordsRefused("published database row and export receipt disagree")
        try:
            stored_manifest = json.loads(str(pub_row[5]))
        except (TypeError, json.JSONDecodeError):
            raise FinalRecordsRefused("published database manifest_json is invalid") from None
        if stored_manifest != publication["manifest"]:
            raise FinalRecordsRefused("published database manifest_json and receipt disagree")
        exports = _validate_exports(root, con, db_checks, publication)
        coverage = _coverage_counts(con)
        _compare_coverage_export(coverage, exports["json"]["coverage_statistics.json"])
        _validate_summary_exports(con, exports["json"], coverage)
        crosswalk = _validate_crosswalk(root, con)
        annotations = _validate_annotations(root, con)
        a17 = _validate_a17(root)
        validation = _validation_results(candidate_paths["validation results"], root)
        tests = _validate_test_runs(
            candidate_paths["test-run manifest"], logs_dir,
            drafts["ids"]["repair ledger"] | supplemental["ids"]["related findings"],
            drafts["ids"]["exception ledger"], root)
        input_rows, input_counts = _input_records(
            root, con, drafts["inputs"], audit["input_by_accession"])
        additional_claims = {
            str(row["input_id"]): {
                "status": "not_in_final_independent_audit",
                "relationship": "discovered_during_repair",
                "repair_discovery": row.get("repair_discovery"),
            }
            for row in supplemental["additional_inputs"]["rows"]}
        additional_input_rows, additional_input_counts = _additional_input_records(
            root, con, supplemental["additional_inputs"], additional_claims)
        exception_rows, exception_counts = _exception_records(
            con, drafts["exceptions"], a17, audit["exception_by_id"])
        ledger_rows = _repair_ledger_records(
            root, con, drafts["ledger"], tests, db_checks, publication, validation,
            input_rows, exception_rows, a17, audit["issue_by_id"])
        related_rows = _related_repair_records(
            root, con, supplemental["related"], tests, publication,
            additional_input_rows,
            snapshot_evidence={
                "snapshots": snapshots,
                "redundant_input_alias": redundant_input_alias,
            })
        _check_operational_placeholders(
            list(ledger_rows) + list(related_rows), exception_rows,
            list(input_rows) + list(additional_input_rows))
        _, ledger_ids = _unique_ids(ledger_rows, "issue_id", LEDGER_ROWS, "final repair ledger")
        _, exception_ids = _unique_ids(
            exception_rows, "original_exception_id", EXCEPTION_ROWS,
            "final exception dispositions")
        _, input_ids = _unique_ids(input_rows, "input_id", INPUT_ROWS, "final input inventory")
        _, related_ids = _unique_ids(
            related_rows, "issue_id", RELATED_FINDING_ROWS, "related repair findings")
        _, additional_input_ids = _unique_ids(
            additional_input_rows, "input_id", ADDITIONAL_INPUT_ROWS,
            "additional input inventory")
        if ledger_ids != drafts["ids"]["repair ledger"] \
                or exception_ids != drafts["ids"]["exception ledger"] \
                or input_ids != drafts["ids"]["input inventory"] \
                or related_ids != supplemental["ids"]["related findings"] \
                or additional_input_ids != supplemental["ids"]["additional inputs"]:
            raise FinalRecordsRefused("final record population changed during generation")

        baseline_coverage = audit["coverage"]
        field_outcomes = {str(r[0]): int(r[1]) for r in con.execute(
            "SELECT outcome,COUNT(*) FROM field_status GROUP BY outcome ORDER BY outcome")}
        all_ledger_rows = sorted(
            list(ledger_rows) + list(related_rows), key=lambda row: row["issue_id"])
        ledger_doc = {
            "schema": "ferc-final-repair-ledger-v2", "draft": False,
            "state": "build_a_candidate", "generated_at": generated_at,
            "closure_policy": ("Accepted-at-Build-A is not release closure; later archive, Build B, "
                               "package parity and promotion receipts remain separate boundaries."),
            "population": {
                "audited_final_findings": len(ledger_rows),
                "related_findings_discovered_during_repair": len(related_rows),
                "total": len(all_ledger_rows),
            },
            "counts": {"rows": len(all_ledger_rows),
                       "disposition": dict(collections.Counter(
                           r["disposition"] for r in all_ledger_rows)),
                       "closure_status": dict(collections.Counter(
                           r["closure_status"] for r in all_ledger_rows))},
            "source_drafts": [drafts["ledger_identity"],
                              supplemental["related_identity"]],
            "rows": all_ledger_rows,
        }
        exception_doc = {
            "schema": "ferc-final-exception-dispositions-v1", "draft": False,
            "state": "build_a_candidate", "generated_at": generated_at,
            "population": EXCEPTION_ROWS, "all_unique_ids": True,
            "all_individually_dispositioned": True,
            "counts": {"rows": len(exception_rows),
                       "acceptance_result": dict(collections.Counter(
                           r["acceptance_result"] for r in exception_rows)),
                       "disposition": exception_counts},
            "source_drafts": [drafts["exception_identity"]], "rows": exception_rows,
        }
        input_doc = {
            "schema": "ferc-final-input-inventory-v1", "draft": False,
            "state": "build_a_candidate", "generated_at": generated_at,
            "population": INPUT_ROWS,
            "population_reconciliation": drafts["inputs"].get("population_reconciliation"),
            "counts": {"rows": len(input_rows), "disposition": input_counts,
                       "classification": dict(collections.Counter(
                           r["classification"] for r in input_rows))},
            "source_drafts": [drafts["input_identity"]], "rows": input_rows,
        }
        additional_input_doc = {
            "schema": "ferc-final-additional-inputs-discovered-v1", "draft": False,
            "state": "build_a_candidate", "generated_at": generated_at,
            "relationship_to_audited_inventory": (
                "separate from, and does not renumber, the final audit's 13 absent inputs"),
            "population": len(additional_input_rows),
            "counts": {"rows": len(additional_input_rows),
                       "disposition": additional_input_counts,
                       "acceptance_result": dict(collections.Counter(
                           r["acceptance_result"] for r in additional_input_rows)),
                       "classification": dict(collections.Counter(
                           r["classification"] for r in additional_input_rows))},
            "source_drafts": [supplemental["additional_input_identity"]],
            "rows": additional_input_rows,
        }
        report = _report(
            generated_at, db_checks, publication, validation, coverage,
            baseline_coverage, crosswalk, ledger_rows, exception_rows, input_rows, tests,
            related_rows, additional_input_rows)

        files: Dict[str, bytes] = {
            "FINAL_REPAIR_LEDGER.json": _canonical_json(ledger_doc),
            "FINAL_REPAIR_LEDGER.csv": _csv_bytes(all_ledger_rows, (
                "issue_id", "source_row_type", "original_issue_ids", "severity",
                "audit_claim", "affected_component_or_data", "root_cause", "targeted_test",
                "targeted_test_status", "disposition",
                "acceptance_result", "closure_status", "gates", "evidence",
                "residual_limitation")),
            "FINAL_EXCEPTION_DISPOSITIONS_34.json": _canonical_json(exception_doc),
            "FINAL_EXCEPTION_DISPOSITIONS_34.csv": _csv_bytes(exception_rows, (
                "original_exception_id", "related_original_issue_ids",
                "related_final_finding_ids", "occurrence_or_obligation", "audit_claim", "root_cause",
                "action_taken", "current_output", "disposition", "acceptance_result",
                "obligation_effect", "residual_limitation", "evidence")),
            "FINAL_INPUT_INVENTORY_13.json": _canonical_json(input_doc),
            "FINAL_INPUT_INVENTORY_13.csv": _csv_bytes(input_rows, (
                "input_id", "accession_number", "adapter", "source_system",
                "classification", "classification_basis", "audit_claim", "entities", "consumers",
                "capture", "cache_objects", "build_a_application", "disposition",
                "acceptance_result", "residual_limitation", "evidence")),
            "FINAL_ADDITIONAL_INPUTS_DISCOVERED.json": _canonical_json(
                additional_input_doc),
            "FINAL_ADDITIONAL_INPUTS_DISCOVERED.csv": _csv_bytes(
                additional_input_rows, (
                    "input_id", "input_kind", "docket", "accession_number",
                    "adapter", "source_system",
                    "classification", "classification_basis", "audit_claim", "entities",
                    "consumers", "capture", "cache_objects", "build_a_application",
                    "disposition", "acceptance_result", "residual_limitation", "evidence")),
            "INTEGRATED_REPAIR_REPORT.md": report,
        }
        record_identities = {name: {"bytes": len(raw), "sha256": _sha(raw)}
                             for name, raw in sorted(files.items())}
        evidence_index = {
            "schema": "ferc-final-test-evidence-index-v1", "draft": False,
            "state": "build_a_candidate", "generated_at": generated_at,
            "status": "PASS_WITH_EXPLICIT_LIMITS" if (
                any(r["acceptance_result"] != "accepted" for r in exception_rows)
                or any(r["acceptance_result"] != "accepted_for_candidate"
                       for r in input_rows)
                or any(r["acceptance_result"] != "accepted_at_build_a_boundary"
                       for r in related_rows)
                or _additional_inputs_have_limits(additional_input_rows)
                or validation["summary"]["SKIPPED"])
                else "PASS",
            "boundary": ("Build A database/export/self-test evidence only; not a Build B, "
                         "archive-integrity or local-promotion receipt."),
            "audit_inputs": audit["identities"],
            "draft_inputs": {k: drafts[k] for k in
                             ("ledger_identity", "exception_identity", "input_identity")},
            "draft_source_identity_checks": drafts["source_identities"],
            "supplemental_draft_inputs": {
                "related_findings": supplemental["related_identity"],
                "additional_inputs": supplemental["additional_input_identity"],
            },
            "supplemental_source_identity_checks": supplemental["source_identities"],
            "declared_run_plan": run_plan,
            "database": db_checks, "publication": publication,
            "exports": {"row_counts": exports["row_counts"],
                        "file_identities": publication["files"]},
            "coverage": {"current": coverage,
                         "audited_frozen_baseline": baseline_coverage.get("coverage_headline"),
                         "requirement_counts": dict(collections.Counter(
                             str(r[0]) for r in con.execute(
                                 "SELECT requirement FROM coverage_expected")))},
            "field_reconciliation": {**crosswalk, "outcomes": field_outcomes},
            "annotations": annotations, "a17_reviewed_image_gate": a17,
            "integrated_validation": validation, "test_runs": tests,
            "exception_counts": exception_doc["counts"],
            "input_counts": input_doc["counts"],
            "related_finding_counts": {
                "rows": len(related_rows),
                "acceptance_result": dict(collections.Counter(
                    r["acceptance_result"] for r in related_rows)),
            },
            "additional_input_counts": additional_input_doc["counts"],
            "record_identities_excluding_this_index": record_identities,
            "limitations": [
                "This is implementation-owner self-testing, not an independent audit.",
                "Archive construction, clean-extraction Build B, package parity and local "
                "promotion occur after this evidence boundary.",
                "A17 claims bounded offline local Vision OCR only for two exact cached source/"
                "render identities, with required agreement to independent manual review.",
            ],
        }
        files["FINAL_TEST_EVIDENCE_INDEX.json"] = _canonical_json(evidence_index)
    finally:
        con.close()

    return _atomic_publish(output_dir, files, generated_at)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="isolated candidate root")
    parser.add_argument("--db", required=True, help="closed/checkpointed Build A SQLite database")
    parser.add_argument("--output-dir", required=True,
                        help="candidate directory that receives final records")
    parser.add_argument("--logs-dir", required=True,
                        help="root of complete logs named by the test-run manifest")
    parser.add_argument("--audit-dir", required=True,
                        help="completed independent audit output directory")
    parser.add_argument("--test-run-manifest", required=True,
                        help="hash/count/command manifest for actual complete test logs")
    parser.add_argument("--validation", required=True,
                        help="Build A verification/validation_results.json")
    parser.add_argument("--publication-receipt", required=True,
                        help="Build A publication_receipt.json")
    parser.add_argument("--ledger-draft", required=True,
                        help="64-row implementation repair-ledger draft")
    parser.add_argument("--exception-draft", required=True,
                        help="34-row exception-disposition draft")
    parser.add_argument("--input-draft", required=True,
                        help="13-row recovered-input inventory draft")
    parser.add_argument("--related-findings-draft", required=True,
                        help="repair-time findings kept separate from the audited 64 rows")
    parser.add_argument("--additional-inputs-draft", required=True,
                        help="inputs discovered during repair, separate from the audited 13")
    parser.add_argument("--generated-at",
                        help="timezone-aware deterministic record time; defaults to run-plan built_at")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = build(args)
    except (FinalRecordsRefused, sqlite3.Error, OSError) as exc:
        print(f"FINAL RECORDS REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
