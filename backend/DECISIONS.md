# Decision log

Material semantic departures from the older specifications, and the corrections
this build makes to documents that are now known to be wrong. Each entry says
what changed, why, and what evidence settled it.

Precedence applied throughout: the all-regime instruction and the revised
template supersede conflicting older drafts, and actual source evidence governs
factual interpretation.

## Corrections to project documents

**D-1. The annual capacity report is not Form 549B.** The revised template's
`[F3]` register and the Day-3 spec attribute the annual capacity report to Form
549B. It is filed under **18 CFR 284.13(d)(2), Docket RM85-1**, with eLibrary
Class/Type `Report/Form` / `Peak Day Capacity Report`. Form 549B is the Index of
Customers, a different filing on a quarterly cadence. Established from the live
eLibrary Class/Type vocabulary and from the filings themselves.
*Consequence:* the capacity adapter targets the correct document class. Anything
searching for "549B capacity" finds nothing.

**D-2. LNG operational reports are not `284.126(g)`.** The Day-3 spec mapped LNG
operational reports universally to `284.126(g) Semi-Annual Storage Report`. The
revised template already flagged this as unverified; it is **wrong**. No LNG
class or type exists anywhere in the 261-row Class/Type vocabulary. Every LNG
operational report located (Sabine, Corpus Christi, Elba, Gulf LNG) is
`Report/Form` / `Certificate of Compliance Report`. Exactly one filing in the
corpus carries 284.126(g) and its same-day twin is typed as a compliance report:
a filer error, not a regime.
*Consequence:* the reporting obligation is sourced from each facility's own NGA
§3 order condition, quoted verbatim with spans, never inferred from a Class/Type.

**D-3. Tennessee Gas Pipeline files as C000020.** An earlier working note used
C000662. The production roster is correct and was not changed.

**D-4. `Shipper_ID` in Form 549D dataset 29 is not a shipper identifier.** It is
a surrogate row key — 113,255 distinct values over 113,255 rows. The natural key
is `(Form549D_ID, Sequence_Number)`. Shipper identity is filer-local, by name.
*Consequence:* concentration aggregates contracts to the legal shipper before
ranking, and identity coverage is published beside every share.

**D-5. `P sum = 2 x D total` is false for IOC point records.** Measured at 91.8%
(Transco) and 87.8% (TGP). The exception-free invariant is Σ per-leg ≥ MDQ.
S8/S9 are segment endpoints; S8 is not a receipt code.

**D-6. Form 549D annual revenue does include storage, contrary to Order 735-A's
stated exclusion.** 544 Q4 storage rows carry $237,017,275. This is reported as
filed and scoped out of the transportation figure *and its denominator*; neither
interpretation is applied to the data. It is a source-semantic finding, not a
defect, and it is not resolved here.

## Departures in engine semantics

**D-7. Coverage is keyed on period basis and exact interval.** The previous
coverage report keyed on (metric, form, year, quarter) and so counted a
year-to-date fact as satisfying a requested quarter. The only permitted
equivalence is Q1, whose YTD interval genuinely *is* the quarter. A blank filed
quarter and a validly derived quarter are now separate coexisting observations.

**D-8. `APPLICABILITY_UNKNOWN` still attempts extraction.** An unresolved
taxonomy means we cannot say whether the form requires a concept. It does not
mean we should stop looking: if the filer reported it, that settles the question
empirically. Only when nothing is found does the slot report unknown
availability. (Before this, an unresolvable taxonomy version silently emptied ten
years of migrated-era history.)

**D-9. A schedule's aggregate rows are resolved arithmetically and from the
filer's own captions, never by magnitude.** Three distinct double-counts existed
and are handled separately: alternative dimensional cuts of one quantity; a
schedule restating its details as totals on the same axis; and details plus
subgroup totals plus a grand total together. Where two cuts disagree, the FINER
enumeration is published with the discrepancy attached as a warning — chosen on
completeness, which is a stated reason, never on being the larger number.

**D-10. Ownership is a label, never a multiplier.** A 50% JV interest is
displayed as such; the filing entity's figures are its own 100% system data and
are never apportioned, and a filing-entity total is never duplicated across
several physical assets or rolled into a parent.

