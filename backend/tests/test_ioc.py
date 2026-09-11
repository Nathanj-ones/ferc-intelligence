"""
Regressions for the Index of Customers adapter, against REAL retrieved filings.

Nothing here is mocked. The tests run the adapter's own retrieval path through
the shared content-addressed source cache, so the bytes under test are the exact
bytes FERC served, and they parse and canonicalise them for real. They write to
their own temporary staging database and never touch the shared one.

The A17 reviewed-image regression is deliberately self-contained and runs before
the cache-dependent classes: it uses a release-relative hash-pinned record of the
independent visual review to exercise the reviewed-page transcription and
canonicalisation path. It does not claim to rerender or OCR the source PDFs.

Each test pins one of the semantic mistakes this adapter exists to avoid:

  test_snapshot_date_is_not_the_report_date
  test_storage_quantity_is_a_stock_not_a_daily_rate
  test_s8_is_a_segment_endpoint_not_a_receipt_code
  test_no_p_sum_equals_twice_d_total_rule
  test_contracts_are_aggregated_to_the_shipper_before_ranking
  test_a_contract_missing_from_a_snapshot_is_not_a_termination

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from adapters import ioc                                            # noqa: E402
from ferclib.registry import BY_ADAPTER, REGISTRY                             # noqa: E402
from ferclib.http import Client, SourceCache                        # noqa: E402
from ferclib.staging import Staging                                 # noqa: E402
from ferclib.status import Availability, Validation                 # noqa: E402
from acceptance.harness import fixture_json                        # noqa: E402

TRANSCO = {"entity_key": "C000654",
           "legal_name": "Transcontinental Gas Pipe Line Company, LLC",
           "template": "interstate_gas", "parent": "Williams", "ticker": "WMB",
           "assets": [{"asset_id": "wmb-transco", "display_name": "Transco",
                       "template": "interstate_gas"}]}
TGP = {"entity_key": "C000020",
       "legal_name": "Tennessee Gas Pipeline Company, L.L.C.",
       "template": "interstate_gas", "parent": "Kinder Morgan", "ticker": "KMI",
       "assets": [{"asset_id": "kmi-tennessee-gas-pipeline",
                   "display_name": "Tennessee Gas Pipeline",
                   "template": "interstate_gas"}]}


class _Args:
    year_from, year_to = 2024, 2026


#: The replay date. IOC discovery bounds its search at `ctx.today()`, so on the
#: real clock the window silently widens every day and a test that passed in
#: September fails in October for no reason connected to the code. Pinning it to
#: the source-capture date is what makes an offline replay against the cache
#: deterministic -- the same thing the audit's harness achieved by monkeypatching
#: `ioc.dt`, now expressible through the adapter's own context contract.
REPLAY_TODAY = dt.date(2026, 9, 7)


class _Ctx:
    """The adapter contract's ctx, backed by the real client and a throwaway db."""

    def __init__(self, tmp: pathlib.Path, today: dt.date = REPLAY_TODAY):
        self.args = _Args()
        self.force = False
        self.offline = True
        self.workdir = tmp
        self._today = today
        self.cache = SourceCache(HERE / "source_cache")
        self.client = Client(self.cache, min_interval=0.3, budget=400,
                             logger=lambda level, msg: None, offline=True)
        self.staging = Staging(tmp / "test_ioc.sqlite")
        self.staging.start_run("test", {}, "test", "test")
        self.messages: list[tuple[str, str]] = []

    def today(self) -> dt.date:
        """The adapter's clock. Pinned so an offline replay is reproducible."""
        return self._today

    def seed_entity(self, entity: dict) -> None:
        """Mirror the production identity precondition in this disposable DB.

        Filing rows now have enforced entity/asset foreign keys.  The former
        real-byte fixture opened a fresh schema but never seeded those parents,
        so retrieval succeeded and every filing then rolled back at persistence.
        Keeping the parents explicit here exercises the production FK boundary
        instead of disabling it or letting a zero-case integration run appear
        to test IOC semantics.
        """
        entity_key = entity["entity_key"]
        self.staging.write_entities([{
            "entity_key": entity_key,
            "cid": entity_key if entity_key.startswith("C") else None,
            "local_key": None if entity_key.startswith("C") else entity_key,
            "legal_name": entity["legal_name"],
            "parent": entity.get("parent"),
            "ticker": entity.get("ticker"),
            "jurisdiction": entity.get("jurisdiction"),
            "note": "real-byte IOC integration fixture",
        }])
        assets = [{
            "asset_id": asset["asset_id"],
            "ticker": entity.get("ticker"),
            "display_name": asset["display_name"],
            "template": asset["template"],
            "authority": None,
            "cod_group": None,
            "group_key": None,
            "status": "operating",
            "note": "real-byte IOC integration fixture",
        } for asset in entity["assets"]]
        mappings = [{
            "asset_id": asset["asset_id"],
            "entity_key": entity_key,
            "mapping_scope": "whole_entity",
            "effective_from": None,
            "effective_to": None,
            "note": "real-byte IOC integration fixture",
        } for asset in entity["assets"]]
        ownership = ([{
            "entity_key": entity_key,
            "parent": entity["parent"],
            "ticker": entity.get("ticker"),
            "pct": None,
            "basis": "direct",
            "qualifier": None,
            "effective_from": None,
            "effective_to": None,
        }] if entity.get("parent") else [])
        self.staging.write_assets(assets, mappings, ownership, [])
        seeded = self.staging.query(
            "SELECT e.entity_key,m.asset_id FROM entities e "
            "JOIN asset_entity_map m ON m.entity_key=e.entity_key "
            "WHERE e.entity_key=? ORDER BY m.asset_id", (entity_key,))
        expected = sorted(asset["asset_id"] for asset in entity["assets"])
        if [row["asset_id"] for row in seeded] != expected:
            raise AssertionError(
                f"IOC fixture identity precondition failed for {entity_key}: {seeded}")

    def log(self, level, msg, *, adapter="", entity_cid=""):
        self.messages.append((level, msg))

    def close(self):
        self.staging.close()


