"""
549D semantics (A16, A04 liquids, A08 share, Page 700) -- w3-financial acceptance tests.

Rule 4: every fix ships with a NEGATIVE test that fails when the defect is
reintroduced. Each test below therefore does two things -- asserts the correct
behaviour, and then MUTATES the input to reintroduce the exact defect the audit
found and asserts that the mutation is REJECTED. A test that passes because the
mutated record became acceptable is a failed test.

Authored by w3-financial and adopted into `tests/` by w6-acceptance, which owns
this directory (REPAIR_CONTRACT Rule 1). The six-record historical exception
population is now an explicit release-relative, SHA-256-verified fixture; no
personal audit path is searched and absence is a failure rather than a skip.
The semantic assertions and synthetic mutations remain unchanged.

Run:
    cd outputs/repair_all_regimes && python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from adapters import form549d as f549                                      # noqa: E402
from adapters import oil_index                                            # noqa: E402
from acceptance.harness import fixture_json                               # noqa: E402
from ferclib.status import VersionStatus                                  # noqa: E402


def row(**kw):
    """A 549D contract row with enough columns for the probes to work."""
    base = {
        "Form549D_ID": "18901", "Sequence_Number": "1",
        "Contract_Number": "TRN00593", "Service_Type": "Transportation",
        "Character_of_Service": "Firm", "Affiliate_Status": "No",
        "Shipper_Name": "ACME GAS", "Filer_Proprietary_Shipper_ID": "S1",
        "Total_Rev": "1000", "Usage_BU": "500",
        "Service_Rate_Schedule": "FT-1", "Rate_Docket": "RP11-1",
        "Receipt_Point_Name": "P1", "Delivery_Point_Name": "P2",
        "Contract_Begin": "01/01/2024", "Contract_End": "12/31/2030",
    }
    base.update(kw)
    return base


# =====================================================================
# A16 -- storage revenue may never reach a transportation total
# =====================================================================

class StorageCannotLeakIntoTransport(unittest.TestCase):

    def test_transport_selection_excludes_storage_and_parking(self):
        rows = [row(Sequence_Number="1", Service_Type="Transportation"),
                row(Sequence_Number="2", Service_Type="Storage", Total_Rev="99999"),
                row(Sequence_Number="3", Service_Type="Parking/Lending"),
                row(Sequence_Number="4", Service_Type="")]
        sel = f549.transport_rows(rows)
        self.assertEqual(len(sel), 1)
        self.assertEqual(f549._s(sel[0]["Sequence_Number"]), "1")
        # the correct selection passes the invariant
        f549.assert_transport_excludes_storage(sel)

    def test_NEGATIVE_storage_row_in_transport_selection_is_rejected(self):
        """Reintroduce the defect: let a Storage row into the transport set."""
        good = [row(Sequence_Number="1")]
        mutated = good + [row(Sequence_Number="2", Service_Type="Storage",
                              Total_Rev="13346064")]
        with self.assertRaises(AssertionError) as cm:
            f549.assert_transport_excludes_storage(mutated)
        self.assertIn("excluding revenues from storage services", str(cm.exception))

    def test_NEGATIVE_case_variant_storage_still_rejected(self):
        """'STORAGE' and 'storage' normalise to the same service. No bypass."""
        for variant in ("STORAGE", "storage", "  Storage  "):
            with self.assertRaises(AssertionError):
                f549.assert_transport_excludes_storage(
                    [row(Service_Type=variant)])

    def test_storage_exclusion_is_definitional_not_discretionary(self):
        """The FERC source that settles it must remain quoted in the module."""
        self.assertIn("exclude storage revenues from the report",
                      f549.ORDER_735A["p22_quote"])
        self.assertIn("excluding revenues from storage services",
                      f549.ORDER_735A["regulation_as_revised"])
        self.assertIn("TRANSPORTATION service".lower(),
                      f549.FIELD_72["definition_quote"].lower())
        self.assertEqual(f549.FIELD_72["source_column"], "Total_Rev")
        # and the citation must be the real one, not a bare "paragraph 22"
        self.assertEqual(f549.ORDER_735A["citation"], "133 FERC para 61,216")
        self.assertEqual(f549.ORDER_735A["docket"], "RM09-2-001")

    def test_storage_residue_is_not_a_storage_revenue_total(self):
        """Form 549D stopped collecting per-customer storage revenue."""
        self.assertIn("284.126(c)(5)",
                      f549.ORDER_735A["storage_revenue_lives_here_instead"])


# =====================================================================
# A16 -- Q4 usage availability does not establish annual-revenue coverage
# =====================================================================

class UsageDoesNotEstablishRevenue(unittest.TestCase):

    def test_revenue_with_no_q4_usage_is_still_legitimate(self):
        """FERC requires annual revenue even when Q4 carries no volumes."""
        rev_rows = [row(Total_Rev="2543381", Usage_BU="0")]
        f549.assert_revenue_not_inferred_from_usage(rev_rows, [], published=True)

    def test_NEGATIVE_publishing_revenue_from_usage_alone_is_rejected(self):
        """Reintroduce the defect: no revenue-bearing row, but usage exists."""
        usage_only = [row(Total_Rev="0", Usage_BU="11798820")]
        with self.assertRaises(AssertionError) as cm:
            f549.assert_revenue_not_inferred_from_usage(
                [], usage_only, published=True)
        self.assertIn("Q4 usage is not evidence of annual revenue",
                      str(cm.exception))

    def test_ferc_instruction_is_quoted_verbatim(self):
        q = f549.FIELD_72["annual_revenue_is_independent_of_q4_volumes"]
        self.assertIn("you must enter annual revenues", q)
        self.assertIn("NEITHER necessary NOR sufficient", q)


class DatasetVersionIsNotOccurrenceDate(unittest.TestCase):

    def test_filing_record_preserves_version_timestamp_outside_snapshot_date(self):
        stamp = "2026-09-07T06:36:03.232Z"
        head = {"Form549D_ID": "SYN-549D-1",
                "Filing_Year__DASH__Quarter": "2025-Q2",
                "Submission_Date": "08/15/2025",
                "Original__SLASH__Resubmission": "Original",
                "Performed_Transport_This_Quarter": "Y"}
        tables = {"snapshot": stamp,
                  "resolution": {"respondent_id": 28, "contract_id": 29},
                  "source_url": "https://api.data.ferc.gov/redacted"}
        filing = f549._filing_record(
            None, {"entity_key": "C000001"}, head, [], tables, True, 1)
        self.assertIsNone(filing["snapshot_date"])
        self.assertEqual(stamp, filing["retrieved_at"])
        self.assertIn(stamp, filing["taxonomy_version"])
        self.assertEqual("2025-04-01", filing["period_start"])
        self.assertEqual("2025-06-30", filing["period_end"])
        self.assertEqual("2025-08-15", filing["submitted_on"])


# =====================================================================
# A16 -- ambiguous service/billing populations stay gated
# =====================================================================

class AmbiguityGatesHold(unittest.TestCase):

    def _ambiguous_pair(self, v1, v2):
        """Two rows under one contract differing ONLY in shipper identity."""
        return [row(Sequence_Number="1", Shipper_Name="ALPHA",
                    Filer_Proprietary_Shipper_ID="S1", Total_Rev=v1),
                row(Sequence_Number="2", Shipper_Name="BETA",
                    Filer_Proprietary_Shipper_ID="S2", Total_Rev=v2)]

    def test_ambiguous_group_is_detected(self):
        g = f549.probe_grain(self._ambiguous_pair("269309", "269309"))
        self.assertEqual(g["grain_class"], "ambiguous_contract_group")
        self.assertFalse(g["resolved"])

    def test_EQUAL_AMOUNTS_BLOCK_rather_than_collapse(self):
        """The forbidden rule is 'same amount means duplicate'. Assert the
        OPPOSITE direction: two identical non-zero values must BLOCK."""
        rows = self._ambiguous_pair("269309", "269309")
        g = f549.probe_grain(rows)
        scen = f549.grain_scenarios(g, rows, "Total_Rev")
        self.assertFalse(scen["treatments_coincide"],
                         "identical amounts must still block the total")
        blocked = (not g["resolved"]) and (not scen["treatments_coincide"])
        self.assertTrue(blocked)

    def test_small_divergence_does_not_unblock(self):
        """A tiny percentage gap is not permission to pick a reading."""
        rows = self._ambiguous_pair("50777188", "269309")   # ~0.5% of the total
        g = f549.probe_grain(rows)
        scen = f549.grain_scenarios(g, rows, "Total_Rev")
        a = scen["scenario_A_one_value_per_ambiguous_contract"]
        b = scen["scenario_B_row_sum"]
        divergence = abs(b - a) / b
        self.assertLess(divergence, 0.01, "precondition: a small divergence")
        self.assertFalse(scen["treatments_coincide"],
                         "a small divergence must NOT resolve the grain")

    def test_NEGATIVE_a_divergence_threshold_would_ungate_and_must_fail(self):
        """Reintroduce the defect as a mutation of the DECISION RULE.

        This is the mutation the audit warned about: 'a small aggregation-method
        divergence does not establish the correct method'. If anyone replaces
        the structural test with a tolerance, these gated cases silently open.
        """
        rows = self._ambiguous_pair("50777188", "269309")
        g = f549.probe_grain(rows)
        scen = f549.grain_scenarios(g, rows, "Total_Rev")

        def mutated_gate(scenario, tolerance=0.01):
            a = scenario["scenario_A_one_value_per_ambiguous_contract"]
            b = scenario["scenario_B_row_sum"]
            return abs(b - a) / b > tolerance      # "close enough -> publish"

        self.assertFalse(mutated_gate(scen),
                         "precondition: the threshold rule would ungate this")
        # The shipped rule must NOT agree with the threshold rule.
        shipped_blocks = not scen["treatments_coincide"]
        self.assertTrue(shipped_blocks)
        self.assertNotEqual(
            shipped_blocks, mutated_gate(scen),
            "a divergence threshold reintroduces the A16 defect: it ungates a "
            "population the structural test correctly blocks")

    def test_treatments_coincide_only_when_structurally_impossible_to_differ(self):
        """One non-zero row in the ambiguous group -> readings cannot differ."""
        rows = self._ambiguous_pair("269309", "0")
        g = f549.probe_grain(rows)
        scen = f549.grain_scenarios(g, rows, "Total_Rev")
        self.assertTrue(scen["treatments_coincide"])

    def test_service_defining_difference_resolves_the_group(self):
        rows = [row(Sequence_Number="1", Service_Rate_Schedule="FT-1"),
                row(Sequence_Number="2", Service_Rate_Schedule="IT-2")]
        g = f549.probe_grain(rows)
        self.assertTrue(g["resolved"])
        self.assertEqual(g["grain_class"], "contract_x_service_leg")

    def test_the_four_audited_gates_are_still_reachable(self):
        """The audit recorded 4 gated observations; the gate logic that produced
        them must still block on their shape (2 revenue-bearing rows in one
        ambiguous group)."""
        for col, v1, v2 in (("Total_Rev", "269309", "269309"),      # C000826 FY2025
                            ("Total_Rev", "146924", "440340"),      # C000588 FY2025
                            ("Usage_BU", "635466", "267208"),       # C000434 2024Q2
                            ("Usage_BU", "1955882", "33119")):      # C000588 2025Q4
            rows = [row(Sequence_Number="1", Shipper_Name="A", **{col: v1}),
                    row(Sequence_Number="2", Shipper_Name="B", **{col: v2})]
            g = f549.probe_grain(rows)
            scen = f549.grain_scenarios(g, rows, col)
            self.assertFalse(g["resolved"], f"{col} {v1}/{v2} must stay ambiguous")
            self.assertFalse(scen["treatments_coincide"],
                             f"{col} {v1}/{v2} must stay gated")


# =====================================================================
# A16 -- the two audited populations are labelled, and are not targets
# =====================================================================

class PopulationsAreLabelledNotTargeted(unittest.TestCase):

    def test_both_populations_carry_window_and_occurrence_rule(self):
        hist = f549.AUDITED_POPULATIONS["historical_all_occurrences"]
        incl = f549.AUDITED_POPULATIONS["included_universe_2024_2025"]
        self.assertEqual(hist["rows"], 544)
        self.assertEqual(hist["revenue"], "237017275")
        self.assertEqual(incl["rows"], 179)
        self.assertEqual(incl["revenue"], "39299027")
        for p in (hist, incl):
            self.assertFalse(p["is_a_target"])
            self.assertTrue(p["window"] and p["definition"])

    def test_agreement_is_reported_not_forced(self):
        same = f549.compare_storage_population(544, "237017275")
        self.assertTrue(same["agrees"])
        diff = f549.compare_storage_population(540, "236000000")
        self.assertFalse(diff["agrees"])
        # both figures survive; nothing is coerced
        self.assertEqual(diff["audited_baseline_rows"], 544)
        self.assertEqual(diff["recomputed_rows"], 540)
        self.assertFalse(diff["is_a_target"])
        self.assertIn("NOT adjusted to reproduce the baseline", diff["note"])


# =====================================================================
# A16 -- blocker reconciliation: 4 filer-years + 1 consolidated policy record
# =====================================================================

class BlockerReconciliation(unittest.TestCase):

    def _exceptions(self):
        fixture = fixture_json("form549d_historical_semantic_exceptions.json")
        rows = fixture.get("rows") or []
        self.assertEqual(fixture.get("provenance", {}).get("source_register_rows"), 34)
        self.assertEqual(len(rows), 6,
                         "the exact six Form 549D semantic records are required; "
                         "missing evidence is a failure, never a skip")
        return rows

    def test_the_six_blockers_are_4_filer_years_plus_2_policy_duplicates(self):
        sem = [e for e in self._exceptions()
               if e["adapter"] == "form549d" and e["kind"] == "semantic"]
        self.assertEqual(len(sem), 6)
        filer_years = [e for e in sem if ":" in e["scope"]]
        policy = [e for e in sem if e["scope"] == f549.POLICY_BLOCKER_SCOPE]
        self.assertEqual(len(filer_years), 4)
        self.assertEqual(len(policy), 2)
        self.assertEqual(sorted(e["scope"] for e in filer_years),
                         ["C000826:2024", "C000826:2025",
                          "C001773:2024", "C001773:2025"])

    def test_the_two_policy_duplicates_differ_only_in_summary_text(self):
        """Root cause: blocker_id hashes the SUMMARY, and a volatile dollar
        figure lived there."""
        policy = [e for e in self._exceptions()
                  if e["adapter"] == "form549d"
                  and e["scope"] == f549.POLICY_BLOCKER_SCOPE]
        self.assertEqual(len({e["blocker_id"] for e in policy}), 2)
        self.assertEqual(len({e["summary"] for e in policy}), 2,
                         "the summaries must differ -- that is why two ids exist")
        self.assertEqual(sorted(e["blocker_id"] for e in policy),
                         sorted(f549.SUPERSEDED_POLICY_BLOCKERS))

    def test_NEGATIVE_a_volatile_figure_in_the_summary_reintroduces_the_split(self):
        """Mutation: put a changing number back into the identity-bearing text."""
        import hashlib

        def bid(scope, summary):
            return "blk-" + hashlib.sha256(
                f"form549d|{scope}|{summary}".encode()).hexdigest()[:16]

        stable = f549.POLICY_BLOCKER_SUMMARY
        self.assertEqual(bid(f549.POLICY_BLOCKER_SCOPE, stable),
                         bid(f549.POLICY_BLOCKER_SCOPE, stable),
                         "the shipped summary must be stable across runs")
        mutated_a = stable + "; table-wide 544 Q4 storage rows carry $237,017,275"
        mutated_b = stable + "; table-wide 545 Q4 storage rows carry $237,020,000"
        self.assertNotEqual(bid(f549.POLICY_BLOCKER_SCOPE, mutated_a),
                            bid(f549.POLICY_BLOCKER_SCOPE, mutated_b),
                            "embedding a volatile figure in the summary splits "
                            "one policy question into two blockers -- the A16 defect")
        # and the shipped constant must not contain a dollar figure at all
        self.assertNotIn("$", stable)
        self.assertNotIn("237,017,275", stable)

    def test_filer_year_scopes_are_never_consolidated(self):
        """Policy records consolidate; SOURCE rows never do."""
        self.assertEqual(len(set(f549.SUPERSEDED_POLICY_BLOCKERS)), 2)
        for b in self._exceptions():
            if b["adapter"] == "form549d" and ":" in b.get("scope", ""):
                self.assertNotIn(b["blocker_id"], f549.SUPERSEDED_POLICY_BLOCKERS)


# =====================================================================
# A08 -- a zero must have a supported source population
# =====================================================================

class ZeroNeedsAPopulation(unittest.TestCase):

    def test_measured_zero_is_distinguishable_from_no_data(self):
        considered = [row(Character_of_Service="Interruptible") for _ in range(7)]
        pop, edge = f549._population(
            "obs-x", filing_ids=["18901"], inclusion="Firm rows",
            exclusion="Interruptible and UNKNOWN rows",
            members=[], candidates=len(considered),
            aggregate_value="0.0", aggregate_unit="percent")
        self.assertEqual(pop["row_count"], 0)
        self.assertEqual(pop["candidate_count"], 7)
        self.assertEqual(pop["excluded_count"], 7)
        self.assertIn("none satisfied the inclusion rule", pop["empty_reason"])
        self.assertEqual(edge["input_role"], "population")
        if f549._PERSIST_POPULATIONS:
            self.assertEqual(edge["input_population_id"], pop["population_id"])
        else:
            # while no writer exists, the population travels INLINE so the edge
            # never points at a row that does not exist
            self.assertIsNone(edge["input_population_id"])
            self.assertIn("candidate_count", edge["input_value"])

    def test_NEGATIVE_a_population_edge_never_points_at_a_missing_row(self):
        """A18's defect in miniature: a reference with no referent."""
        _, edge = f549._population(
            "obs-w", filing_ids=["18901"], inclusion="i", exclusion="e",
            members=[row()], candidates=1)
        if not f549._PERSIST_POPULATIONS:
            self.assertIsNone(edge.get("input_population_id"),
                              "an unwired writer must not leave a dangling "
                              "input_population_id")

    def test_NEGATIVE_every_edge_carries_the_population_key_for_upsert(self):
        """Staging._upsert derives its columns from rows[0] alone, so a key
        missing from the FIRST edge is silently dropped from ALL of them. That
        is how 41 population FKs went NULL while the population rows wrote
        fine. Every edge must therefore carry the key."""
        e = f549._edge("obs-q", 1, "addend", "+", "18901", "k", "Total_Rev",
                       "a..b", "1", "iso4217:USD", "original")
        self.assertIn("input_population_id", e,
                      "a plain edge missing this key, sorted first, strips the "
                      "FK from every population edge in the batch")
        _, pedge = f549._population("obs-q", filing_ids=["18901"], inclusion="i",
                                    exclusion="e", members=[row()], candidates=1)
        self.assertEqual(set(e) - set(pedge), set(),
                         "plain and population edges must be column-homogeneous")
        self.assertEqual(set(pedge) - set(e), set())

    def test_NEGATIVE_no_retrieved_data_is_not_reported_as_a_measured_zero(self):
        """Reintroduce the defect: nothing retrieved, published as 0."""
        pop, _ = f549._population(
            "obs-y", filing_ids=[], inclusion="Firm rows",
            exclusion="none", members=[], candidates=0,
            aggregate_value="0.0", aggregate_unit="percent")
        self.assertEqual(pop["candidate_count"], 0)
        self.assertIn("ABSENCE OF DATA", pop["empty_reason"])
        self.assertNotIn("none satisfied", pop["empty_reason"])

    def test_inclusion_and_exclusion_rules_are_both_required(self):
        pop, _ = f549._population(
            "obs-z", filing_ids=["18901"], inclusion="Transportation rows",
            exclusion="Storage, Parking/Lending, Other, blank",
            members=[row()], candidates=3, aggregate_value="1000",
            aggregate_unit="iso4217:USD")
        self.assertTrue(pop["inclusion_rule"])
        self.assertTrue(pop["exclusion_rule"])
        self.assertIn("Storage", pop["exclusion_rule"])

    def test_NEGATIVE_member_digest_changes_if_a_storage_row_is_smuggled_in(self):
        """The digest is what lets w6-acceptance prove no leak occurred."""
        clean = [row(Sequence_Number="1"), row(Sequence_Number="2")]
        leaked = clean + [row(Sequence_Number="3", Service_Type="Storage")]
        a, _ = f549._population("obs-d", filing_ids=["18901"], inclusion="i",
                                exclusion="e", members=clean, candidates=3)
        b, _ = f549._population("obs-d", filing_ids=["18901"], inclusion="i",
                                exclusion="e", members=leaked, candidates=3)
        self.assertNotEqual(a["member_digest"], b["member_digest"])

    def test_population_is_deterministic(self):
        rows = [row(Sequence_Number="2"), row(Sequence_Number="1")]
        a, _ = f549._population("obs-e", filing_ids=["18901"], inclusion="i",
                                exclusion="e", members=rows, candidates=2)
        b, _ = f549._population("obs-e", filing_ids=["18901"], inclusion="i",
                                exclusion="e", members=list(reversed(rows)),
                                candidates=2)
        self.assertEqual(a["member_digest"], b["member_digest"])
        self.assertEqual(a["population_id"], b["population_id"])


