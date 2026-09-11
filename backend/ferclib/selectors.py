"""
Generic, config-driven fact selection.

The reference engine expressed selection as ~1,200 lines of per-metric
imperative code. Here the same rules are one dispatch table driven by the
registry's `selector` string, so a new metric is a registry entry rather than a
new code path -- which is what "one implementation, not one script tree per
asset" requires.

Selection invariants carried over from the verified engine, each of which exists
because a real filing broke the naive version:

  * never pick a headline by sign, magnitude or first row. Negative adjustment
    members are valid raw facts and are not peaks;
  * never mix subtotal rows with the detail rows they already contain;
  * never sum overlapping cuts of the same quantity (miles is the classic case);
  * a typed-dimension TOTAL label and an explicit-member total are different
    filings' ways of saying the same thing -- both are accepted, separately;
  * identical duplicate facts are resolved deterministically by lowest document
    order, and the duplicate count is recorded rather than hidden.
"""

from __future__ import annotations

import json
import re

from . import periods


def _dims(row: dict, key: str) -> list[dict]:
    raw = row.get(key) or ""
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def explicit_dims(row: dict) -> list[dict]:
    return _dims(row, "explicit_dims_json")


def typed_dims(row: dict) -> list[dict]:
    return _dims(row, "typed_dims_json")


def _local(qname: str) -> str:
    return (qname or "").split(":")[-1]


# ---------------------------------------------------------------- predicates

def sel_undim(row: dict, **_) -> bool:
    """No dimensions at all: the plain reported figure."""
    return not row.get("explicit_dims_json") and not row.get("typed_dims_json")


def sel_explicit_only(row: dict, axis: str = "", member: str = "", **_) -> bool:
    """Exactly one explicit member on the named axis, and no typed dimension.

    'Exactly one' matters: several filings carry a two-axis twin of the same
    value, and selecting the minimal-dimension context keeps the choice stable
    across years."""
    ex = explicit_dims(row)
    return (not row.get("typed_dims_json") and len(ex) == 1
            and _local(ex[0].get("axis")) == axis and _local(ex[0].get("member")) == member)


def sel_typed_system(row: dict, **_) -> bool:
    """Exactly one typed dimension on SystemNameAxis: the p.520 system row."""
    ty = typed_dims(row)
    return (not row.get("explicit_dims_json") and len(ty) == 1
            and _local(ty[0].get("axis")) == "SystemNameAxis")


def sel_typed_label(row: dict, label: str = "TOTAL", **_) -> bool:
    """A typed-dimension row whose domain value is a TOTAL-style label."""
    ty = typed_dims(row)
    if len(ty) != 1:
        return False
    v = (ty[0].get("value") or "").strip().upper()
    return v == label.upper() or v.startswith(label.upper())


def sel_peak_axis(row: dict, which: str = "single-day", **_) -> bool:
    """The SYSTEM-level p.518 peak row.

    The schedule reports each peak twice: once for the system, and again broken
    down by destination. The system row carries exactly ONE member naming the
    peak kind; a breakdown carries that member PLUS a destination member:

        SingleDayPeakDeliveriesMember                                12,577,560
        SingleDayPeakDeliveriesMember + DeliveredToInterstatePipelines 2,494,678
        SingleDayPeakDeliveriesMember + DeliveredToOthers             10,082,882

    Matching the peak member alone therefore returns all three, which is an
    ambiguity, not a peak. Requiring the minimal-dimension row selects the system
    figure without ever choosing by magnitude -- and the breakdowns remain in the
    raw layer. Adjustment members are excluded by name, never by sign or size.
    """
    want = "SINGLE" if which == "single-day" else "THREE"
    ex = explicit_dims(row)
    ty = typed_dims(row)
    if len(ex) == 1 and not ty:
        m = _local(ex[0].get("member")).upper()
        return want in m and "ADJUST" not in m
    if len(ty) == 1 and not ex:
        v = (ty[0].get("value") or "").upper()
        return want in v and "ADJUST" not in v
    return False