_STATE: dict = {}


def _load(entity):
    """Retrieve + canonicalise once per entity for the whole module."""
    key = entity["entity_key"]
    if key in _STATE:
        return _STATE[key]
    temporary = tempfile.TemporaryDirectory(prefix=f"ioc-{key}-")
    tmp = pathlib.Path(temporary.name)
    ctx = None
    try:
        ctx = _Ctx(tmp)
        ctx.seed_entity(entity)
        filings = ioc.retrieve(ctx, entity, year_from=2024, year_to=2026)
        expected = ioc.freeze_expected(ctx, entity, filings, entity["assets"])
        ctx.staging.freeze_expected(expected)
        observations, edges = ioc.canonicalise(ctx, entity, filings, expected)
    except BaseException:
        if ctx is not None:
            ctx.close()
        temporary.cleanup()
        raise
    _STATE[key] = {"ctx": ctx, "filings": filings, "expected": expected,
                   "observations": observations, "edges": edges,
                   "temporary": temporary,
                   "canonical": sorted((f for f in filings if f["is_canonical"]),
                                       key=lambda f: f["snapshot_date"])}
    return _STATE[key]


def _obs(state, metric_id, **where):
    out = [o for o in state["observations"] if o["metric_id"] == metric_id]
    for k, v in where.items():
        out = [o for o in out if o.get(k) == v]
    return out


class HarnessIsolationTests(unittest.TestCase):
    """The real-byte integration fixture must exercise, then remove, its DB."""

    def test_real_byte_context_seeds_fk_parents_and_removes_temp_root(self):
        temporary = tempfile.TemporaryDirectory(prefix="ioc-lifecycle-")
        root = pathlib.Path(temporary.name)
        ctx = _Ctx(root)
        try:
            ctx.seed_entity(TRANSCO)
            rows = ctx.staging.query(
                "SELECT e.entity_key,m.asset_id FROM entities e "
                "JOIN asset_entity_map m ON m.entity_key=e.entity_key "
                "WHERE e.entity_key=? ORDER BY m.asset_id",
                (TRANSCO["entity_key"],))
            self.assertEqual(
                sorted(asset["asset_id"] for asset in TRANSCO["assets"]),
                [row["asset_id"] for row in rows])
        finally:
            ctx.close()
            temporary.cleanup()
        self.assertFalse(root.exists(),
                         "the disposable IOC database root survived test cleanup")


class IOCRetrievalTests(unittest.TestCase):
    """The retrieval itself, on real bytes."""

    @classmethod
    def setUpClass(cls):
        cls.transco = _load(TRANSCO)
        cls.tgp = _load(TGP)

    def test_retrieval_returned_real_filings(self):
        for state, name in ((self.transco, "Transco"), (self.tgp, "TGP")):
            self.assertGreaterEqual(len(state["canonical"]), 10,
                                    f"{name}: expected at least 10 quarterly snapshots")
            for f in state["canonical"]:
                self.assertTrue(f["_parsed"]["contracts"],
                                f"{name} {f['filing_id']}: no D records parsed")
                self.assertEqual(f["_parsed"]["header"]["pipeline_id"],
                                 state["canonical"][0]["_parsed"]["header"]["pipeline_id"])

    def test_tgp_header_pipeline_id_is_c000020(self):
        """The roster CID for TGP is C000020; the header agrees. C000662 does not
        appear anywhere in TGP's own filings and must not be used to match them."""
        ids = {f["_parsed"]["header"]["pipeline_id"] for f in self.tgp["canonical"]}
        self.assertEqual(ids, {"C000020"})
        self.assertNotIn("C000662", ids)

    def test_nonpublic_accession_is_recorded_not_retried(self):
        """Transco's withdrawn Q3-2025 filing 20250701-5067 has availCode N and
        401s on download. It must be recorded, skipped, and never parsed."""
        got = {f["filing_id"] for f in self.transco["filings"]}
        self.assertNotIn("20250701-5067", got)
        blockers = self.transco["ctx"].staging.query(
            "SELECT summary FROM blockers WHERE adapter='ioc'")
        self.assertTrue(any("20250701-5067" in b["summary"] and "availCode N" in b["summary"]
                            for b in blockers),
                        "the nonpublic accession must leave a blocker naming availCode N")

    def test_letter_only_accession_is_skipped(self):
        """20251001-5142 carries a transmittal letter and no data file."""
        self.assertNotIn("20251001-5142",
                         {f["filing_id"] for f in self.transco["filings"]})


