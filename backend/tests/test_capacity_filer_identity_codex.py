#!/usr/bin/env python3
"""Capacity filer ownership and population-identity regressions."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from adapters import capacity  # noqa: E402


class CapacityFilerDescriptionBoundary(unittest.TestCase):
    def test_mountainwest_and_overthrust_are_not_interchangeable(self):
        mwp = "MountainWest Pipeline, LLC"
        mwo = "MountainWest Overthrust Pipeline, LLC"
        own_mwp = ("Annual Peak Day Capacity Report of MountainWest Pipeline, LLC "
                   "for 2025.")
        own_mwo = ("Annual Peak Day Capacity Report of MountainWest Overthrust "
                   "Pipeline, LLC for 2025.")
        self.assertTrue(capacity._description_names_filer(mwp, own_mwp))
        self.assertTrue(capacity._description_names_filer(mwo, own_mwo))
        self.assertFalse(capacity._description_names_filer(mwp, own_mwo))
        self.assertFalse(capacity._description_names_filer(mwo, own_mwp))

    def test_valid_punctuation_and_revised_prefix_do_not_break_boundary(self):
        self.assertTrue(capacity._description_names_filer(
            "Tennessee Gas Pipeline Company, L.L.C.",
            "Revised Annual Peak Day Capacity Report of Tennessee Gas Pipeline "
            "Company, L.L.C. for 2025."))
        self.assertFalse(capacity._description_names_filer(
            "Tennessee Gas Pipeline Company, L.L.C.",
            "Annual Peak Day Capacity Report of Another Tennessee Pipeline for 2025."))


class CapacityPopulationIdentity(unittest.TestCase):
    @staticmethod
    def _filing(filing_id="F-1"):
        return {
            "filing_id": filing_id, "reporting_year": 2025,
            "_text_layer": "yes", "_doc": [{"page": 1}],
            "_figures": [{"kind": "table", "page": 1, "row_index": 3,
                           "is_total": False, "value_text": "100"}],
        }

    def test_population_id_binds_owner_observation_and_occurrence(self):
        filing = self._filing()
        one = capacity._total_population("C001087", filing, "obs-one")
        repeat = capacity._total_population("C001087", filing, "obs-one")
        other_entity = capacity._total_population("C001088", filing, "obs-two")
        other_observation = capacity._total_population("C001087", filing, "obs-three")
        other_filing = capacity._total_population(
            "C001087", self._filing("F-2"), "obs-one")
        self.assertEqual(one["population_id"], repeat["population_id"])
        self.assertEqual(len({one["population_id"], other_entity["population_id"],
                              other_observation["population_id"],
                              other_filing["population_id"]}), 4)
        self.assertEqual(one["observation_id"], "obs-one")
        self.assertEqual(one["filing_ids"], '["F-1"]')


if __name__ == "__main__":
    unittest.main(verbosity=2)