def sel_peak_axis_relaxed(row: dict, which: str = "single-day", **_) -> bool:
    """The peak row when the minimal-dimension form finds nothing.

    FERC's 2021 migration attached an extra TransmissionSystemDeliveriesToAxis
    member to some peak contexts, so the migrated-era rows carry two members
    where the native ones carry one. Requiring exactly one member is right for
    the VOLUMES -- it is what separates the system total from its
    destination breakdown -- but it rejects a migrated DATE that has no
    breakdown at all, only a migration artefact.

    So this is the second tier, tried only when the strict form matched nothing.
    It matches on the peak member alone; if that turns out to be ambiguous,
    `pick()` refuses it, exactly as it would refuse any other ambiguity. Nothing
    is selected by magnitude at either tier.
    """
    want = "SINGLE" if which == "single-day" else "THREE"
    for d in explicit_dims(row):
        m = _local(d.get("member")).upper()
        if want in m and "ADJUST" not in m:
            return True
    for d in typed_dims(row):
        v = (d.get("value") or "").upper()
        if want in v and "ADJUST" not in v:
            return True
    return False


SELECTORS = {
    "undim": sel_undim,
    "explicit-only": sel_explicit_only,
    "typed-system": sel_typed_system,
    "typed-label": sel_typed_label,
    "peak-axis": sel_peak_axis,
    "peak-axis-relaxed": sel_peak_axis_relaxed,
}


def parse_selector(spec: str) -> list[tuple[str, dict]]:
    """'explicit-only:UtilityTypeAxis=GasUtilityMember | undim' ->
       [('explicit-only', {...}), ('undim', {})], tried in order."""
    out: list[tuple[str, dict]] = []
    for alt in spec.split("|"):
        alt = alt.strip()
        if not alt:
            continue
        name, _, arg = alt.partition(":")
        name = name.strip()
        if name not in SELECTORS:
            continue                       # aggregate/derived forms handled elsewhere
        kwargs: dict = {}
        arg = arg.strip()
        if name == "explicit-only" and "=" in arg:
            axis, member = arg.split("=", 1)
            kwargs = {"axis": axis.strip(), "member": member.strip()}
        elif name == "typed-label" and arg:
            kwargs = {"label": arg}
        elif name.startswith("peak-axis") and arg:
            kwargs = {"which": arg}
        out.append((name, kwargs))
    return out


# ---------------------------------------------------------------- matching

def period_matches(row: dict, basis: str, start: str, end: str, instant: str) -> tuple[bool, str]:
    """Interval-exact period matching (see ferclib.periods)."""
    actual_basis = row.get("period_class") or ""
    if actual_basis == "instant":
        actual_basis = periods.INSTANT
    return periods.satisfies(basis, start, end, instant,
                             actual_basis, row.get("period_start") or "",
                             row.get("period_end") or "", row.get("instant") or "")


def candidates(facts: list[dict], metric, basis: str, start: str, end: str,
               instant: str) -> tuple[list[dict], list[str]]:
    """Facts of the metric's concept (or a declared alias) that match BOTH the
    selector and the exact requested period grain. Returns (hits, rejections)."""
    wanted = {metric.concept} | {a["concept"] for a in metric.aliases}
    alts = parse_selector(metric.selector)
    rejects: list[str] = []

    eligible = []
    for row in facts:
        if row.get("concept_local") not in wanted:
            continue
        ok, why = period_matches(row, basis, start, end, instant)
        if not ok:
            rejects.append(f"{row.get('source_fact_id','')[:12]}: {why}")
            continue
        eligible.append(row)

    # Alternatives are tried as WHOLE PASSES, in order, and the first pass that
    # matches anything wins. Trying them per row instead lets a narrow selector
    # match one row while a broader fallback matches others, so a schedule that
    # reports a system total AND its breakdown returns all of them at once and
    # looks ambiguous. The alternation means "prefer this rule, else that one",
    # not "accept whichever rule each row happens to satisfy".
    for name, kwargs in alts:
        hits = []
        for row in eligible:
            if SELECTORS[name](row, **kwargs):
                r = dict(row)
                r["_selector"] = name + (f":{kwargs}" if kwargs else "")
                hits.append(r)
        if hits:
            return hits, rejects

    for row in eligible:
        rejects.append(f"{row.get('source_fact_id','')[:12]}: no selector matched "
                       f"(dims={row.get('explicit_dims_json') or row.get('typed_dims_json') or 'none'})")
    return [], rejects