class SnapshotDateTests(unittest.TestCase):
    """Header item e, not item c, and not the eLibrary filed date."""

    @classmethod
    def setUpClass(cls):
        cls.transco = _load(TRANSCO)
        cls.tgp = _load(TGP)

    def test_snapshot_date_is_not_the_report_date(self):
        """TGP refiled Q3-2026 on 7 July 2026. The report date moved 7/1 -> 7/7
        and the indicator flipped O -> R, but the as-of date stayed 7/1."""
        rev = next(f for f in self.tgp["filings"] if f["filing_id"] == "20260707-5148")
        header = rev["_parsed"]["header"]
        self.assertEqual(header["report_date"], "2026-07-07")
        self.assertEqual(header["original_revised"], "R")
        self.assertEqual(header["snapshot_date"], "2026-07-01")
        self.assertEqual(rev["snapshot_date"], "2026-07-01",
                         "the filing's effective date must be item e, not item c")
        self.assertNotEqual(rev["snapshot_date"], header["report_date"])

    def test_observations_are_dated_by_the_snapshot_not_the_filing(self):
        """Transco's Q3-2025 index was filed nine days late but is as of 1 July."""
        late = next(f for f in self.transco["filings"] if f["filing_id"] == "20250709-5064")
        self.assertEqual(late["filed_date"], "2025-07-09")
        self.assertEqual(late["snapshot_date"], "2025-07-01")
        mdq = _obs(self.transco, "ioc_firm_transport_mdq", filing_id="20250709-5064")
        self.assertTrue(mdq)
        for o in mdq:
            self.assertEqual(o["instant_date"], "2025-07-01")
            self.assertNotEqual(o["instant_date"], o["value_text"])
            self.assertIn("header item e", o["qa_flags"])

    def test_every_snapshot_observation_carries_the_header_date(self):
        for state in (self.transco, self.tgp):
            by_filing = {f["filing_id"]: f["snapshot_date"] for f in state["filings"]}
            for o in state["observations"]:
                if o["filing_id"] in by_filing and o["metric_id"] != "ioc_mdq_change":
                    self.assertEqual(o["instant_date"], by_filing[o["filing_id"]],
                                     f"{o['metric_id']} is not dated by header item e")

    def test_a_non_quarter_start_snapshot_is_flagged_not_repaired(self):
        """Transco's 20251104-5069 carries item e = 2025-11-01, which is not the
        first day of a calendar quarter. It is kept as filed and flagged, and it
        is its own snapshot rather than a revision of the 1 October index."""
        odd = next((f for f in self.transco["filings"]
                    if f["filing_id"] == "20251104-5069"), None)
        self.assertIsNotNone(odd)
        self.assertEqual(odd["snapshot_date"], "2025-11-01")
        self.assertEqual(odd["version_status"], "original",
                         "a different as-of date is not a revision of another snapshot")
        flagged = _obs(self.transco, "ioc_firm_transport_mdq", filing_id="20251104-5069")
        self.assertTrue(flagged)
        for o in flagged:
            self.assertIn("ANOMALY", o["qa_flags"])
            self.assertEqual(o["validation"], Validation.SOURCE_ANOMALY_REVIEW)


class StorageQuantityTests(unittest.TestCase):
    """D item p is a STOCK. The legacy 'max Daily Quantity' caption is wrong."""

    @classmethod
    def setUpClass(cls):
        cls.transco = _load(TRANSCO)
        cls.tgp = _load(TGP)

    def test_storage_quantity_is_a_stock_not_a_daily_rate(self):
        for state in (self.transco, self.tgp):
            for o in _obs(state, "ioc_contracted_storage_quantity",
                          availability=Availability.PRESENT):
                self.assertIsNotNone(o["unit"])
                self.assertNotIn("/day", o["unit"],
                                 "the contracted storage quantity is not a per-day rate")
                self.assertNotIn("/d", o["unit"].replace("/day", ""))
                self.assertIn("STOCK", o["qa_flags"])

    def test_storage_quantity_is_never_added_to_transport_mdq(self):
        """No published figure equals MDQ + storage quantity, on any snapshot."""
        for state in (self.transco, self.tgp):
            for f in state["canonical"]:
                cs = f["_parsed"]["contracts"]
                mdq = sum(c["transport_mdq"] or 0 for c in cs)
                sto = sum(c["storage_quantity"] or 0 for c in cs)
                forbidden = mdq + sto
                for o in state["observations"]:
                    if o["value_num"] is not None and o["instant_date"] == f["snapshot_date"]:
                        self.assertNotAlmostEqual(
                            o["value_num"], forbidden, places=3,
                            msg=f"{o['metric_id']} equals MDQ + storage quantity")

    def test_point_storage_rates_are_strictly_below_the_contract_stock(self):
        """The P-record storage quantity (item yn) is a DAILY rate, so on every
        storage contract it sums to strictly less than the D-record stock. This is
        the dimensional proof that the two fields are not the same quantity."""
        checked = 0
        for state in (self.transco, self.tgp):
            f = state["canonical"][-1]
            for c in f["_parsed"]["contracts"]:
                if not c["storage_quantity"]:
                    continue
                rates = sum(p["storage_qty"] or 0 for p in c["points"])
                if not rates:
                    continue
                checked += 1
                self.assertLess(rates, c["storage_quantity"],
                                f"contract {c['contract_number']}: P storage rates are not "
                                f"below the D storage stock")
        self.assertGreater(checked, 50, "expected many storage contracts to check")


