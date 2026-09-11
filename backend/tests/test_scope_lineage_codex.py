"""Regression tests for legal/facility scope and rule-metadata separation."""

from __future__ import annotations

import types
import unittest

from ferclib import xbrl_adapter
from ferclib.status import Availability, Method, Origin, Validation, VersionStatus


def metric(metric_id, *, selector="filed", dependencies=(), unit="USD",
           schedule="FERC schedule test", regimes=(("Form 2", "quarter"),)):
    return types.SimpleNamespace(
        id=metric_id,
        scope="inputs must have identical regulatory scope",
        selector=selector,
        dependencies=tuple(dependencies),
        canonical_unit=unit,
        concept=metric_id,
        schedule=schedule,
        regimes=regimes,
    )


def observation(m, value, scope):
    return xbrl_adapter._obs(
        "CID", m, "FERC Form 2", "quarter", "2025-01-01", "2025-03-31", "",
        2025, "Q1", value_text=str(value), value_num=float(value), unit="USD",
        availability=Availability.PRESENT, origin=Origin.NATIVE_XBRL,
        method=Method.FILED, version_status=VersionStatus.ORIGINAL,
        validation=Validation.PASS, tax="2025", scope=scope,
    )


class ScopeLineageRegressionTests(unittest.TestCase):
    class _ScopeStaging:
        def query(self, sql, params=()):
            return [{"entity_identifier": "C000001"}]

    def test_schedule_labels_do_not_create_false_scope_mismatch(self):
        ctx = types.SimpleNamespace(staging=self._ScopeStaging())
        entity = {"entity_key": "C000001", "legal_name": "Example Pipeline"}
        filing = {"filing_id": "F1"}
        fact = {"context_id": "D2025", "explicit_dims_json": "[]",
                "typed_dims_json": "[]"}
        revenue = metric("revenue", schedule="p.114 line 2; p.300-301")
        income = metric("income", schedule="p.114")
        left = xbrl_adapter._source_scope(ctx, entity, revenue, filing, fact)
        right = xbrl_adapter._source_scope(ctx, entity, income, filing, fact)
        self.assertEqual(left, right)
        self.assertNotIn("p.114", left)

    def test_actual_dimension_and_page700_subset_remain_distinct(self):
        ctx = types.SimpleNamespace(staging=self._ScopeStaging())
        entity = {"entity_key": "C000001", "legal_name": "Example Pipeline"}
        filing = {"filing_id": "F1"}
        a = {"context_id": "A", "explicit_dims_json":
             '[{"axis_local":"FacilityAxis","member_local":"North"}]',
             "typed_dims_json": "[]"}
        b = {"context_id": "B", "explicit_dims_json":
             '[{"axis_local":"FacilityAxis","member_local":"South"}]',
             "typed_dims_json": "[]"}
        ordinary = metric("revenue")
        north = xbrl_adapter._source_scope(ctx, entity, ordinary, filing, a)
        south = xbrl_adapter._source_scope(ctx, entity, ordinary, filing, b)
        self.assertNotEqual(north, south)
        page700 = metric("p700_revenue", regimes=(("Form 6 Page 700", "annual"),))
        consolidated = {"context_id": "C", "explicit_dims_json": "[]",
                        "typed_dims_json": "[]"}
        panel = xbrl_adapter._source_scope(ctx, entity, page700, filing, consolidated)
        whole = xbrl_adapter._source_scope(ctx, entity, ordinary, filing, consolidated)
        self.assertNotEqual(panel, whole)
        self.assertIn("Page 700 interstate panel", panel)

    def test_typed_dimension_value_and_domain_are_preserved(self):
        ctx = types.SimpleNamespace(staging=self._ScopeStaging())
        entity = {"entity_key": "C001031", "legal_name": "Example Pipeline"}
        filing = {"filing_id": "F1"}
        fact = {"context_id": "T1", "explicit_dims_json": "[]",
                "typed_dims_json":
                    '[{"axis":"ferc:SystemNameAxis",'
                    '"domain":"ferc:SystemNameDomain","value":"0"}]'}
        scope = xbrl_adapter._source_scope(
            ctx, entity, metric("gas_delivered"), filing, fact)
        self.assertIn("ferc:SystemNameAxis=0", scope)
        self.assertIn("domain ferc:SystemNameDomain", scope)
        self.assertNotIn("unresolved", scope)

    def test_registry_instruction_is_not_stored_as_observation_scope(self):
        m = metric("revenue")
        actual = "FERC filing entity Example Pipeline; consolidated source context"
        obs = observation(m, 10, actual)
        self.assertEqual(obs["scope"], actual)
        self.assertEqual(obs["scope_rule"], m.scope)
        self.assertNotEqual(obs["scope"], obs["scope_rule"])

    def test_ratio_preserves_supported_common_scope(self):
        num_m, den_m = metric("income"), metric("revenue")
        ratio_m = metric(
            "margin", selector="derived_ratio", dependencies=("income", "revenue"),
            unit="percent")
        actual = "FERC filing entity Example Pipeline; consolidated source context"
        edges = []
        out = xbrl_adapter._ratios(
            None, None, "CID", {"margin": ratio_m},
            [observation(num_m, 25, actual), observation(den_m, 100, actual)], edges)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["availability"], Availability.PRESENT)
        self.assertEqual(out[0]["value_num"], 25.0)
        self.assertEqual(out[0]["unit"], "percent")
        self.assertEqual(out[0]["scope"], actual)
        self.assertEqual(len(edges), 2)

    def test_ratio_refuses_mismatched_scope_and_emits_no_lineage(self):
        num_m, den_m = metric("income"), metric("revenue")
        ratio_m = metric(
            "margin", selector="derived_ratio", dependencies=("income", "revenue"),
            unit="percent")
        edges = []
        out = xbrl_adapter._ratios(
            None, None, "CID", {"margin": ratio_m},
            [observation(num_m, 25, "facility A"),
             observation(den_m, 100, "facility B")], edges)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["availability"], Availability.NOT_APPLICABLE)
        self.assertEqual(out[0]["validation"], Validation.SCOPE_INCOMPATIBLE)
        self.assertIn("scope unresolved", out[0]["scope"])
        self.assertEqual(edges, [])

    def test_ratio_refuses_matching_but_unresolved_scope(self):
        num_m, den_m = metric("income"), metric("revenue")
        ratio_m = metric(
            "margin", selector="derived_ratio", dependencies=("income", "revenue"),
            unit="percent")
        unresolved = "FERC filing entity CID; context dimensions Axis=unresolved member"
        edges = []
        out = xbrl_adapter._ratios(
            None, None, "CID", {"margin": ratio_m},
            [observation(num_m, 25, unresolved),
             observation(den_m, 100, unresolved)], edges)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["validation"], Validation.SCOPE_INCOMPATIBLE)
        self.assertIsNone(out[0]["value_num"])
        self.assertEqual(edges, [])


if __name__ == "__main__":
    unittest.main()