def pick(hits: list[dict]) -> tuple[dict | None, int, str]:
    """Deterministic choice among candidates.

    Identical values -> lowest document order, with the duplicate count recorded.
    DIFFERENT values -> no pick at all. Choosing by magnitude or first-row would
    be exactly the "first/largest-value shortcut" the specification forbids.
    """
    if not hits:
        return None, 0, "no candidate"
    values = {(h.get("value_as_filed") or "").strip() for h in hits}
    if len(values) > 1:
        return None, len(hits), (f"ambiguous: {len(hits)} candidates with {len(values)} distinct "
                                 f"values {sorted(values)[:4]}; no magnitude or first-row shortcut applied")
    chosen = sorted(hits, key=lambda h: int(h.get("document_order") or 0))[0]
    return chosen, len(hits), ("single candidate" if len(hits) == 1
                               else f"{len(hits)} identical facts; lowest document order selected")


# ---------------------------------------------------------------- aggregates

TOTAL_RE = re.compile(r"^\s*(GRAND\s+)?(TOTAL|SYSTEM TOTAL)\b", re.I)
SUBTOTAL_RE = re.compile(r"\bSUB[- ]?TOTAL\b", re.I)
#: "Field: Total Field Compressor Stations" -- a total of a NAMED subgroup
GROUP_TOTAL_RE = re.compile(r"\bTOTAL\b", re.I)


def companion_labels(all_facts: list[dict]) -> dict[tuple, str]:
    """(axis_local, member_value) -> the human label the filer gave that member.

    FERC typed-dimension members are opaque positional codes ("2-23"), and the
    readable caption lives in a SEPARATE fact on the same axis whose value is
    text ("Other: TOTAL Compressor Stations:"). Without this join a total row is
    indistinguishable from a detail row, and summing everything triple-counts a
    schedule that carries details, subgroup totals and a grand total together.

    Several text concepts can caption the same axis member, so ALL captions for a
    member are kept. First-wins over an unordered query made the result depend on
    SQLite row order, which silently changed whether a schedule's grand total was
    recognised.
    """
    out: dict[tuple, set] = {}
    for r in all_facts:
        ty = typed_dims(r)
        if len(ty) != 1:
            continue
        v = (r.get("value_as_filed") or "").strip()
        if not v or _fnum(v) is not None:
            continue                    # numeric: a measurement, not a caption
        key = (_local(ty[0].get("axis")), ty[0].get("value"))
        out.setdefault(key, set()).add(v)
    return out


def row_captions(row: dict, labels: dict[tuple, set]) -> list[str]:
    """Every caption the filer attached to this row's dimension member."""
    ty = typed_dims(row)
    if len(ty) == 1:
        hit = labels.get((_local(ty[0].get("axis")), ty[0].get("value")))
        if hit:
            return sorted(hit)
        return [ty[0].get("value") or ""]
    ex = explicit_dims(row)
    return [_local(ex[0].get("member"))] if ex else []


def row_label(row: dict, labels: dict[tuple, set]) -> str:
    """The most total-like caption, so a row captioned both 'Total' and something
    else is still recognised as a total."""
    caps = row_captions(row, labels)
    if not caps:
        return ""
    def _score(c):
        tail = c.strip().rstrip(":").rsplit(":", 1)[-1].strip()
        if TOTAL_RE.match(tail):
            return 2
        if SUBTOTAL_RE.search(c) or GROUP_TOTAL_RE.search(tail):
            return 1
        return 0
    return max(caps, key=lambda c: (_score(c), len(c)))


def classify_rows(rows: list[dict], labels: dict[tuple, str]):
    """(total-ish rows, detail rows) by their filed captions."""
    totalish, details = [], []
    for r in rows:
        lab = row_label(r, labels).strip().rstrip(":").strip()
        r["_caption"] = lab
        # captions are "Group: Caption"; a trailing colon is decorative
        tail = lab.rsplit(":", 1)[-1].strip() if ":" in lab else lab
        if SUBTOTAL_RE.search(lab) or TOTAL_RE.match(tail.strip()) or \
           (GROUP_TOTAL_RE.search(tail) and "TOTAL" in tail.upper()):
            totalish.append(r)
        else:
            details.append(r)
    return totalish, details