class PointRecordTests(unittest.TestCase):
    """S8 is a segment endpoint. There is no 'P sum = 2 x D total' rule."""

    @classmethod
    def setUpClass(cls):
        cls.transco = _load(TRANSCO)
        cls.tgp = _load(TGP)

    def test_s8_is_a_segment_endpoint_not_a_receipt_code(self):
        self.assertIn("segment", ioc.POINT_CODES["S8"].lower())
        self.assertNotIn("receipt", ioc.POINT_CODES["S8"].lower())
        self.assertIn("receipt", ioc.POINT_CODES["M2"].lower())
        s8 = [o for o in _obs(self.transco, "ioc_points") if "S8" in o["scope"]]
        self.assertTrue(s8, "Transco reports S8 segment endpoints")
        for o in s8:
            self.assertIn("segment", o["scope"].lower())
            self.assertNotIn("receipt point", o["scope"].lower())
            self.assertIn("SEGMENT ENDPOINTS, not receipt codes", o["qa_flags"])

    def test_the_two_pipelines_use_different_point_dialects(self):
        """Transco reports segments and no M2/MQ at all; TGP reports points and
        no S8/S9. A parser that assumes either dialect is wrong on the other."""
        latest_t = self.transco["canonical"][-1]["_parsed"]["contracts"]
        latest_g = self.tgp["canonical"][-1]["_parsed"]["contracts"]
        codes_t = {p["point_code"] for c in latest_t for p in c["points"]}
        codes_g = {p["point_code"] for c in latest_g for p in c["points"]}
        self.assertIn("S8", codes_t)
        self.assertIn("S9", codes_t)
        self.assertNotIn("M2", codes_t)
        self.assertIn("M2", codes_g)
        self.assertIn("MQ", codes_g)
        self.assertNotIn("S8", codes_g)

    def test_no_p_sum_equals_twice_d_total_rule(self):
        """The doubling identity FAILS on a material minority of contracts, so it
        is never used. What does hold on every contract is that each leg's sum is
        at least the MDQ."""
        for state, name in ((self.transco, "Transco"), (self.tgp, "TGP")):
            f = state["canonical"][-1]
            eligible = [c for c in f["_parsed"]["contracts"]
                        if (c["transport_mdq"] or 0) > 0 and c["points"]]
            self.assertGreater(len(eligible), 100)
            failures = sum(
                1 for c in eligible
                if abs(sum(p["transport_qty"] or 0 for p in c["points"])
                       - 2 * c["transport_mdq"]) > 1e-6)
            self.assertGreater(failures, 0,
                               f"{name}: the doubling rule is false and must be measurable "
                               f"as false")
            # the invariant that DOES hold: each leg sums to at least the MDQ
            legs = {"receipt": {"M2", "S9"}, "delivery": {"MQ", "S8"}}
            for c in eligible:
                for _leg, codes in legs.items():
                    rows = [p for p in c["points"] if p["point_code"] in codes]
                    if not rows:
                        continue
                    self.assertGreaterEqual(
                        sum(p["transport_qty"] or 0 for p in rows) + 1e-6,
                        c["transport_mdq"],
                        f"{name} contract {c['contract_number']}: a leg sums below the MDQ")

    def test_published_mdq_comes_from_d_records_not_from_p_records(self):
        for state in (self.transco, self.tgp):
            for f in state["canonical"]:
                d_total = sum(c["transport_mdq"] or 0 for c in f["_parsed"]["contracts"])
                p_total = sum(p["transport_qty"] or 0 for c in f["_parsed"]["contracts"]
                              for p in c["points"])
                published = _obs(state, "ioc_firm_transport_mdq",
                                 filing_id=f["filing_id"],
                                 availability=Availability.PRESENT)
                self.assertEqual(len(published), 1)
                self.assertAlmostEqual(published[0]["value_num"], d_total, places=3)
                if p_total:
                    self.assertNotAlmostEqual(published[0]["value_num"], p_total, places=3)
                    self.assertNotAlmostEqual(published[0]["value_num"], p_total / 2.0,
                                              places=3)

    def test_point_observations_are_location_detail_only(self):
        # The unit is asserted against the REGISTRY's declared unit_rule rather
        # than a literal. This test previously hard-coded "records" while the
        # registry declared "codes", so it failed on a correct adapter: a test
        # that pins a second, private copy of a contract will always eventually
        # disagree with the real one. Binding it to the declaration keeps the
        # assertion exactly as strict without letting it drift again.
        declared = next(m for m in REGISTRY if m.id == "ioc_points").unit_rule
        self.assertTrue(declared, "the registry declares no unit rule for ioc_points")
        seen = 0
        for state in (self.transco, self.tgp):
            for o in _obs(state, "ioc_points"):
                if o["availability"] != Availability.PRESENT:
                    continue
                seen += 1
                self.assertEqual(o["unit"], declared)
                self.assertEqual(o["validation"], Validation.SCOPE_INCOMPATIBLE)
                self.assertIn("no allocation is published", o["qa_flags"])
        self.assertGreater(seen, 0, "no present ioc_points observation was examined, "
                                    "so this check exercised nothing")


