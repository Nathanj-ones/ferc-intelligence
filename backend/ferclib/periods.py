"""
Period grain: the fix for the Form 2-A coverage defect.

The independent check of 7 September 2026 found that coverage was keyed on
(metric, form, year, quarter) and therefore counted three requested QUARTER
observations as present when only a YEAR-TO-DATE fact existed:

    2025 Q2 gas operating revenue   -> YTD 0 only, no quarter observation
    2025 Q2 maintenance expense     -> YTD 0 only, no quarter observation
    2026 Q2 gas delivered           -> YTD 20,750 only, no quarter observation

At the requested grain the same frozen denominator gives 102/134 = 76.1%, not
105/134 = 78.4%.

The principle applied here is that the EXACT INTERVAL decides equivalence, not
the basis label:

  * A Q1 year-to-date fact covers 1 Jan - 31 Mar. That interval IS the Q1
    quarter, so it legitimately satisfies a requested Q1 quarter. This is the
    "quarter (also YTD)" case, and it is a genuine equivalence.
  * A Q2 year-to-date fact covers 1 Jan - 30 Jun. That is not the Q2 quarter
    interval, so it can NEVER satisfy a requested Q2 quarter. It may only be an
    INPUT to a sequential-YTD derivation.

Everything else in this module follows from that one rule.
"""

from __future__ import annotations

import datetime as dt

# ---------------------------------------------------------------- bases

INSTANT = "instant"
MONTHLY = "monthly"
QUARTER = "quarter"
YTD = "ytd"
ANNUAL = "annual"
INTERVAL = "interval"                 # exact regulatory effective interval
SNAPSHOT = "snapshot"                 # IOC header snapshot date
AS_OF = "as_of"                       # document-sourced "as of" assertion
ANNUAL_OBSERVATION = "annual_observation"   # a point observation reported annually,
                                            # e.g. a peak-day date. Not an interval total.

DURATION_BASES = {MONTHLY, QUARTER, YTD, ANNUAL, INTERVAL}
POINT_BASES = {INSTANT, SNAPSHOT, AS_OF, ANNUAL_OBSERVATION}

QUARTER_START = {"Q1": (1, 1), "Q2": (4, 1), "Q3": (7, 1), "Q4": (10, 1)}
QUARTER_END = {"Q1": (3, 31), "Q2": (6, 30), "Q3": (9, 30), "Q4": (12, 31)}


def quarter_interval(year: int, quarter: str) -> tuple[str, str]:
    ms, ds = QUARTER_START[quarter]
    me, de = QUARTER_END[quarter]
    return f"{year:04d}-{ms:02d}-{ds:02d}", f"{year:04d}-{me:02d}-{de:02d}"


def ytd_interval(year: int, quarter: str) -> tuple[str, str]:
    me, de = QUARTER_END[quarter]
    return f"{year:04d}-01-01", f"{year:04d}-{me:02d}-{de:02d}"


def annual_interval(year: int) -> tuple[str, str]:
    return f"{year:04d}-01-01", f"{year:04d}-12-31"


def days_between(start: str, end: str) -> int | None:
    try:
        return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
    except (ValueError, TypeError):
        return None


def classify_interval(start: str, end: str) -> str:
    """Basis implied by an interval's own length. Dates only, never context-ID
    naming conventions and never a filing's quarter label."""
    d = days_between(start, end)
    if d is None:
        return "other_ambiguous"
    if 28 <= d <= 31:
        return MONTHLY
    if 85 <= d <= 95:
        return QUARTER
    if d >= 350:
        return ANNUAL
    if d >= 175:
        return YTD
    return f"other_{d}d"


# ---------------------------------------------------------------- the rule

def satisfies(requested_basis: str, requested_start: str, requested_end: str,
              requested_instant: str,
              actual_basis: str, actual_start: str, actual_end: str,
              actual_instant: str) -> tuple[bool, str]:
    """
    Does an actual observation satisfy a requested slot AT THE REQUESTED GRAIN?

    Returns (ok, reason). The reason is recorded either way, so a rejection is
    auditable rather than silent.
    """
    # An ANNUAL OBSERVATION is a point the filer reports once a year -- a peak
    # date, a station count. FERC files it against the annual DURATION context,
    # not an instant, so the requested instant is matched to the period END.
    if requested_basis == ANNUAL_OBSERVATION and actual_basis == ANNUAL:
        if requested_instant and actual_end and requested_instant != actual_end:
            return False, (f"annual observation for {requested_instant} does not match the "
                           f"filed annual period ending {actual_end}")
        return True, (f"annual observation matched to the filed annual period "
                      f"{actual_start}..{actual_end}")

    if requested_basis in POINT_BASES or actual_basis in POINT_BASES:
        if requested_basis != actual_basis:
            return False, f"basis mismatch: requested {requested_basis}, actual {actual_basis}"
        if requested_instant and actual_instant and requested_instant != actual_instant:
            return False, f"instant mismatch: requested {requested_instant}, actual {actual_instant}"
        return True, "exact instant match"

    # Duration bases: the interval is what matters.
    if not (requested_start and requested_end and actual_start and actual_end):
        return False, "incomplete interval on one side"

    if actual_start == requested_start and actual_end == requested_end:
        if actual_basis == requested_basis:
            return True, "exact interval and basis match"
        # Genuine equivalence: same interval, differently labelled. The only real
        # case is Q1, where year-to-date and quarter describe the same 90 days.
        return True, (f"equivalent interval ({actual_start}..{actual_end}); "
                      f"filed basis {actual_basis} coincides with requested {requested_basis}")

    return False, (f"interval mismatch: requested {requested_start}..{requested_end} "
                   f"({requested_basis}), actual {actual_start}..{actual_end} ({actual_basis})")