def resolve_filed_total(totalish: list[dict], tol_abs: float, tol_rel: float = 0.0):
    """Pick the GRAND total out of a set of total-ish rows.

    A schedule may carry subgroup totals, a grand total, or only subgroup totals
    and no grand total at all. Guessing by magnitude gets the last case wrong --
    the largest subtotal is not the system figure. So the filer's own caption
    decides the KIND, and arithmetic only breaks ties between several candidate
    grand totals:

      * caption tail begins "TOTAL"/"GRAND TOTAL" and is not a "Subtotal"
        -> a grand-total candidate;
      * anything else total-ish -> a subgroup total.

    Returns (grand_total_row, subgroup_rows, residual). `grand_total_row` is None
    when the schedule has no grand total, and the caller must then SUM the
    disjoint subgroup rows rather than pick one.
    """
    vals = [(r, _fnum(r.get("value_as_filed"))) for r in totalish]
    vals = [(r, v) for r, v in vals if v is not None]
    if not vals:
        return None, [], 0.0

    def _is_grand(row) -> bool:
        cap = (row.get("_caption") or "").strip().rstrip(":")
        if SUBTOTAL_RE.search(cap):
            return False
        tail = cap.rsplit(":", 1)[-1].strip() if ":" in cap else cap
        return bool(TOTAL_RE.match(tail))

    grands = [(r, v) for r, v in vals if _is_grand(r)]
    subs = [(r, v) for r, v in vals if not _is_grand(r)]

    if not grands:
        return None, [r for r, _ in subs], 0.0
    if len(grands) == 1:
        top, top_v = grands[0]
    else:
        # several rows claim to be a total: the real one contains the others
        top, top_v = max(grands, key=lambda x: x[1])
        others = sum(v for r, v in grands if r is not top)
        slack = max(tol_abs, abs(top_v) * tol_rel)
        if others > top_v + slack:
            return None, [r for r, _ in vals], 0.0
    residual = top_v - sum(v for _, v in subs)
    return top, [r for r, _ in subs], residual