class ShipperConcentrationTests(unittest.TestCase):
    """Contracts are aggregated to the legal shipper BEFORE ranking."""

    @classmethod
    def setUpClass(cls):
        cls.transco = _load(TRANSCO)
        cls.tgp = _load(TGP)

    def test_contracts_are_aggregated_to_the_shipper_before_ranking(self):
        for state, name in ((self.transco, "Transco"), (self.tgp, "TGP")):
            f = state["canonical"][-1]
            cs = [c for c in f["_parsed"]["contracts"] if c["transport_mdq"] is not None]
            total = sum(c["transport_mdq"] for c in cs)
            self.assertGreater(total, 0)

            by_shipper: dict[str, float] = {}
            for c in cs:
                key = ioc.normalise_shipper(c["shipper_name"])
                by_shipper[key] = by_shipper.get(key, 0.0) + c["transport_mdq"]
            aggregated = 100.0 * sum(sorted(by_shipper.values(), reverse=True)[:5]) / total
            per_contract = 100.0 * sum(
                sorted((c["transport_mdq"] for c in cs), reverse=True)[:5]) / total
            self.assertGreater(aggregated, per_contract,
                               f"{name}: aggregation must change the answer, otherwise the "
                               f"test proves nothing")

            # the PRIMARY weight answers the requested slot and so carries the
            # registry scope verbatim; it names its weight in qa_flags
            metric = next(mm for mm in BY_ADAPTER["ioc"]
                          if mm.id == "ioc_top5_shipper_concentration")
            published = [o for o in _obs(state, "ioc_top5_shipper_concentration",
                                         instant_date=f["snapshot_date"])
                         if o["scope"] == metric.scope]
            self.assertEqual(len(published), 1)
            self.assertIn("transportation MDQ", published[0]["qa_flags"])
            self.assertAlmostEqual(published[0]["value_num"], aggregated, places=6)
            self.assertNotAlmostEqual(published[0]["value_num"], per_contract, places=6)

    def test_every_share_publishes_its_denominator_and_coverage(self):
        for state in (self.transco, self.tgp):
            f = state["canonical"][-1]
            top5 = [o for o in _obs(state, "ioc_top5_shipper_concentration",
                                    instant_date=f["snapshot_date"])
                    if o["availability"] == Availability.PRESENT]
            self.assertTrue(top5)
            for o in top5:
                self.assertIn("denominator:", o["qa_flags"])
                self.assertIn("identity coverage", o["qa_flags"])
                self.assertIn("unknown-expiry share", o["qa_flags"])
                self.assertIn("aggregated to", o["qa_flags"])
            # and the denominator is a real lineage edge, not just prose
            ids = {o["observation_id"] for o in top5}
            edges = [e for e in state["edges"] if e["observation_id"] in ids]
            self.assertTrue(any(e["input_role"] == "denominator" for e in edges))
            self.assertTrue(any(e["input_role"] == "group_member" for e in edges))

    def test_expiry_buckets_are_reported_separately_with_unknown_and_continuing(self):
        for state in (self.transco, self.tgp):
            f = state["canonical"][-1]
            got = {o["scope"].split("bucket ")[1].split(" |")[0]
                   for o in _obs(state, "ioc_expiry_profile",
                                 instant_date=f["snapshot_date"])
                   if "bucket " in o["scope"]}
            self.assertEqual(got, set(ioc.EXPIRY_BUCKETS))
            for o in _obs(state, "ioc_expiry_profile", instant_date=f["snapshot_date"]):
                if o["availability"] == Availability.PRESENT:
                    self.assertIn("ROLLOVER PERIOD IN DAYS", o["qa_flags"])
                    self.assertIn("never as an expiry", o["qa_flags"])

    def test_the_continuation_field_is_not_turned_into_a_termination_date(self):
        """Item n is a DURATION IN DAYS, not a date. Across every Transco
        snapshot the live vocabulary is 30/60/180/186/279/365/730/1095 -- plain
        day counts, populated only once the primary term has passed. No
        observation may carry a date derived from item m plus item n."""
        values: set[str] = set()
        for f in self.transco["canonical"]:
            values |= {c["rollover_days"] for c in f["_parsed"]["contracts"]
                       if c["rollover_days"]}
        self.assertTrue(values, "item n is populated somewhere in the window")
        for v in values:
            self.assertTrue(v.isdigit(), f"item n value {v!r} is not a plain day count")
            self.assertLessEqual(int(v), 3660, f"item n value {v!r} is implausible as days")
            self.assertNotIn("/", v)
            self.assertNotIn("-", v)
        self.assertTrue({"365", "1095", "730"} <= values,
                        f"expected the common rollover periods; got {sorted(values)}")
        # and no contract's expiry bucket was derived from m + n
        for f in self.transco["canonical"]:
            for c in f["_parsed"]["contracts"]:
                if c["rollover_days"] and c["primary_term_expiry"]:
                    self.assertLessEqual(c["primary_term_expiry"], f["snapshot_date"],
                                         "item n is only populated once the primary term "
                                         "has passed; a future expiry with an item n value "
                                         "would need a different reading")
        for o in self.transco["observations"]:
            self.assertNotIn("termination date", (o["value_text"] or ""))