def is_q1_equivalent(year: int, basis: str, start: str, end: str) -> bool:
    """True when a YTD interval coincides with Q1 for that year."""
    return basis == YTD and (start, end) == quarter_interval(year, "Q1")


# ---------------------------------------------------------------- derivation

def sequential_ytd_inputs(year: int, quarter: str) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """
    Inputs for Q_n = YTD_n - YTD_(n-1): ((this ytd start,end),(prior ytd start,end)).

    Q1 returns None: it needs no derivation because its YTD IS the quarter.
    Applies to GAS as well as liquids -- the audit required this generally, not
    only where the older code happened to implement it.
    """
    order = ["Q1", "Q2", "Q3", "Q4"]
    if quarter not in order or quarter == "Q1":
        return None
    prior = order[order.index(quarter) - 1]
    return ytd_interval(year, quarter), ytd_interval(year, prior)


def q4_from_annual_minus_q3_ytd(year: int) -> tuple[tuple[str, str], tuple[str, str]]:
    """Q4 = filed annual total - filed Q3 year-to-date. Two source facts.

    This is an ALGEBRAIC identity over filed inputs, not independent evidence of
    the Q4 figure: a Q4 derived this way will always sum back to the annual
    total by construction. Label it accordingly wherever it is published."""
    return annual_interval(year), ytd_interval(year, "Q3")


def monthly_sum_inputs(year: int, quarter: str) -> list[tuple[str, str]]:
    """Month intervals composing a quarter, for forms that carry a monthly
    schedule (Form 2 p.299). Form 2-A does not collect p.299, which is why the
    sequential-YTD route above exists."""
    ms = QUARTER_START[quarter][0]
    out = []
    for m in (ms, ms + 1, ms + 2):
        last = (dt.date(year + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)).day
        out.append((f"{year:04d}-{m:02d}-01", f"{year:04d}-{m:02d}-{last:02d}"))
    return out


# ---------------------------------------------------------------- due dates

#: Official FERC filing deadlines. Observed filing lag is a scheduling
#: statistic, never a regulatory deadline. Sources: 18 CFR 260.1/260.2/260.300,
#: FERC natural gas and oil industry forms pages.
DEADLINES = {
    "Form 2":      {"kind": "annual", "month": 4, "day": 18},
    "Form 2A":     {"kind": "annual", "month": 4, "day": 18},
    "Form 6":      {"kind": "annual", "month": 4, "day": 18},
    "Form 3Q Gas": {"kind": "days_after_quarter", "days": 60, "nonmajor_days": 70},
    "Form 6Q":     {"kind": "days_after_quarter", "days": 70},
    "Form 549B Capacity": {"kind": "annual", "month": 3, "day": 1},
}


def _next_business_day(d: dt.date) -> dt.date:
    """Weekend adjustment. Federal holidays are NOT modelled here; a deadline
    landing on one is reported as approximate rather than asserted."""
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def due_date(form: str, year: int, quarter: str, *, nonmajor: bool = False) -> dt.date | None:
    rule = DEADLINES.get(form)
    if not rule:
        return None
    if rule["kind"] == "annual":
        return _next_business_day(dt.date(year + 1, rule["month"], rule["day"]))
    days = rule.get("nonmajor_days" if nonmajor else "days", rule["days"])
    me, de = QUARTER_END[quarter]
    return _next_business_day(dt.date(year, me, de) + dt.timedelta(days=days))


def filing_due_status(form: str, year: int, quarter: str, today: dt.date,
                      *, nonmajor: bool = False) -> tuple[str, str]:
    """(status, explanation). A filing absent before its deadline is NOT overdue,
    and an absent record is only a potential missing-filing event after index,
    retrieval health, eligibility and deadline have all been checked."""
    due = due_date(form, year, quarter, nonmajor=nonmajor)
    if due is None:
        return "unknown_deadline", f"no published deadline rule for {form}"
    if today <= due:
        return "not_yet_due", (f"{form} {year} {quarter} is due {due.isoformat()}"
                               f"{' (non-major, 70 days)' if nonmajor else ''}; "
                               f"today is {today.isoformat()}")
    return "past_due", (f"{form} {year} {quarter} was due {due.isoformat()}; "
                        "absence still requires index and retrieval-health checks "
                        "before it is called a missing filing")