def partition_totals(facts: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(total rows, subtotal rows, detail rows) by typed-dimension label.

    Kept separate so a sum can never mix a subtotal with the details it already
    contains, and so an aggregate row is never added to the detail it summarises.
    """
    totals, subs, details = [], [], []
    for f in facts:
        ty = typed_dims(f)
        label = (ty[0].get("value") if ty else "") or ""
        if SUBTOTAL_RE.search(label):
            subs.append(f)
        elif TOTAL_RE.match(label):
            totals.append(f)
        else:
            details.append(f)
    return totals, subs, details


def dimensional_signature(row: dict) -> tuple:
    """The set of axes a fact is cut by.

    A FERC schedule frequently reports the same quantity under two ALTERNATIVE
    cuts -- compressor horsepower per named station (typed
    NameAndLocationOfCompressorStationAxis) and again by compressor type
    (explicit CompressorTypesAxis). Each cut sums to the same system total, so
    summing across cuts doubles it. Grouping by signature keeps a sum inside one
    cut and turns the other into an independent cross-check.
    """
    ex = tuple(sorted(_local(d.get("axis")) for d in explicit_dims(row)))
    ty = tuple(sorted(_local(d.get("axis")) for d in typed_dims(row)))
    return ("explicit", ex, "typed", ty)


def partition_by_signature(rows: list[dict]) -> dict[tuple, list[dict]]:
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault(dimensional_signature(r), []).append(r)
    return groups


def disjoint_sum(rows: list[dict], *, tolerance: float = 0.0) -> tuple[float | None, str]:
    """Sum rows that are proven mutually exclusive by their own member labels.

    Refuses when two rows share a label -- an overlapping cut, and summing those
    is exactly the double-count this whole module exists to prevent.
    """
    seen: dict[str, dict] = {}
    total = 0.0
    for r in rows:
        ty = typed_dims(r)
        ex = explicit_dims(r)
        key = ((ty[0].get("value") if ty else "")
               or (_local(ex[0].get("member")) if ex else "")
               or r.get("source_fact_id", ""))
        if key in seen:
            return None, f"overlapping cut: member {key!r} appears more than once; refusing to sum"
        seen[key] = r
        v = _fnum(r.get("value_as_filed"))
        if v is None:
            return None, f"non-numeric member value for {key!r}"
        total += v
    return total, f"disjoint sum of {len(rows)} mutually exclusive members"


def check_aggregate(aggregate: float, detail_sum: float, tolerance_abs: float,
                    tolerance_rel: float = 0.0) -> tuple[bool, str]:
    diff = abs(aggregate - detail_sum)
    rel = diff / abs(aggregate) if aggregate else 0.0
    ok = diff <= tolerance_abs or (tolerance_rel and rel <= tolerance_rel)
    return ok, (f"aggregate {aggregate:,.4f} vs detail sum {detail_sum:,.4f} "
                f"(diff {diff:,.4f}, {rel:.6%})")


# ---------------------------------------------------------------- tolerances
# Metric-specific, per the revised template's rule 10: the blanket $1 hard gate
# is removed. TGP has a source-backed $2 identity warning that is real evidence,
# not a defect to be forgiven or repaired. A warning never becomes a repair, and
# a small arithmetic tolerance never forgives a SEMANTIC error.

TOLERANCE = {
    "currency": {"abs": 100.0, "rel": 0.00001},
    "identity": {"abs": 100.0, "warn_abs": 1.0, "rel": 0.00001},
    "volume":   {"abs": 1000.0, "rel": 0.00001},
    "miles":    {"abs": 0.15, "rel": 0.0},
    "count":    {"abs": 0.0, "rel": 0.0},
    "percent":  {"abs": 0.0001, "rel": 0.0},
}


def tolerance_for(unit_rule: str) -> dict:
    u = (unit_rule or "").lower()
    if "usd" in u:
        return TOLERANCE["currency"]
    if "utr:mi" in u or "mi" in u.split():
        return TOLERANCE["miles"]
    if "dth" in u or "bbl" in u or "barrel" in u:
        return TOLERANCE["volume"]
    if "percent" in u or "ratio" in u:
        return TOLERANCE["percent"]
    if "pure" in u or "count" in u:
        return TOLERANCE["count"]
    return TOLERANCE["currency"]


# ---------------------------------------------------------------- aggregate folding

def _fnum(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def fold_aggregates(rows: list[dict], tol_abs: float, tol_rel: float = 0.0):
    """Collapse a filed schedule's aggregate rows over the details they summarise.

    FERC schedules interleave detail rows, subtotal rows and a grand total in ONE
    typed axis whose members are opaque codes ("0-24"), so neither the code nor a
    label keyword reliably says which is which. What DOES say so is arithmetic: an
    aggregate row's value equals the sum of the rows it summarises.

    So we walk the schedule in document order over a stack. When a row's value
    equals the sum of a suffix of the stack, that suffix is its children and the
    row replaces them. What remains at the end is a mutually exclusive set, and
    summing it double-counts nothing.

    Worked example -- Transco FY2016 transmission miles:
        rows 0-1 .. 0-22 (details) sum to 9236.2
        row  0-24 "OPERATED BY RESPONDENT"     = 9236.2  -> folds them in
        row  0-25 "NOT OPERATED BY RESPONDENT" =   56.5  -> stands alone
        result 9292.7, which is the independently reviewed reference value.
    Naively summing every row gives 18,585.4 -- exactly double.

    Returns (total, top_level_rows, note) or (None, [], reason).
    """
    ordered = sorted(rows, key=lambda r: int(r.get("document_order") or 0))
    stack: list[tuple[dict, float, list]] = []
    folds = 0
    for r in ordered:
        v = _fnum(r.get("value_as_filed"))
        if v is None:
            continue
        matched = False
        # longest suffix first: prefer the widest aggregate the row can explain
        for k in range(len(stack), 1, -1):
            suffix = stack[-k:]
            total = sum(x[1] for x in suffix)
            diff = abs(total - v)
            if diff <= tol_abs or (tol_rel and v and diff / abs(v) <= tol_rel):
                children = [x for x in suffix]
                del stack[-k:]
                stack.append((r, v, children))
                folds += 1
                matched = True
                break
        if not matched:
            stack.append((r, v, []))
    total = sum(x[1] for x in stack)
    note = (f"{len(ordered)} filed rows folded into {len(stack)} mutually exclusive "
            f"top-level row(s) via {folds} aggregate relationship(s); "
            "aggregate rows replace the details they summarise rather than being "
            "added to them")
    return total, [x[0] for x in stack], note


def _balanced_subset(vals: list[float], scale: int, target: int, *, max_members: int = 6):
    """Indices of a SMALL subset summing exactly to `target` (scaled integers).

    Bounded deliberately. A filed aggregate layer is a handful of total rows --
    two ownership totals, four subgroup totals -- never half the schedule, so the
    search stops at `max_members`. That keeps this exact and fast (tens of
    thousands of combinations, not an unbounded subset-sum), and it also makes a
    spurious match far less likely than an unrestricted search would.
    """
    import itertools
    ints = [int(round(v * scale)) for v in vals]
    if any(i < 0 for i in ints):
        return None                     # signed rows: no meaningful partition
    n = len(ints)
    limit = min(max_members, n // 2)
    # A hard work budget. C(86, 6) is 4.5e8 combinations -- on a real 86-row
    # compressor schedule the search stopped being a search and became a hang.
    # The budget keeps this bounded for any schedule size; when it is exhausted
    # the function returns None, which means "no balanced split FOUND", and the
    # caller falls through to its other structural tests rather than guessing.
    budget = 200_000
    for size in range(1, limit + 1):
        for combo in itertools.combinations(range(n), size):
            budget -= 1
            if budget <= 0:
                return None
            if sum(ints[i] for i in combo) == target:
                return list(combo)
    return None


def split_detail_aggregate(rows: list[dict], tol_abs: float, tol_rel: float = 0.0):
    """Split a schedule into its detail rows and its aggregate rows.

    FERC lays a schedule out as detail lines followed by the total lines that
    summarise them, all on ONE typed axis with opaque member codes, so neither
    the axis nor the member says which is which. What does say so is that the
    two halves BALANCE: the totals reproduce the details.

    Transco FY2016 transmission miles, for example, files 22 line rows summing to
    9,292.7 and then two ownership totals -- "OPERATED BY RESPONDENT" 9,236.2 and
    "NOT OPERATED BY RESPONDENT" 56.5 -- which sum to the same 9,292.7. Adding all
    24 rows gives 18,585.4, exactly double the real figure.

    Returns (details, aggregates, note) for the split with the MOST detail rows,
    or (None, None, "") when no balanced split exists.
    """
    ordered = sorted(rows, key=lambda r: int(r.get("document_order") or 0))
    vals = [_fnum(r.get("value_as_filed")) for r in ordered]
    if any(v is None for v in vals) or len(vals) < 3:
        return None, None, ""
    total = sum(vals)
    best = None
    for k in range(len(vals) - 1, 0, -1):
        head, tail = sum(vals[:k]), sum(vals[k:])
        diff = abs(head - tail)
        slack = max(tol_abs, abs(head) * tol_rel)
        if diff <= slack and abs(total - 2 * head) <= 2 * slack:
            best = k
            break
    if best is None:
        # The aggregate rows need not be contiguous. If the rows partition into
        # two equal halves at all, that is the signature of a filed aggregate
        # layer restating its own details, and the answer is one half.
        scale = 100 if tol_abs and tol_abs < 1 else 1
        target = int(round(total * scale / 2))
        if abs(total * scale / 2 - target) < 0.5 and target > 0:
            idx = _balanced_subset(vals, scale, target)
            if idx is not None and len(idx) < len(vals) - len(idx):
                agg_rows = [ordered[i] for i in idx]
                det_rows = [r for i, r in enumerate(ordered) if i not in set(idx)]
                return (det_rows, agg_rows,
                        f"{len(det_rows)} detail row(s) and {len(agg_rows)} aggregate "
                        f"row(s) each sum to {total / 2:,.4g}; the schedule restates its "
                        "own details as totals, so the two halves are alternative "
                        "statements of one quantity and are never added together")
        return None, None, ""
    return (ordered[:best], ordered[best:],
            f"schedule splits into {best} detail row(s) summing to {sum(vals[:best]):,.4g} "
            f"and {len(vals) - best} aggregate row(s) summing to {sum(vals[best:]):,.4g}; "
            "the two halves balance, so they are alternative statements of the same "
            "quantity and are never added together")