class SnapshotDiffTests(unittest.TestCase):
    """A contract missing from a snapshot is not a termination event."""

    @classmethod
    def setUpClass(cls):
        cls.transco = _load(TRANSCO)

    def test_a_contract_missing_from_a_snapshot_is_not_a_termination(self):
        state = self.transco
        pairs = list(zip(state["canonical"], state["canonical"][1:]))
        self.assertTrue(pairs)
        disappeared_somewhere = 0
        for prev, cur in pairs:
            before = {c["contract_number"] for c in prev["_parsed"]["contracts"]
                      if c["contract_number"]}
            after = {c["contract_number"] for c in cur["_parsed"]["contracts"]
                     if c["contract_number"]}
            disappeared_somewhere += len(before - after)
        self.assertGreater(disappeared_somewhere, 0,
                           "the fixture must actually contain disappearing contracts")

        # 1. no event of any kind asserts a termination
        events = [e for o in state["observations"] for e in (o.get("_events") or [])]
        for e in events:
            blob = f"{e['event_type']} {e['headline']} {e['detail'] or ''}".lower()
            self.assertNotIn("terminat", blob.replace("not a termination", "")
                             .replace("not termination", ""))
        for o in state["observations"]:
            for field in ("value_text", "notes"):
                self.assertNotIn("terminat", str(o.get(field) or "").lower())

        # 2. the only thing published about the difference is the MDQ change, and
        #    it says in terms that the disappearances are not terminations
        diffs = _obs(state, "ioc_mdq_change", availability=Availability.PRESENT)
        self.assertTrue(diffs)
        for o in diffs:
            self.assertIn("NOT TERMINATION OR AWARD EVENTS", o["qa_flags"])
            self.assertEqual(o["unit"], "Dth/day")

        # 3. the earliest snapshot has no predecessor, and says so rather than
        #    borrowing another snapshot's change
        first = state["canonical"][0]["snapshot_date"]
        earliest = _obs(state, "ioc_mdq_change", instant_date=first)
        self.assertEqual(len(earliest), 1)
        self.assertEqual(earliest[0]["availability"], Availability.NOT_APPLICABLE)
        self.assertIn("needs a preceding snapshot", earliest[0]["missing_reason"])
        self.assertIsNone(earliest[0]["value_num"])

    def test_a_revised_file_is_a_separate_event_from_an_economic_change(self):
        tgp = _load(TGP)
        events = [e for o in tgp["observations"] for e in (o.get("_events") or [])]
        revisions = [e for e in events if e["event_class"] == "revision"]
        self.assertTrue(revisions, "TGP filed two .TA1 revisions in the window")
        for e in revisions:
            self.assertEqual(e["destination"], "filing_archive")
            self.assertIn(e["event_type"], ("revised_filing", "identical_resubmission"))
            self.assertNotIn("terminat", (e["detail"] or "").lower())
        # the revision restates the SAME snapshot: the snapshot date does not move
        rev = next(f for f in tgp["filings"] if f["filing_id"] == "20260707-5148")
        orig = next(f for f in tgp["filings"] if f["filing_id"] == "20260701-5255")
        self.assertEqual(rev["snapshot_date"], orig["snapshot_date"])
        self.assertEqual(rev["version_status"], "revised")
        self.assertEqual(rev["supersedes_filing_id"], orig["filing_id"])
        self.assertEqual(rev["is_canonical"], 1)
        self.assertEqual(orig["is_canonical"], 0)

    def test_mdq_change_has_lineage_to_both_snapshots(self):
        state = self.transco
        for o in _obs(state, "ioc_mdq_change", availability=Availability.PRESENT):
            edges = [e for e in state["edges"] if e["observation_id"] == o["observation_id"]]
            roles = {e["input_role"] for e in edges}
            self.assertEqual(roles, {"minuend", "subtrahend"})


class CoverageAccountingTests(unittest.TestCase):
    """Every frozen slot ends in a measured status, and our gaps stay ours."""

    @classmethod
    def setUpClass(cls):
        cls.transco = _load(TRANSCO)

    def test_every_frozen_slot_is_answered_at_its_own_grain(self):
        """Slots are per (metric, snapshot). Coverage matches on the full grain --
        entity, metric, regime, basis, instant AND scope -- so every slot needs an
        observation at exactly its own scope and its own as-of date. A value from
        another snapshot must never be able to answer it."""
        for state in (self.transco, _load(TGP)):
            produced = {(o["metric_id"], o["instant_date"] or "", o["scope"])
                        for o in state["observations"]}
            for slot in state["expected"]:
                key = (slot["metric_id"], slot.get("instant_date") or "", slot["scope"])
                self.assertIn(key, produced,
                              f"{slot['metric_id']} at {slot.get('instant_date')} has no "
                              f"observation at the slot's own grain")

    def test_slots_are_frozen_per_snapshot_not_collapsed(self):
        state = self.transco
        snapshots = {f["snapshot_date"] for f in state["canonical"]}
        metrics = {s["metric_id"] for s in state["expected"]}
        self.assertGreaterEqual(len(snapshots), 11)
        self.assertEqual(len(state["expected"]), len(snapshots) * len(metrics),
                         "one slot per (metric, snapshot) is the requested denominator")
        self.assertEqual({s.get("instant_date") for s in state["expected"]}, snapshots)

    def test_no_slot_is_left_as_unfinished_engineering(self):
        state = self.transco
        gaps = [o for o in state["observations"]
                if o["availability"] == Availability.NOT_IMPLEMENTED]
        self.assertEqual(gaps, [], f"unimplemented metrics remain: "
                                   f"{[o['metric_id'] for o in gaps]}")

    def test_units_are_read_from_the_header_never_assumed(self):
        state = self.transco
        for f in state["canonical"]:
            self.assertEqual(f["_parsed"]["header"]["uom_transport_code"], "T")
            self.assertEqual(f["_parsed"]["header"]["uom_transport"], "Dth")
        for o in _obs(state, "ioc_firm_transport_mdq",
                      availability=Availability.PRESENT):
            self.assertEqual(o["unit"], "Dth/day")
            self.assertIn("never assumed to be MMBtu", o["qa_flags"])

    def test_running_twice_produces_identical_observation_ids(self):
        """Idempotence: the observation id is a function of the full grain, so a
        second canonicalisation of the same filings reproduces every row."""
        state = self.transco
        again, edges2 = ioc.canonicalise(state["ctx"], TRANSCO, state["filings"],
                                         state["expected"])
        first = {o["observation_id"]: (o["value_text"], o["availability"], o["scope"],
                                       o["unit"], o["instant_date"])
                 for o in state["observations"]}
        second = {o["observation_id"]: (o["value_text"], o["availability"], o["scope"],
                                        o["unit"], o["instant_date"])
                  for o in again}
        self.assertEqual(first, second)
        self.assertEqual(len(state["observations"]), len(again))
        self.assertEqual(len(state["edges"]), len(edges2))