# =====================================================================
# A16 -- truthful failure classification. Never "FERC outage".
# =====================================================================

class FailuresAreClassifiedTruthfully(unittest.TestCase):

    def test_missing_credential_is_local_configuration(self):
        from ferclib.http import FetchError
        exc = FetchError("<FERC_API_KEY>",
                         "FERC_API_KEY is not set in the environment or .env")
        c = f549.classify_retrieval_failure(exc)
        self.assertEqual(c["classification"],
                         oil_index.CLASS_MISSING_CREDENTIAL)
        self.assertFalse(c["is_ferc_condition"])
        self.assertEqual(c["kind"], "configuration")
        self.assertIn("NOT a FERC outage", c["explicitly_not"])

    def test_NEGATIVE_a_missing_credential_is_never_called_a_source_failure(self):
        """This is precisely how the two 549D blockers were mislabelled."""
        from ferclib.http import FetchError
        exc = FetchError("<FERC_API_KEY>",
                         "FERC_API_KEY is not set in the environment or .env")
        c = f549.classify_retrieval_failure(exc)
        self.assertNotEqual(c["kind"], "source")
        self.assertNotEqual(c["classification"],
                            oil_index.CLASS_FERC_UNAVAILABLE)
        for word in ("outage", "unavailable", "down"):
            self.assertNotIn(word, c["summary"].lower())

    def test_unattempted_request_is_not_evidence_about_ferc(self):
        c = oil_index.classify_failure("precondition failed",
                                       request_attempted=False)
        self.assertEqual(c["classification"], oil_index.CLASS_NOT_ATTEMPTED)
        self.assertFalse(c["is_ferc_condition"])
        self.assertIn("carries no information about the source",
                      c["explicitly_not"])

    def test_publisher_block_is_not_a_ferc_outage(self):
        c = oil_index.classify_failure("HTTP 403 Forbidden",
                                       request_attempted=True)
        self.assertEqual(c["classification"], oil_index.CLASS_ACCESS_BLOCKED)
        self.assertFalse(c["is_ferc_condition"])

    def test_a_real_ferc_error_IS_reported_as_one(self):
        """The classifier must not be so cautious that it never blames FERC."""
        c = oil_index.classify_failure("HTTP 503 from api.data.ferc.gov",
                                       request_attempted=True)
        self.assertEqual(c["classification"], oil_index.CLASS_FERC_UNAVAILABLE)
        self.assertTrue(c["is_ferc_condition"])
        self.assertEqual(c["kind"], "source")

    def test_credential_free_replay_is_distinct_from_a_live_credential(self):
        c = oil_index.classify_failure("OfflineCacheMiss: cache miss for url",
                                       request_attempted=False, offline=True)
        self.assertEqual(c["classification"], oil_index.CLASS_CACHE_MISS)
        self.assertFalse(c["is_ferc_condition"])
        self.assertIn("FERC was never contacted", c["explicitly_not"])

    def test_retrieval_attempts_are_recorded_honestly(self):
        blocked = [a for a in oil_index.RETRIEVAL_ATTEMPTS
                   if a["classification"] == oil_index.CLASS_ACCESS_BLOCKED]
        self.assertTrue(blocked)
        for a in blocked:
            self.assertTrue(a["attempted"])
            self.assertIn("NOT a FERC outage", a["explicitly_not"])