**D-11. Our unfinished work is never reported as a FERC data gap.**
`Availability.NOT_IMPLEMENTED` is a distinct state and is counted as unfinished
engineering. Conversely a source blank is never reported as an extraction
failure. `Availability.INTERPRETATION_BLOCKED` was added for the third case:
source retrieved and complete, meaning unresolved.

## Superseded by the revised template

**D-12. "Return on rate base" as a headline is removed.** Replaced by the Page
700 revenue-versus-cost panel. The filed return is an allowance inside a
cost-of-service calculation and cannot establish what a carrier earned.

**D-13. Project-level revenue over plant is out of scope.** Retained as a future
candidate only. It is not EBITDA yield, ROIC or IRR, and no complete
profitability numerator has been established.

**D-14. The 0.5% duplicate-divergence rule is removed.** A small difference does
not prove correct aggregation. Where the reporting grain is unresolved the
aggregate is blocked and the alternative treatments are retained as labelled
audit scenarios — not as confidence bounds.

## Documents that were absent

Both named specification documents that the previous task could not find were
supplied in this handoff and were read: `FERC_standard_asset_templates_revised.md`
and `FERC_Monday_7_September_build_plan.md`. No document required by this task
was missing.

---

# Repair decisions — 8 September 2026

Made by the integrator against the independent audit of 8 September (A01–A22).
Each is a choice the audit left open, or a defect the audit did not report.

## D-15. Units: value and unit must agree, declared per metric (A04)

The audit offered two contracts and required one to be chosen and documented.
Chosen: **the stored number agrees with its declared unit**, with the unit
declared by the metric rather than decided in the derivation engine.

`Metric` now carries `canonical_unit` (what the stored value is in),
`display_unit` and `display_scale` (exactly one conversion, applied in one
place). `ferclib/xbrl_adapter.py::_ratios` previously chose the unit with a
hardcoded ternary — `"percent" if m.unit_rule == "percent" else "ratio" …` —
which is why 1,251 fractions carried `percent` and why `liq_revenue_per_barrel`
was flattened to `ratio` even though its registry entry already said USD per
barrel. The engine now reads `canonical_unit` and scales once, in `Decimal`.

**Two conventions coexist deliberately**, because uniformity is what caused the
bug:

* *Derived* percentages are stored percent-valued. The value is ours to define,
  so it is defined to agree with its name and its unit. 169025347/391223090 now
  stores 43.204338 `percent`, matching the audit's acceptance case exactly.
* *Filed* fractions are stored exactly as FERC filed them. `p700_wacc` arrives as
  `xbrli:pure` 0.0913 and stays 0.0913, because the contract forbids changing a
  raw source value. Its registry entry previously declared `unit_rule="percent"`
  while storing a fraction — the same defect as A04, in filed rather than derived
  values, which the audit did not report. It now declares `canonical_unit=
  "fraction"` with `display_scale=100`.

Uniformity was rejected because a single blanket convention is exactly what let a
fraction masquerade as a percent. Per-metric declaration is machine-checkable;
a convention is not. `check_unit_contract()` raises **at import** if an
ambiguously-united metric has no explicit contract, so a new one cannot be added
silently, and `exporters._apply_display_contract` refuses to export any value
whose stored unit contradicts its metric's declared unit.

## D-16. Unit families: admissibility is not comparability (A04, A06)

`UNIT_FAMILIES` is built from the 70 distinct unit tokens the delivered run
actually stored — FERC's real vocabulary, not an idealised one. All 70 resolve;
none is unrecognised.

* **Energy and volume are separate families.** A dekatherm measures energy and a
  cubic foot measures gas volume; the heat content relating them varies by stream
  and is not ours to assume. Merging them would license precisely the comparison
  the table exists to refuse.
* **Percent and fraction are separate families**, which is what makes the A04
  export guard possible.
* **Same family does not mean same magnitude.** `unit_scale_of()` is recorded
  separately from the family so that admissibility (may this observation satisfy
  this slot?) is never confused with comparability (may these two numbers be
  compared?).