class ImageOnlyReviewFixtureTests(unittest.TestCase):
    """A17's required reviewed-image path, independent of the IOC cache replay."""

    def test_an_image_only_source_is_labelled_as_reviewed_not_as_parsed(self):
        """A17: a page read by a human is not a page that was parsed.

        `confidence="verified_span:reviewed_page_image"` is correct -- the span IS
        verified, and the suffix records HOW the page was read. This fixture is
        mandatory and hash-pinned by the acceptance fixture manifest; it never
        skips or searches a sibling tree when the old ad-hoc image path is absent.
        """
        from adapters import capacity

        evidence = fixture_json("reviewed_image_sources.json")
        self.assertEqual(len(evidence.get("sources") or []), 2,
                         "both independently reviewed image-only filings are required")
        for source in evidence["sources"]:
            content_hash = source["source_object_sha256"]
            review = capacity.REVIEWED_PAGE_IMAGES.get(content_hash)
            self.assertIsNotNone(
                review, f"{source['accession']}: reviewed-source configuration missing")
            self.assertEqual(review["accession"], source["accession"])
            self.assertEqual(review["byte_size"], source["source_bytes"])
            self.assertFalse(review["ocr_used"])

            pages = capacity.reviewed_pages(content_hash)
            self.assertIsNotNone(pages)
            page_text = "\n".join(row["text"] for page in pages for row in page["rows"])
            for qualifier in source.get("required_qualifiers") or []:
                self.assertIn(
                    qualifier.lower().replace("-", " "),
                    page_text.lower().replace("-", " "),
                    f"{source['accession']}: reviewed transcript lost qualifier {qualifier!r}")
            figures = capacity.read_figures(pages, 2023)
            filing = {
                "filing_id": source["accession"], "reporting_year": 2023,
                "filed_date": "2024-02-23", "_as_of": "2024-02-23",
                "_as_of_basis": "date on the independently reviewed filing",
                "_figures": figures, "_shifted": [], "_text_layer": "no",
                "_review": review,
                "_extraction": capacity.REVIEWED_IMAGE_METHOD,
                "_document_id": f"eLibrary|{source['accession']}|reviewed-image-fixture",
                "content_hash": content_hash, "version_status": "original",
            }
            observations = capacity._capacity_observations(
                None, {"entity_key": "W6ACC_SYNTHETIC_IMAGE_REVIEW"}, filing,
                {"cap_reported_capacity": capacity.BY_ID["cap_reported_capacity"]})
            facts = [o["_document_fact"] for o in observations
                     if "_document_fact" in o]
            self.assertTrue(facts, f"{source['accession']}: no canonical facts produced")
            by_value = {d["value_text"]: d for d in facts}
            for expected in source["expected"]:
                self.assertIn(expected["value_text"], by_value)
                fact = by_value[expected["value_text"]]
                self.assertEqual(fact["unit"], expected["unit"])
                self.assertEqual(fact["qualifier"], expected["qualifier"])
                self.assertEqual(fact["extraction_method"],
                                 "reviewed_page_image_transcription")
                self.assertEqual(fact["review_state"], "reviewed")
                self.assertIn("reviewed_page_image", fact["confidence"])
                self.assertIn("OCR used: False", fact["reviewer_note"] or "")
                self.assertIn(fact["value_text"], fact["verbatim_span"])
        self.assertIsNone(
            capacity.reviewed_pages("0" * 64),
            "an unreviewed content hash inherited another document's manual transcript")