# =====================================================================
# Page 700 safeguard
# =====================================================================

class Page700IsNotRealisedReturn(unittest.TestCase):

    def _registry(self):
        from ferclib import registry
        return {m.id: m for m in registry.REGISTRY}

    def test_page_700_scope_differs_from_whole_entity_form_6(self):
        reg = self._registry()
        p700 = [m for m in reg.values() if m.id.startswith("p700_")]
        self.assertTrue(p700, "no Page 700 metrics in the registry")
        whole = [m for m in reg.values()
                 if m.adapter == "liquids_xbrl" and not m.id.startswith("p700_")
                 and m.scope]
        p700_scopes = {m.scope for m in p700}
        whole_scopes = {m.scope for m in whole}
        self.assertFalse(p700_scopes & whole_scopes,
                         "Page 700 and whole-entity Form 6 must never share a "
                         "scope string, or a ratio could be built across them")

    def test_NEGATIVE_a_page700_over_form6_ratio_must_be_refused(self):
        """Scope is part of the value; mixing the panels is the defect.

        Exercises the shared engine's real refusal branch rather than asserting
        that the scopes merely differ. A ratio of a Page 700 return component
        over whole-entity Form 6 capital would look like ROIC and would be
        entirely fictitious.
        """
        from ferclib import registry, xbrl_adapter
        reg = self._registry()
        p700 = next(m for m in reg.values() if m.id.startswith("p700_"))
        whole = next(m for m in reg.values()
                     if m.adapter == "liquids_xbrl"
                     and not m.id.startswith("p700_") and m.scope
                     and m.scope != p700.scope)
        self.assertNotEqual(p700.scope, whole.scope)
        # units agreeing must NOT be enough to license the ratio
        self.assertTrue(hasattr(registry, "units_compatible"))
        src = pathlib.Path(xbrl_adapter.__file__).read_text()
        self.assertIn("SCOPE_INCOMPATIBLE", src.replace("Validation.", ""))
        self.assertIn("the ratio is refused", src)
        self.assertIn('num["scope"] != den["scope"]', src,
                      "the engine must compare scope before building a ratio")

    def test_return_components_are_cost_of_service_inputs(self):
        """Return / WACC / rate base are ALLOWANCES inside a cost-of-service
        computation. They are not realised ROIC and not an approved return."""
        reg = self._registry()
        wanted = [m for m in reg.values()
                  if m.id.startswith("p700_")
                  and any(k in m.id for k in ("return", "wacc", "rate_base",
                                              "capital_structure"))]
        self.assertTrue(wanted, "expected Page 700 return/WACC/rate-base metrics")
        for m in wanted:
            text = " ".join(filter(None, [m.meaning, m.quality_gate, m.scope])).lower()
            self.assertTrue(
                any(p in text for p in ("allowance", "cost-of-service",
                                        "cost of service", "not an achieved",
                                        "not a realised", "not realised",
                                        "screen", "as-filed", "not approved",
                                        "not an approved")),
                f"{m.id} must say it is a cost-of-service input, not an "
                f"achieved or approved return. Got: {text[:200]}")

    def test_adapter_module_states_the_safeguard(self):
        from adapters import liquids_xbrl
        doc = (liquids_xbrl.__doc__ or "").lower()
        self.assertIn("not an achieved return", doc)
        self.assertIn("allowance", doc)
        self.assertIn("never be mixed with whole-entity", doc)


# =====================================================================
# A04 (liquids half) -- the adapter must not re-scale or re-label
# =====================================================================

class LiquidsAdapterDoesNotRescale(unittest.TestCase):

    def test_liquids_adapter_is_configuration_only(self):
        """The integrator owns the unit contract; this adapter must not touch
        a value or a unit after the shared engine has set it."""
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "adapters" / "liquids_xbrl.py").read_text()
        for forbidden in ("* 100", "*100", "/ 100", "/100",
                          'unit =', 'unit=', "canonical_unit",
                          "value_num =", "value_text ="):
            self.assertNotIn(
                forbidden, src,
                f"adapters/liquids_xbrl.py must not contain {forbidden!r}: "
                "re-scaling or re-labelling after the shared engine would "
                "double-apply the A04 unit contract")

    def test_no_computation_lives_in_the_liquids_adapter(self):
        from adapters import liquids_xbrl
        fns = [n for n in dir(liquids_xbrl)
               if callable(getattr(liquids_xbrl, n)) and not n.startswith("_")]
        self.assertEqual(sorted(f for f in fns if f in
                                ("retrieve", "freeze_expected", "canonicalise")),
                         ["canonicalise", "freeze_expected", "retrieve"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