* An unrecognised token returns `''` and is never a wildcard. An unknown unit is
  a reason to refuse a match.

## D-17. A ratio on a negligible basis is flagged, not suppressed and not passed

*A defect the audit did not report.* Fayetteville Express (C001012) files
`gas_operating_revenues` of $0.00 for six quarters and $3,499 for 2025Q3 against
net utility operating income of −$19,446,909. The engine divided these cleanly
and published `operating_margin_pct` = −555,785% with `validation='pass'`. The
existing zero-denominator guard is a knife edge: exactly zero is refused, $3,499
is published as validated.

The arithmetic is correct and the inputs are exactly as filed, so neither may be
altered, and the value is not suppressed — a filed condition this unusual is
itself information. But it is not an economic quantity and must never serve as an
unqualified headline or as an input to another calculation. Such rows now carry
`SOURCE_ANOMALY_REVIEW`, which is in `MUST_PROPAGATE`, so the export writer
enforces that the explanation travels with the number.

The threshold (`_RATIO_SANITY = 100`) sits far outside every legitimate value in
the delivered data — the widest genuine margin is −307% — so it separates a real
economic loss from an artefact of a vanishing basis without touching the former.

## D-18. One release identity, four acyclic stages (A13, A21)

`build_release.py` is rewritten and `build_full_bundle.py` is retired to a
forwarding shim, because two independent packaging paths with two disagreeing
manifests is itself the A21 defect.

The release is measured against a **fixed contract declared in advance**
(`acceptance/contract.py`, owned by w6-acceptance), never against an inventory of
whatever happens to exist. Verified: removing `exports/quarterly_key_metrics.csv`
now fails at stage 1 with no archive built, where the old builder returned PASS
with an 84-file manifest.

The chain is strictly acyclic: payload files → hashed from bytes →
`artifact_manifest.json` → archive → hashed from bytes → an **external** receipt.
The manifest contains neither itself nor the receipt. The full manifest includes
the database as an ordinary content-hashed payload file; the lite manifest omits
`staging/` by its declared payload contract. The receipt lives outside the
archive it certifies, so its stated hash can be true.

**Every hash and size comes from bytes actually read.** On this volume
`stat().st_size` returns a stale placeholder for a cloud-evicted file until the
bytes are read — `RUN_STATUS.md` reported 8,046 bytes by `stat()` and 35,171 by
reading. Both previous builders recorded `stat()` sizes, so their manifests
carried byte counts that had never been verified against any bytes.

## D-19. Set-based lineage, with both rules and a member digest (A08)

Some aggregates have thousands of contributing rows, where one edge each is
neither storable nor readable. The **only** permitted alternative to per-row
edges is now a `lineage_populations` row, reached through
`lineage_edges.input_population_id`, recording the source table, the filing
occurrences drawn from, an inclusion rule, an exclusion rule, matched and
candidate counts, and a `member_digest` over the sorted member keys.

Both rules are mandatory: a set defined only by what it includes cannot be
checked for what it wrongly kept. `row_count = 0` is legitimate but then
`empty_reason` is required, because *nothing qualified* and *nothing was
retrieved* are different facts and only one of them is a zero.

## D-20. The baseline is opened read-only, and never hardlinked

The delivered baseline database drifted by 12,288 bytes on 8 September from two
unintended writes: an integrator `PRAGMA wal_checkpoint(TRUNCATE)` taken while
"safely snapshotting" it, and a `ferclib.staging.Staging()` construction, whose
`__init__` issues `PRAGMA journal_mode=WAL`. Content was unaffected — all 29
table counts and both integrity checks matched the audit's own record — and the
file was restored byte-exact and set mode 444.

Two rules follow. Protected databases are opened only as
`file:...?mode=ro&immutable=1`. And `staging/` is **never** hardlinked: a
hardlinked database shares an inode, so writes through a "disposable clone" land
on the original. `source_cache/` hardlinks remain correct — those objects are
content-addressed and only ever added, never rewritten in place.

The general lesson is the same one A01 is about: an operation that presents
itself as read-only must not mutate durable state.