class CapacityReportTests(unittest.TestCase):
    """adapters/capacity.py shares this adapter's eLibrary route, so its
    regressions live here. Same rule: real bytes, no mocks."""

    @classmethod
    def setUpClass(cls):
        from adapters import capacity                                # noqa: PLC0415
        cls.capacity = capacity
        cls.temporary = tempfile.TemporaryDirectory(prefix="cap-")
        cls.addClassCleanup(cls.temporary.cleanup)
        tmp = pathlib.Path(cls.temporary.name)
        cls.ctx = _Ctx(tmp)
        cls.addClassCleanup(cls.ctx.close)
        cls.state = {}
        for ent in (TRANSCO, TGP):
            cls.ctx.seed_entity(ent)
            filings = capacity.retrieve(cls.ctx, ent, year_from=2024, year_to=2026)
            expected = capacity.freeze_expected(cls.ctx, ent, filings, ent["assets"])
            cls.ctx.staging.freeze_expected(expected)
            obs, edges = capacity.canonicalise(cls.ctx, ent, filings, expected)
            cls.state[ent["entity_key"]] = {"filings": filings, "expected": expected,
                                            "observations": obs, "edges": edges}

    def _figure(self, cid, contains, unit=None):
        got = [o for o in self.state[cid]["observations"]
               if o["metric_id"] == "cap_reported_capacity" and contains in o["scope"]]
        if unit:
            got = [o for o in got if o["unit"] == unit]
        return got

    def test_the_authority_is_284_13_d_2_not_form_549b(self):
        """The report is filed under 18 CFR 284.13(d)(2) / RM85-1, NOT Form 549B."""
        self.assertIn("284.13(d)(2)", self.capacity.AUTHORITY)
        self.assertIn("RM85-1", self.capacity.AUTHORITY)
        self.assertEqual(self.capacity.CLASS_TYPE, ("Report/Form", "Peak Day Capacity Report"))
        for cid in self.state:
            for o in self.state[cid]["observations"]:
                self.assertIn("284.13(d)(2)", o["applicability_version"])
                self.assertIn("NOT Form 549B", o["applicability_version"])

    def test_transco_system_total_is_extracted_with_its_unit(self):
        got = self._figure("C000654", "Total Estimated Peak Day Deliverability")
        self.assertTrue(got)
        by_year = {o["reporting_year"]: o for o in got}
        self.assertEqual(by_year[2025]["value_text"], "20,554,300")
        self.assertEqual(by_year[2025]["unit"], "MMBtu/D")
        self.assertEqual(by_year[2025]["availability"], Availability.PRESENT)
        self.assertEqual(by_year[2024]["value_text"], "19,802,172")

    def test_tgp_columns_are_split_by_coordinate_not_by_guess(self):
        """The label is 'Stations 245 & 321' and the Zn1->Zn6 figure is 3,303 --
        the non-positional reading '13,303' was a mis-split."""
        got = self._figure("C000020", "Stations 245 & 321")
        self.assertTrue(got)
        values = {o["value_text"] for o in got}
        self.assertIn("3,303", values)
        self.assertIn("1,034", values)
        self.assertNotIn("13,303", values)
        for o in got:
            self.assertEqual(o["unit"], "MDth/d")
            self.assertEqual(o["validation"], Validation.PASS)

    def test_storage_space_and_deliverability_keep_different_units(self):
        space = self._figure("C000020", "Space")
        deliv = self._figure("C000020", "Deliverability")
        self.assertTrue(space and deliv)
        self.assertEqual({o["unit"] for o in space}, {"MDth"})
        self.assertEqual({o["unit"] for o in deliv}, {"MDth/d"})

    def test_every_published_figure_has_page_evidence_and_a_verbatim_span(self):
        for cid in self.state:
            facts = [o["_document_fact"] for o in self.state[cid]["observations"]
                     if "_document_fact" in o]
            self.assertTrue(facts)
            for d in facts:
                self.assertTrue(d["page"])
                self.assertTrue(d["verbatim_span"])
                self.assertTrue(d["scope_note"])
                self.assertIn(d["value_text"], d["verbatim_span"])
                self.assertIn(d["confidence"].split(":")[0],
                              ("verified_span", "review_required"))
                if d["confidence"].split(":")[0] != "verified_span":
                    self.assertIsNone(d["value_num"],
                                      "an ambiguous split must not publish a number")

    def test_peak_day_ratio_is_gated_and_refused_with_reasons(self):
        for cid in self.state:
            gated = [o for o in self.state[cid]["observations"]
                     if o["metric_id"] == "cap_peak_day_ratio"]
            self.assertTrue(gated)
            for o in gated:
                self.assertIsNone(o["value_num"])
                self.assertEqual(o["validation"], Validation.SCOPE_INCOMPATIBLE)
                self.assertEqual(o["availability"], Availability.NOT_APPLICABLE)
                blob = o["missing_reason"] + " " + o["qa_flags"]
                # all four gate dimensions are named, whether they were tested
                # against a candidate denominator or found not evaluable
                self.assertIn("STORAGE TREATMENT", blob)
                self.assertIn("SEASON", blob)
                self.assertIn("SYSTEM SCOPE", blob)
                self.assertIn("never", blob)
                self.assertIn("annual utilisation", blob)
                self.assertNotIn("utilization", blob.lower())
                self.assertNotIn("utilisation rate", blob.lower())

    def test_no_universal_denominator_is_invented(self):
        """TGP's report has no system total, so no denominator is manufactured
        from a route or a rate schedule."""
        gated = [o for o in self.state["C000020"]["observations"]
                 if o["metric_id"] == "cap_peak_day_ratio"]
        self.assertTrue(gated)
        for o in gated:
            self.assertIn("no system-total capacity figure", o["missing_reason"])

    def test_units_are_never_converted_silently(self):
        units = {o["unit"] for cid in self.state
                 for o in self.state[cid]["observations"]
                 if o["metric_id"] == "cap_reported_capacity" and o["unit"]}
        self.assertTrue(units <= {"MMBtu/D", "MDth/d", "MDth", "MMscf/day", "Bcf",
                                  "Dth/d", "MMscf", "Mcf", "Dth", "MMcf/day"},
                        f"unexpected unit vocabulary: {sorted(units)}")
        self.assertIn("MDth/d", units)
        self.assertIn("MMBtu/D", units)


def tearDownModule():
    for state in _STATE.values():
        state["ctx"].close()
        state["temporary"].cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
