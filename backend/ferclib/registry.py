"""
The all-regime metric registry.

One versioned definition per requested template field. Each carries everything
the pipeline needs to retrieve it, select it, judge it and display it honestly:

  meaning      what the figure actually is, in FERC's own terms
  templates    which asset templates request it
  regimes      which forms/datasets can supply it, as (regime, period_basis)
  scope        the regulatory scope of the number -- the thing that makes or
               breaks a comparison
  unit_rule    the unit vocabulary accepted, including migrated-era variants
  selector     how the value is picked out of the source
  fallback     what to try when the primary route is absent
  derivation   how it may be computed, and from what
  quality_gate the check that must pass before it is called validated
  role         headline | trend | detail | profile | conditional | gated
  gate_reason  for gated metrics: exactly what evidence would ungate it

A metric whose adapter is not yet written is NOT listed with a fake selector: it
is listed with implementation="not_implemented", which the field-status report
counts as unfinished engineering rather than as a FERC data gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

REGISTRY_VERSION = "all-regime-2026-09-09-integrated2"

# regimes
F2, F2A, F3Q = "Form 2", "Form 2A", "Form 3Q Gas"
F6, F6Q = "Form 6", "Form 6Q"
P700 = "Form 6 Page 700"
IOC = "Form 549B IOC"
CAPRPT = "Form 549B Capacity"
F549D = "Form 549D"
DOC = "eLibrary document"
TEXTBLOCK = "XBRL textblock"
OIL_INDEX = "FERC Oil Pipeline Index"

GAS_ANNUAL = (F2, F2A)
GAS_QUARTERLY = (F3Q,)

# templates
T_GAS = "interstate_gas"
T_STORAGE = "gas_storage"
T_LIQ = "liquids"
T_549D = "intrastate_549d"
T_LNG = "lng"


@dataclass
class Metric:
    id: str
    display: str
    meaning: str
    templates: tuple[str, ...]
    regimes: tuple[tuple[str, str], ...]        # (regime, period_basis)
    scope: str
    unit_rule: str
    selector: str
    quality_gate: str
    role: str = "detail"
    concept: str = ""                            # XBRL concept local-name
    aliases: tuple[dict, ...] = ()
    schedule: str = ""
    fallback: str = ""
    derivation: str = ""
    dependencies: tuple[str, ...] = ()
    gate_reason: str = ""
    implementation: str = "implemented"
    adapter: str = ""
    optional_regimes: tuple[tuple[str, str], ...] = ()
    notes: str = ""
    #: The MACHINE-READABLE unit the stored value is actually in. `unit_rule` is
    #: prose describing what the source may supply; this is the contract the
    #: stored number obeys. They are not the same thing, and conflating them is
    #: what produced 1,251 fractions labelled `percent` (audit A04).
    canonical_unit: str = ""
    #: How a consumer should render it, and by what factor. Exactly one
    #: conversion, declared here, applied in one place.
    display_unit: str = ""
    display_scale: float = 1.0
    #: Units FERC is KNOWN to file for this metric that are not its canonical
    #: dimension, each admitted for a stated reason. This is not a loophole: an
    #: entry here is a claim about what the source does, and it must be
    #: evidenced. Everything not listed stays refused.
    admissible_units: tuple[str, ...] = ()
    admissible_units_evidence: str = ""

    def supplies(self, regime: str, basis: str) -> bool:
        return (regime, basis) in self.regimes

    def regime_names(self) -> set[str]:
        return {r for r, _ in self.regimes}


def _fin(*regimes):
    """Financial metrics: annual on the annual forms, quarter+YTD on quarterly."""
    out = []
    for r in regimes:
        if r in GAS_ANNUAL or r in (F6,):
            out += [(r, "annual")]
        else:
            out += [(r, "quarter"), (r, "ytd")]
    return tuple(out)


MIGRATED_TAX = "http://ferc.gov/form/2021-01-01/ferc"
DTH = "utr:dth | ferc:dth (migrated-era unit vocabulary)"
USD = "iso4217:USD"
BBL = "utr:bbl | ferc:bbl | pure (reduced-schedule filers)"

REGISTRY: list[Metric] = []


def M(**kw) -> Metric:
    m = Metric(**kw)
    REGISTRY.append(m)
    return m


# ============================================================ interstate gas
# Carried forward from the Transco/TGP/Form 2-A verified engine. The concepts,
# selectors and quality gates below are the ones that passed those regressions;
# changing them would break the reference regressions on purpose.

M(id="gas_operating_revenues", display="Gas operating revenues",
  meaning="Total operating revenues of the filing entity's gas utility operations, as filed.",
  templates=(T_GAS, T_STORAGE), concept="OperatingRevenues",
  regimes=_fin(F2, F2A, F3Q) + ((F2, "quarter"), (F2A, "quarter")),
  scope="filing entity, whole entity", unit_rule=USD, selector="undim",
  schedule="Form 2/2-A p.114 line 2; p.300-301; Form 3-Q",
  quality_gate="four filed/derived quarters reconcile to the filed annual total within $100 or 0.001%",
  derivation="q4_monthly_sum | q4_annual_minus_q3_ytd | sequential_ytd",
  fallback="where the form carries no monthly revenue schedule (Form 2-A has no p.299), "
           "use annual minus Q3 year-to-date and label it algebraic",
  role="headline", adapter="gas_xbrl"),

M(id="utility_operating_expenses", display="Utility operating expenses",
  meaning="Total utility operating expenses as filed on the income statement.",
  templates=(T_GAS, T_STORAGE), concept="UtilityOperatingExpenses",
  regimes=_fin(F2, F2A, F3Q) + ((F2, "quarter"), (F2A, "quarter")),
  scope="filing entity, whole entity", unit_rule=USD, selector="undim",
  schedule="Form 2/2-A p.114; Form 3-Q",
  quality_gate="quarterly sum reconciles to annual within $100/0.001%; "
               "revenue - expenses = NUOI within metric tolerance",
  derivation="q4_minus_quarters | sequential_ytd", role="headline", adapter="gas_xbrl"),

M(id="net_utility_operating_income", display="Net utility operating income",
  meaning="The filed FERC accounting measure of operating income. Not EBITDA, "
          "not distributable cash flow, not a return on capital.",
  templates=(T_GAS, T_STORAGE), concept="NetUtilityOperatingIncome",
  regimes=_fin(F2, F2A, F3Q) + ((F2, "quarter"), (F2A, "quarter")),
  scope="filing entity, whole entity", unit_rule=USD, selector="undim",
  schedule="Form 2/2-A p.114; Form 3-Q",
  quality_gate="identity revenue - expenses = NUOI; a source-backed rounding gap is a "
               "warning, not a repair",
  derivation="q4_minus_quarters | sequential_ytd", role="headline", adapter="gas_xbrl"),

M(id="operating_margin_pct", display="Operating margin",
  meaning="Net utility operating income divided by gas operating revenues on an "
          "identical scope and period.",
  templates=(T_GAS, T_STORAGE), regimes=((F2, "annual"), (F2A, "annual"), (F3Q, "quarter")),
  scope="identical to both inputs; never mixes scopes", unit_rule="percent",
  selector="derived_ratio", derivation="ratio(net_utility_operating_income, gas_operating_revenues)",
  dependencies=("net_utility_operating_income", "gas_operating_revenues"),
  quality_gate="both inputs present, same entity, same exact interval, same scope; "
               "denominator non-zero and not itself under review",
  role="headline", adapter="gas_xbrl"),

M(id="net_income", display="Net income (loss)",
  meaning="Entity net income including items outside pipeline operations.",
  templates=(T_GAS, T_STORAGE), concept="NetIncomeLoss", regimes=_fin(F2, F2A, F3Q),
  scope="filing entity; includes items outside pipeline operations", unit_rule=USD,
  selector="undim", schedule="Form 2/2-A p.114-117; Form 3-Q",
  quality_gate="canonical uniqueness only", role="detail", adapter="gas_xbrl"),

M(id="gas_plant_in_service", display="Gas plant in service (period-end balance)",
  meaning="Gross gas utility plant in service at the period end.",
  templates=(T_GAS, T_STORAGE), concept="GasPlantInService",
  regimes=((F2, "instant"), (F2A, "instant"), (F3Q, "instant")),
  scope="filing entity", unit_rule=USD, selector="undim",
  schedule="Form 2/2-A p.200-204; Form 3-Q",
  quality_gate="balance movement is not capital spending and is never labelled capex",
  role="profile", adapter="gas_xbrl"),

M(id="transmission_plant", display="Transmission plant (period-end balance)",
  meaning="Gross transmission-function plant at the period end.",
  templates=(T_GAS,), concept="TransmissionPlant",
  regimes=((F2, "instant"), (F2A, "instant"), (F3Q, "instant")),
  scope="transmission function only", unit_rule=USD, selector="undim",
  schedule="Form 2 p.204 line 55; Form 3-Q",
  quality_gate="never net against the TOTAL accumulated provision -- functions must match",
  role="profile", adapter="gas_xbrl"),

M(id="accum_prov_depreciation_gas_plant",
  display="Accumulated provision for depreciation of gas utility plant",
  meaning="Total accumulated depreciation provision for gas utility plant.",
  templates=(T_GAS, T_STORAGE),
  concept="AccumulatedProvisionForDepreciationOfGasUtilityPlant",
  regimes=((F2, "instant"), (F2A, "instant")),
  scope="gas utility plant, filing entity", unit_rule=USD,
  selector="explicit-only:UtilityTypeAxis=GasUtilityMember", schedule="Form 2/2-A p.219",
  quality_gate="minimal-dimension context selected; two-axis twin carries the same value",
  role="profile", adapter="gas_xbrl"),

M(id="accum_depreciation_transmission", display="Accumulated depreciation - transmission",
  meaning="Accumulated depreciation attributable to the transmission function.",
  templates=(T_GAS,), concept="AccumulatedDepreciationTransmission",
  regimes=((F2, "instant"), (F2A, "instant")),
  scope="transmission function only - narrower than the total provision", unit_rule=USD,
  selector="explicit-only:UtilityTypeAxis=GasUtilityMember", schedule="Form 2 p.219 line 26",
  quality_gate="must be <= the total accumulated provision for the same instant",
  role="profile", adapter="gas_xbrl"),

for _mid, _disp, _con, _sched in [
        ("depreciation_expense", "Depreciation expense", "DepreciationExpense", "p.336-338"),
        ("operation_expense", "Operation expense", "OperationExpense", "p.317-325"),
        ("maintenance_expense", "Maintenance expense", "MaintenanceExpense", "p.317-325")]:
    M(id=_mid, display=_disp, meaning=f"{_disp} as filed, exact FERC label preserved.",
      templates=(T_GAS, T_STORAGE), concept=_con, regimes=_fin(F2, F2A, F3Q),
      scope="filing entity", unit_rule=USD, selector="undim",
      schedule=f"Form 2/2-A {_sched}; Form 3-Q",
      quality_gate="canonical uniqueness only", role="detail", adapter="gas_xbrl",
      derivation="sequential_ytd")

M(id="total_throughput", display="Total volume of throughput",
  meaning="Total gas volume moved through the system in the period.",
  templates=(T_GAS,), concept="TotalVolumeOfThroughput",
  regimes=((F3Q, "quarter"), (F2, "quarter"), (F2A, "quarter")),
  scope="system", unit_rule=DTH, selector="undim",
  schedule="Form 2/2-A p.521-M1 line 68; Form 3-Q",
  quality_gate="total = forwardhaul + backhaul where all three are filed; a filed blank "
               "total stays blank and is never replaced by a component",
  role="headline", adapter="gas_xbrl"),

M(id="forwardhaul_throughput", display="Forwardhaul volume of throughput",
  meaning="Forwardhaul component of throughput.",
  templates=(T_GAS,), concept="ForwardhaulVolumeOfThroughput",
  regimes=((F3Q, "quarter"), (F2, "quarter"), (F2A, "quarter")),
  optional_regimes=((F3Q, "quarter"),),
  scope="system", unit_rule=DTH, selector="undim",
  schedule="Form 2/2-A p.521-M1 line 66; Form 3-Q",
  quality_gate="component; never used as the total", role="component", adapter="gas_xbrl"),

M(id="backhaul_throughput", display="Backhaul volume of throughput",
  meaning="Backhaul component of throughput.",
  templates=(T_GAS,), concept="BackhaulVolumeOfThroughput",
  regimes=((F3Q, "quarter"), (F2, "quarter"), (F2A, "quarter")),
  optional_regimes=((F3Q, "quarter"), (F2, "quarter"), (F2A, "quarter")),
  scope="system", unit_rule=DTH, selector="undim",
  schedule="Form 2/2-A p.521-M1 line 67; Form 3-Q",
  quality_gate="a blank backhaul is NOT zero; absence is reported as a source blank",
  role="component", adapter="gas_xbrl"),

_GA = ((F2, "annual"), (F2A, "annual"), (F2, "quarter"), (F2A, "quarter"),
       (F3Q, "quarter"), (F3Q, "ytd"))
for _mid, _disp, _con, _line, _scope in [
        ("gas_received_by_utility", "Quantity of natural gas received by utility",
         "QuantityOfNaturalGasReceivedByUtility", "line 15",
         "system - receipt side, every measurement point"),
        ("gas_delivered_by_utility", "Quantity of natural gas delivered by utility",
         "QuantityOfNaturalGasDeliveredByUtility", "line 37",
         "system - delivery side before losses/unaccounted"),
        ("gas_of_others_received_for_transmission",
         "Gas of others received for transmission (Account 489.2)",
         "QuantityOfNaturalGasReceivedByUtilityGasOfOthersReceivedForTransmission", "line 3",
         "system - receipt-side third-party component"),
        ("deliveries_of_gas_transported_for_others",
         "Deliveries of gas transported for others (Account 489.2)",
         "QuantityOfNaturalGasDeliveredByUtilityDeliveriesOfGasTransportedForOthers", "line 27",
         "system - delivery-side third-party component"),
        ("gas_losses_and_unaccounted_for", "Gas losses and gas unaccounted for",
         "GasAccountGasLossesAndGasUnaccountedForGasAccount", "line 42",
         "system - loss/unaccounted component of the delivery side"),
        ("gas_account_balancing_total",
         "Deliveries, gas losses and unaccounted-for (balancing total)",
         "DeliveriesGasLossesAndUnaccountedForGasAccount", "line 44",
         "system - balancing total; equals total gas received")]:
    M(id=_mid, display=_disp, meaning=f"{_disp}, p.520 system row.",
      templates=(T_GAS, T_STORAGE), concept=_con, regimes=_GA,
      scope=_scope, unit_rule=DTH, selector="typed-system",
      schedule=f"Form 2/2-A p.520 {_line}; Form 3-Q",
      quality_gate="only the p.520 system row is canonical; other cuts stay raw; "
                   "receipt-side total equals the delivery-side balancing total",
      derivation="sequential_ytd", role="detail", adapter="gas_xbrl")

M(id="transmission_miles", display="Total miles of transmission line",
  meaning="Miles of transmission line on the system, from the dedicated schedule.",
  templates=(T_GAS,), concept="LengthOfTransmissionLinesOfTransmissionSystems",
  regimes=((F2, "instant"),), optional_regimes=((F2, "instant"),),
  scope="system", unit_rule="utr:mi",
  selector="undim | filed_aggregate_sum | typed-label:TOTAL", schedule="Form 2 p.514",
  quality_gate="aggregate-row sum equals detail-row sum within 0.15 mi; "
               "overlapping cuts are never summed",
  derivation="filed_aggregate_sum",
  fallback="where the form has no p.514 (Form 2-A), a narrative textblock figure may be "
           "recorded separately as profile_miles_narrative - never as a p.514 total",
  role="profile", adapter="gas_xbrl")

for _mid, _disp, _con, _unit in [
        ("compressor_units", "Number of compressor units", "NumberOfUnitsAtCompressorStation",
         "pure | xbrli:pure (count)"),
        ("certificated_horsepower", "Certificated horsepower",
         "CertificatedHorsepowerForEachCompressorStation",
         "utr:MW (filed; the rendered column is horsepower - warning attached)"),
        ("compressor_fuel", "Gas for compressor fuel", "GasForCompressorFuel", DTH)]:
    M(id=_mid, display=_disp, meaning=f"{_disp} from the compressor-station schedule.",
      templates=(T_GAS,), concept=_con, regimes=((F2, "annual"),),
      scope="system", unit_rule=_unit,
      selector="undim | typed-label:TOTAL | subtotal_sum", schedule="Form 2 p.508-509",
      quality_gate="subtotal sum equals detail sum exactly; subtotals and details never mixed",
      derivation="subtotal_sum", role="profile", adapter="gas_xbrl")

# ---------------------------------------------------------------- storage
M(id="storage_capacity", display="Certificated storage capacity",
  meaning="The authorised storage quantity. NOT necessarily working gas and NOT deliverability.",
  templates=(T_GAS, T_STORAGE), concept="CertificatedStorageCapacity",
  regimes=((F2, "annual"),), scope="system", unit_rule="utr:dth | pure (rendered p.512-513 supplies Dth)",
  selector="undim", schedule="Form 2 p.512-513",
  quality_gate="unit note attached on pure/unitless years; never added to inventory or MDQ",
  role="headline", adapter="gas_xbrl")

M(id="max_day_withdrawal", display="Maximum day's withdrawal from storage",
  meaning="The OBSERVED maximum daily withdrawal in the year. Not an engineering "
          "maximum deliverability.",
  templates=(T_GAS, T_STORAGE), concept="MaximumDaysWithdrawalFromStorage",
  regimes=((F2, "annual"),), scope="system",
  unit_rule="utr:dth | pure (rendered p.512-513 supplies Dth)", selector="undim",
  schedule="Form 2 p.512-513",
  quality_gate="observed, not capability; shown separately from any capacity estimate",
  role="headline", adapter="gas_xbrl")

# Storage injections and withdrawals, with the own-gas / others-gas split the
# template requires be kept distinct. Concepts verified against filed Transco
# Form 2 facts rather than assumed.
for _mid, _disp, _con, _what in [
        ("storage_injections", "Gas delivered to storage (injections)",
         "GasDeliveredToStorage", "total volume injected into storage"),
        ("storage_injections_own", "Gas delivered to storage - respondent's own gas",
         "GasDeliveredToStorageThatBelongToRespondent", "injections of the respondent's own gas"),
        ("storage_injections_others", "Gas delivered to storage - others' gas",
         "GasDeliveredToStorageThatBelongToOthers", "injections of gas belonging to others"),
        ("storage_withdrawals", "Gas withdrawn from storage",
         "GasWithdrawnFromStorage", "total volume withdrawn from storage"),
        ("storage_withdrawals_own", "Gas withdrawn from storage - respondent's own gas",
         "GasWithdrawnFromStorageThatBelongToRespondent", "withdrawals of the respondent's own gas"),
        ("storage_withdrawals_others", "Gas withdrawn from storage - others' gas",
         "GasWithdrawnFromStorageThatBelongToOthers", "withdrawals of gas belonging to others")]:
    M(id=_mid, display=_disp,
      meaning=f"{_disp}: {_what}, as filed.",
      templates=(T_STORAGE, T_GAS), concept=_con,
      regimes=((F2, "annual"), (F2A, "annual")),
      optional_regimes=((F2A, "annual"),),
      scope="system; own gas and others' gas are reported separately and never merged",
      unit_rule=DTH, selector="undim | typed-label:TOTAL",
      schedule="Form 2 p.512-513 storage operations",
      quality_gate="own and others' components never summed into the total without "
                   "evidence that the filed total is absent; service, gas ownership, "
                   "period and units kept distinct",
      role="detail", adapter="gas_xbrl")

M(id="single_day_peak_deliveries", display="Single-day peak deliveries",
  meaning="Peak single-day transmission deliveries, excluding deliveries to storage.",
  templates=(T_GAS,), concept="VolumesOfGasTransported",
  regimes=((F2, "annual_observation"),), scope="system - excludes deliveries to storage",
  unit_rule=DTH, selector="peak-axis:single-day | peak-axis-relaxed:single-day",
  schedule="Form 2 p.518",
  quality_gate="negative adjustment members are raw evidence and are NEVER selected as a "
               "peak by sign or magnitude",
  role="headline", adapter="gas_xbrl")

M(id="three_day_peak_deliveries", display="Three-day peak deliveries",
  meaning="Peak three-day transmission deliveries, excluding deliveries to storage.",
  templates=(T_GAS,), concept="VolumesOfGasTransported",
  regimes=((F2, "annual_observation"),), scope="system - excludes deliveries to storage",
  unit_rule=DTH, selector="peak-axis:three-day | peak-axis-relaxed:three-day",
  schedule="Form 2 p.518",
  quality_gate="as for the single-day peak", role="detail", adapter="gas_xbrl")

for _mid, _disp, _sel in [("single_day_peak_date", "Single-day peak delivery date", "single-day"),
                          ("three_day_peak_start_date", "Three-day peak start date", "three-day")]:
    M(id=_mid, display=_disp, meaning=f"{_disp} as filed; normalised ISO date kept separately.",
      templates=(T_GAS,), concept="StartDatePeakDeliveries",
      aliases=({"concept": "DescriptiveStartDateForPeakDeliveries",
                "taxonomies": [MIGRATED_TAX],
                "note": "migrated free-text date; the START date is normalised"},),
      regimes=((F2, "annual_observation"),), scope="system", unit_rule="(date - no unit)",
      selector=f"peak-axis:{_sel} | peak-axis-relaxed:{_sel}", schedule="Form 2 p.518",
      quality_gate="raw text preserved; implausible dates flagged, never repaired",
      role="detail", adapter="gas_xbrl")

M(id="aux_peaking_capacity", display="Auxiliary peaking facilities - maximum daily delivery capacity",
  meaning="Sum of per-facility maximum daily delivery capacity. A facility sum, not a "
          "certified system capacity.",
  templates=(T_GAS,), concept="AuxiliaryPeakingFacilitiesMaximumDailyDeliveryCapacityOfFacility",
  regimes=((F2, "annual"),), scope="per-facility, summed; not a system capacity figure",
  unit_rule=DTH, selector="facility_sum", schedule="Form 2 p.519",
  quality_gate="flagged derived; facility count recorded", derivation="facility_sum",
  role="detail", adapter="gas_xbrl")

for _mid, _disp, _con in [
        ("negotiated_rate_volumes", "Volumes of negotiated-rate services", "VolumesOfNegotiatedRateServices"),
        ("discounted_rate_volumes", "Volumes of discounted-rate services", "VolumesOfDiscountedRateServices")]:
    M(id=_mid, display=_disp, meaning=f"{_disp}. Volume only - says nothing about rate levels.",
      templates=(T_GAS,), concept=_con, regimes=((F2, "annual"), (F2A, "annual")),
      scope="filing entity", unit_rule="utr:dth | pure (migrated; rendered p.313 supplies Dth)",
      selector="undim", schedule="Form 2/2-A p.313",
      quality_gate="volume only", role="detail", adapter="gas_xbrl")

# ------------------------------------------------- narrative profile fallback
# Form 2-A carries no p.508/512/514/518 schedule, but its p.211.1 textblock does
# carry FERC-filed narrative disclosures. These are separate observations with
# their own scope. They are never labelled as p.514 totals or certified figures.
for _mid, _disp, _what in [
        ("profile_miles_narrative", "Line length (narrative disclosure)", "line length in miles"),
        ("profile_diameter_narrative", "Line diameter (narrative disclosure)", "line diameter"),
        ("profile_compressor_narrative", "Compression (narrative disclosure)",
         "named station compressor units and horsepower"),
        ("profile_capacity_narrative", "Capacity description (narrative disclosure)",
         "described capacity of connected line sections"),
        ("profile_stations_narrative", "Measurement stations (narrative disclosure)",
         "receipt and delivery station counts"),
        ("profile_flow_statement_narrative", "Operating statement (narrative disclosure)",
         "the filer's own statement about current or scheduled flows")]:
    M(id=_mid, display=_disp,
      meaning=f"FERC-filed narrative disclosure of {_what}, extracted with its exact span.",
      templates=(T_GAS, T_STORAGE), concept="GeneralInformationOnPlantAndOperationsTextblock",
      regimes=((TEXTBLOCK, "annual_observation"),),
      scope="as worded by the filer; NOT a p.514/p.508 schedule total and not a certified figure",
      unit_rule="as stated in the narrative", selector="textblock_span",
      schedule="Form 2-A p.211.1 GeneralInformationOnPlantAndOperationsTextblock",
      quality_gate="exact span, qualifier, unit and source fact identity stored; "
                   "described sections are never added together; no unit conversion "
                   "without a sourced basis",
      role="profile", adapter="gas_xbrl")

# ============================================================ liquids
M(id="liq_operating_revenue", display="Operating revenue (carrier)",
  meaning="Total operating revenue of the filing carrier, whole-entity scope.",
  concept="OperatingRevenues",
  templates=(T_LIQ,), regimes=_fin(F6, F6Q), scope="filing entity, whole entity",
  unit_rule=USD, selector="undim", schedule="Form 6 income statement",
  quality_gate="never mixed with the Page 700 interstate-only panel",
  derivation="sequential_ytd", role="headline", adapter="liquids_xbrl")

M(id="liq_operating_expenses", display="Operating expenses (carrier)",
  meaning="Total operating expenses of the filing carrier.",
  concept="OperatingExpenses",
  templates=(T_LIQ,), regimes=_fin(F6, F6Q), scope="filing entity, whole entity",
  unit_rule=USD, selector="undim", schedule="Form 6 income statement",
  quality_gate="quarterly sum reconciles to annual within tolerance",
  derivation="sequential_ytd", role="headline", adapter="liquids_xbrl")

M(id="liq_net_carrier_operating_income", display="Net carrier operating income",
  meaning="Filed accounting operating performance of the carrier. Not EBITDA or cash flow.",
  concept="NetCarrierOperatingIncome",
  templates=(T_LIQ,), regimes=_fin(F6, F6Q), scope="filing entity, whole entity",
  unit_rule=USD, selector="undim", schedule="Form 6 income statement",
  quality_gate="identity revenue - expenses = NCOI within metric tolerance",
  derivation="sequential_ytd", role="headline", adapter="liquids_xbrl")

M(id="liq_operating_margin_pct", display="Carrier operating margin",
  meaning="Net carrier operating income over operating revenue, identical scope and period.",
  templates=(T_LIQ,), regimes=((F6, "annual"), (F6Q, "quarter")),
  scope="identical to both inputs", unit_rule="percent", selector="derived_ratio",
  derivation="ratio(liq_net_carrier_operating_income, liq_operating_revenue)",
  dependencies=("liq_net_carrier_operating_income", "liq_operating_revenue"),
  quality_gate="both inputs validated, same scope and exact interval",
  role="headline", adapter="liquids_xbrl")

M(id="liq_barrels_delivered", display="Barrels delivered",
  meaning="Barrels delivered in the period.",
  concept="NumberOfBarrelsDeliveredOut",
  templates=(T_LIQ,), regimes=((F6, "annual"), (F6Q, "quarter"), (F6Q, "ytd")),
  scope="filing entity", unit_rule=BBL, selector="undim",
  schedule="Form 6 traffic schedule",
  quality_gate="native filed quarter preferred; a YTD difference is derived and labelled; "
               "a YTD fact never satisfies a requested quarter",
  derivation="sequential_ytd", role="headline", adapter="liquids_xbrl")

M(id="liq_barrels_received", display="Barrels received",
  meaning="Barrels received in the period.",
  concept="NumberOfBarrelsReceived",
  templates=(T_LIQ,), regimes=((F6, "annual"), (F6Q, "quarter"), (F6Q, "ytd")),
  scope="filing entity", unit_rule=BBL, selector="undim",
  schedule="Form 6 traffic schedule", quality_gate="as for barrels delivered",
  derivation="sequential_ytd", role="trend", adapter="liquids_xbrl")

M(id="liq_barrel_miles", display="Barrel-miles",
  meaning="Volume-distance activity. Not a utilisation measure.",
  concept="NumberOfBarrelMilesOnTrunkLinesOfOilProducts",
  templates=(T_LIQ,), regimes=((F6, "annual"), (P700, "annual")),
  scope="preserve interstate versus whole-entity scope explicitly",
  unit_rule="barrel-miles", selector="undim", schedule="Form 6 traffic; Page 700 line",
  quality_gate="interstate and whole-entity figures are separate observations",
  role="detail", adapter="liquids_xbrl")

for _mid, _disp in [("liq_trunk_revenue", "Trunk line revenue"),
                    ("liq_delivery_revenue", "Delivery revenue"),
                    ("liq_allowance_revenue", "Allowance revenue"),
                    ("liq_incidental_revenue", "Incidental revenue")]:
    M(id=_mid, display=_disp, meaning=f"{_disp} component as filed, exact FERC label preserved.",
      templates=(T_LIQ,), regimes=((F6, "annual"),), optional_regimes=((F6, "annual"),),
      scope="filing entity", unit_rule=USD, selector="undim",
      schedule="Form 6 operating revenue accounts",
      quality_gate="components never summed over an already-filed total",
      role="detail", adapter="liquids_xbrl")

for _mid, _disp, _note in [
        ("liq_carrier_property", "Carrier property", "gross carrier property balance"),
        ("liq_noncarrier_property", "Noncarrier property", "gross noncarrier property balance"),
        ("liq_accum_depreciation", "Accumulated depreciation (carrier property)",
         "accumulated depreciation on carrier property"),
        ("liq_gross_additions", "Gross additions to carrier property",
         "gross additions in the year; NOT company-defined maintenance or growth capex"),
        ("liq_miles_of_pipeline", "Miles of pipeline", "miles of pipeline operated")]:
    M(id=_mid, display=_disp, meaning=f"{_disp}: {_note}.",
      templates=(T_LIQ,), regimes=((F6, "instant"),) if "miles" not in _mid else ((F6, "annual"),),
      scope="filing entity", unit_rule=USD if "miles" not in _mid else "utr:mi",
      selector="undim", schedule="Form 6 property schedules",
      quality_gate="exact labels preserved; gross additions are not capex",
      role="profile", adapter="liquids_xbrl")

# ---------------------------------------------------------------- Page 700
#: Page 700 items filed on the 31-December INSTANT context rather than the
#: annual duration. Verified against live Magellan and Saddlehorn filings: rate
#: base and the return components are balances, the flows are durations.
P700_INSTANT = {
    "p700_original_cost_rate_base", "p700_trended_original_cost_rate_base",
    "p700_return_component", "p700_income_tax_allowance",
}

_P700 = [
    ("p700_interstate_operating_revenue", "Page 700 interstate operating revenue",
     "Interstate operating revenue as reported on Page 700. NOT p.114 whole-entity revenue.", USD),
    ("p700_throughput_barrels", "Page 700 throughput (barrels)",
     "Interstate throughput in barrels as reported on Page 700.", BBL),
    ("p700_barrel_miles", "Page 700 barrel-miles", "Interstate barrel-miles on Page 700.",
     "barrel-miles"),
    ("p700_total_cost_of_service", "Page 700 total cost of service",
     "The carrier's reported cost of service. A regulatory computation, not actual cost.", USD),
    # The return component already says it is an allowance and not an achieved
    # return. The rate bases said only what they were, not what they are not --
    # so nothing stopped a consumer computing return / rate base and calling the
    # quotient a realised return on capital. That quotient is the ALLOWED return
    # in a cost-of-service calculation and nothing else. (w3-financial request 2,
    # found by an acceptance test they deliberately left failing.)
    ("p700_original_cost_rate_base", "Page 700 original-cost rate base",
     "The rate base used as the INPUT to the Page 700 cost-of-service computation, on "
     "the original-cost methodology. It is a regulatory construct, not an asset value, "
     "not invested capital, and never a denominator for realised return on capital. "
     "Return over rate base is the ALLOWED return in a cost-of-service calculation, "
     "never an achieved one.", USD),
    ("p700_trended_original_cost_rate_base", "Page 700 trended-original-cost rate base",
     "The rate base used as the INPUT to the Page 700 cost-of-service computation, on "
     "the trended-original-cost methodology. It is a regulatory construct, not an asset "
     "value, not invested capital, and never a denominator for realised return on "
     "capital. Return over rate base is the ALLOWED return in a cost-of-service "
     "calculation, never an achieved one.", USD),
    ("p700_return_component", "Page 700 return component",
     "The return ALLOWANCE inside the cost-of-service calculation. An input to that "
     "calculation, never an achieved return on capital or realised profit.", USD),
    ("p700_income_tax_allowance", "Page 700 income tax allowance",
     "Income tax allowance component of the cost of service.", USD),
    ("p700_depreciation_regulatory", "Page 700 regulatory-basis depreciation",
     "Depreciation on the regulatory basis used in the cost of service.", USD),
    ("p700_wacc", "Page 700 weighted average cost of capital",
     "The WACC used in the return allowance.", "percent"),
    ("p700_capital_structure_debt", "Page 700 capital structure - debt share",
     "Debt proportion in the capital structure used for the return allowance.", "percent"),
    ("p700_capital_structure_equity", "Page 700 capital structure - equity share",
     "Equity proportion in the capital structure used for the return allowance.", "percent"),
]
for _mid, _disp, _mean, _unit in _P700:
    M(id=_mid, display=_disp, meaning=_mean, templates=(T_LIQ,),
      regimes=((P700, "instant" if _mid in P700_INSTANT else "annual"),),
      scope="INTERSTATE, carrier-reported, annual. Never mixed with whole-entity Form 6 figures.",
      unit_rule=_unit, selector="undim", schedule="Form 6 Page 700",
      quality_gate="scope tag 'interstate p.700' must match on both sides of any ratio",
      role="detail", adapter="liquids_xbrl")

M(id="p700_revenue_less_cost_of_service", display="Page 700 revenue less reported cost of service",
  meaning="A REGULATORY SCREEN: reported interstate revenue minus reported cost of service. "
          "Not realised profit and not a finding that rates are unlawful.",
  templates=(T_LIQ,), regimes=((P700, "annual"),),
  scope="both inputs from Page 700, same year, same carrier", unit_rule=USD,
  selector="derived_difference",
  derivation="difference(p700_interstate_operating_revenue, p700_total_cost_of_service)",
  dependencies=("p700_interstate_operating_revenue", "p700_total_cost_of_service"),
  quality_gate="both inputs present and from the same Page 700 filing",
  role="headline", adapter="liquids_xbrl")

M(id="p700_revenue_to_cost_ratio", display="Page 700 revenue / cost-of-service ratio",
  meaning="The same regulatory screen expressed as a ratio.",
  templates=(T_LIQ,), regimes=((P700, "annual"),),
  scope="Page 700 interstate only", unit_rule="ratio", selector="derived_ratio",
  derivation="ratio(p700_interstate_operating_revenue, p700_total_cost_of_service)",
  dependencies=("p700_interstate_operating_revenue", "p700_total_cost_of_service"),
  quality_gate="denominator non-zero; displayed with the dollar figures as one story",
  role="headline", adapter="liquids_xbrl")

M(id="liq_revenue_per_barrel", display="Transport revenue per barrel",
  meaning="Matched transport/trunk revenue divided by matched barrels. Revenue intensity, "
          "NOT the filed tariff rate.",
  templates=(T_LIQ,), regimes=((F6, "annual"), (P700, "annual")),
  scope="requires matching service, product, route and period on both inputs",
  unit_rule="USD per barrel", selector="derived_ratio",
  derivation="ratio(matched revenue, matched barrels)",
  dependencies=("p700_interstate_operating_revenue", "p700_throughput_barrels"),
  quality_gate="both inputs must carry the same scope tag; a whole-entity revenue over a "
               "Page 700 barrel count is refused",
  role="conditional", adapter="liquids_xbrl")

M(id="liq_opex_per_barrel", display="Operating expense per barrel",
  meaning="Matched operating expense divided by matched barrels.",
  templates=(T_LIQ,), regimes=((F6, "annual"),),
  scope="requires matching scope on both inputs", unit_rule="USD per barrel",
  selector="derived_ratio", derivation="ratio(liq_operating_expenses, liq_barrels_delivered)",
  dependencies=("liq_operating_expenses", "liq_barrels_delivered"),
  quality_gate="scope compatibility checked before publication",
  role="conditional", adapter="liquids_xbrl")

M(id="liq_form6_filing_completeness", display="Form 6 filing completeness",
  meaning="Whether the carrier filed a FULL Form 6 or a reduced-schedule filing. "
          "The presence of Page 700 alone does NOT prove a full Form 6.",
  concept="FormType",
  templates=(T_LIQ,), regimes=((F6, "annual_observation"),),
  scope="filing-level attribute", unit_rule="categorical", selector="schedule_census",
  schedule="Form 6 entry point vs filed schedules",
  quality_gate="determined by comparing filed schedules against the form's taxonomy, "
               "not by the presence of any single page",
  role="detail", adapter="liquids_xbrl")

M(id="liq_oil_pipeline_index_factor",
  display="FERC Oil Pipeline Index - ceiling multiplier",
  meaning="The multiplier 18 CFR 342.3(d) directs oil pipelines to apply to the previous "
          "index year's rate ceiling to obtain the next one. A DIMENSIONLESS ratio of "
          "ceiling to ceiling, never a percent. It is an industry-wide regulatory ceiling "
          "adjustment, NOT a commodity oil price and NOT any carrier's actual tariff "
          "change.",
  templates=(T_LIQ,), regimes=((OIL_INDEX, "interval"),),
  scope="industry-wide oil pipeline rate-ceiling adjustment, not a carrier rate",
  unit_rule="multiplier", selector="published_index_notice",
  schedule="Notice of Annual Change in the PPI-FG, Docket No. RM93-11-000",
  quality_gate="the index year runs 1 July to 30 June; a superseded factor is retained "
               "with its own effective date and never overwritten",
  role="detail", adapter="oil_index")

M(id="liq_oil_pipeline_index_change",
  display="FERC Oil Pipeline Index - annual change",
  meaning="The index figure FERC publishes as 'the percent change (expressed as a "
          "decimal)'. Stored as a fraction; displayed as a percent. Equals factor - 1 "
          "and is never interchangeable with the factor.",
  templates=(T_LIQ,), regimes=((OIL_INDEX, "interval"),),
  scope="industry-wide oil pipeline rate-ceiling adjustment, not a carrier rate",
  unit_rule="percent", selector="published_index_notice",
  schedule="Notice of Annual Change in the PPI-FG, Docket No. RM93-11-000",
  quality_gate="read only from each notice's explicit published change; it is NEVER "
               "back-derived from the factor, including for anomaly/reconsideration years",
  role="detail", adapter="oil_index")

# ============================================================ IOC / capacity
M(id="ioc_firm_transport_mdq", display="Firm transportation MDQ",
  meaning="Sum of eligible D-record transportation MDQ at its contract/service grain, "
          "at the header snapshot date. Not physical capacity and not firm revenue.",
  templates=(T_GAS, T_STORAGE), regimes=((IOC, "snapshot"),),
  scope="contracted firm transportation, filer-reported", unit_rule="Dth/day",
  selector="ioc_d_records", schedule="Form 549B Index of Customers, D records",
  quality_gate="snapshot date taken from the header field, not the report date; "
               "contracts aggregated at their own grain before summing",
  role="headline", adapter="ioc"),

M(id="ioc_contracted_storage_quantity", display="Contracted storage quantity",
  meaning="The quantity the pipeline is OBLIGATED TO STORE. Despite the legacy MDQ "
          "caption on the field, this is NOT Dth/day and NOT a withdrawal capability.",
  templates=(T_STORAGE, T_GAS), regimes=((IOC, "snapshot"),),
  scope="contracted storage obligation", unit_rule="Dth (quantity, not per-day)",
  selector="ioc_d_records", schedule="Form 549B IOC storage quantity field",
  quality_gate="never added to transport MDQ, inventory capacity or daily withdrawal",
  role="headline", adapter="ioc"),

M(id="ioc_top5_shipper_concentration", display="Top-five shipper MDQ concentration",
  meaning="Share of matched MDQ held by the five largest LEGAL SHIPPERS, after "
          "aggregating each shipper's contracts. Not credit or revenue concentration.",
  templates=(T_GAS, T_STORAGE), regimes=((IOC, "snapshot"),),
  scope="share of the stated denominator only", unit_rule="percent",
  selector="ioc_aggregate_then_rank",
  quality_gate="explicit denominator published; identity coverage and unknown-identity "
               "share published alongside; contracts aggregated to the shipper first",
  dependencies=("ioc_firm_transport_mdq",), role="headline", adapter="ioc"),

M(id="ioc_expiry_profile", display="Primary-term expiry profile",
  meaning="MDQ by remaining primary term in 0-1, 1-2, 2-5, 5+ year buckets, with "
          "continuing-after-primary-term and unknown reported separately.",
  templates=(T_GAS, T_STORAGE), regimes=((IOC, "snapshot"),),
  scope="matched contracts only", unit_rule="Dth/day by bucket", selector="ioc_expiry_buckets",
  quality_gate="unknown-expiry MDQ share always shown; the continuation field describes a "
               "ROLLOVER PERIOD and is never treated as a guaranteed termination date",
  dependencies=("ioc_firm_transport_mdq",), role="headline", adapter="ioc"),

M(id="ioc_affiliate_share", display="Affiliate contract share",
  meaning="Share of matched MDQ held by contracts flagged as affiliate.",
  templates=(T_GAS, T_STORAGE), regimes=((IOC, "snapshot"),),
  scope="explicit denominator", unit_rule="percent", selector="ioc_flag_share",
  quality_gate="unknown-affiliate rows counted separately from non-affiliate",
  role="detail", adapter="ioc"),

M(id="ioc_identity_coverage", display="Shipper identity coverage",
  meaning="Share of matched MDQ whose shipper identity resolved to a stable ID.",
  templates=(T_GAS, T_STORAGE), regimes=((IOC, "snapshot"),),
  scope="diagnostic", unit_rule="percent", selector="ioc_coverage",
  quality_gate="IDs are filer-local unless evidence supports a wider identity",
  role="detail", adapter="ioc"),

M(id="ioc_mdq_change", display="MDQ change versus prior snapshot",
  meaning="Change in matched MDQ between two snapshots of the same filer.",
  templates=(T_GAS, T_STORAGE), regimes=((IOC, "snapshot"),),
  scope="same filer, consecutive snapshots", unit_rule="Dth/day", selector="ioc_snapshot_diff",
  quality_gate="contracts absent from an incomplete snapshot are NOT termination events; "
               "arrival of a revised file is separated from an economic change",
  dependencies=("ioc_firm_transport_mdq",), role="trend", adapter="ioc"),

M(id="ioc_points", display="Contract point and segment records",
  meaning="P-record receipt (M2), delivery (MQ) and segment endpoint (S8/S9) records "
          "retained for location detail.",
  templates=(T_GAS, T_STORAGE), regimes=((IOC, "snapshot"),),
  scope="location detail only", unit_rule="codes", selector="ioc_p_records",
  quality_gate="S8 is a SEGMENT endpoint, not a receipt code; no universal "
               "'P sum = 2 x D total' assertion; allocations only where type-aware and "
               "separately validated",
  role="detail", adapter="ioc"),

M(id="cap_reported_capacity", display="Reported capacity (annual capacity report)",
  meaning="Entity/route/storage capacity figures from the annual 549B capacity report, "
          "each with its own definition and directional scope.",
  templates=(T_GAS, T_STORAGE), regimes=((CAPRPT, "as_of"),),
  scope="as defined in the report; direction and season preserved",
  unit_rule="as reported", selector="document_span",
  schedule="Form 549B annual capacity report (due March 1)",
  quality_gate="definition, directional scope, unit, date, page evidence and confidence "
               "stored with every figure; a filer-specific value may be shown even when "
               "peer comparison is invalid",
  role="conditional", adapter="capacity"),

M(id="cap_peak_day_ratio", display="Peak-day delivery / deliverability ratio",
  meaning="Peak-day deliveries over reported peak-day deliverability.",
  templates=(T_GAS,), regimes=((CAPRPT, "as_of"),),
  scope="requires matching system scope, season/date, storage treatment and units",
  unit_rule="percent", selector="derived_ratio",
  dependencies=("single_day_peak_deliveries", "cap_reported_capacity"),
  quality_gate="attempted ONLY when all four scope dimensions align; never given a "
               "universal denominator; called peak-day, never annual, utilisation",
  role="gated",
  gate_reason="ungates only when the capacity report's definition, direction, season and "
              "unit are confirmed to match the p.518 peak scope for that filer",
  adapter="capacity"),

# ============================================================ Form 549D
M(id="i311_billed_transport_usage", display="Quarterly billed transportation usage",
  meaning="Billed transportation usage on FERC-reportable NGPA 311/Hinshaw services.",
  templates=(T_549D,), regimes=((F549D, "quarter"),),
  scope="FERC-reportable 311/Hinshaw services only - NOT the whole intrastate pipeline",
  unit_rule="as reported per service", selector="d549_usage",
  quality_gate="grouped by compatible service and unit; storage, injection, withdrawal and "
               "reservation determinants kept separate",
  role="headline", adapter="form549d"),

M(id="i311_storage_determinants", display="Storage / injection / withdrawal / reservation determinants",
  meaning="Separately scoped billing determinants that are not transportation usage.",
  templates=(T_549D,), regimes=((F549D, "quarter"),),
  scope="each determinant separately scoped", unit_rule="as reported",
  selector="d549_determinants",
  quality_gate="never merged into transportation usage or into one another",
  role="detail", adapter="form549d"),

M(id="i311_firm_share", display="Firm share of reported billed usage",
  meaning="Service mix. NOT the percentage of revenue protected by fixed fees.",
  templates=(T_549D,), regimes=((F549D, "quarter"),),
  scope="share of the stated billed activity", unit_rule="percent", selector="d549_share",
  dependencies=("i311_billed_transport_usage",),
  quality_gate="explicit denominator; unknown service type reported separately",
  role="headline", adapter="form549d"),

M(id="i311_top5_shipper_share", display="Top-five shipper share",
  meaning="Share of the specified billed activity held by the five largest shippers.",
  templates=(T_549D,), regimes=((F549D, "quarter"),),
  scope="share of the stated billed activity", unit_rule="percent",
  selector="d549_aggregate_then_rank",
  dependencies=("i311_billed_transport_usage",),
  quality_gate="stable filer-local identity and identity coverage required; "
               "contracts aggregated to the shipper before ranking",
  role="headline", adapter="form549d"),

M(id="i311_affiliate_activity", display="Affiliate activity",
  meaning="Billed activity flagged as affiliate, with unknown-affiliate kept separate.",
  templates=(T_549D,), regimes=((F549D, "quarter"),),
  scope="explicit denominator", unit_rule="percent", selector="d549_flag_share",
  quality_gate="unknown affiliate flags never merged into non-affiliate; blank revenue "
               "is not zero",
  role="detail", adapter="form549d"),

M(id="i311_contract_expiry", display="Contract primary-term expiry",
  meaning="Expiry distribution on an explicitly labelled weight.",
  templates=(T_549D,), regimes=((F549D, "quarter"),),
  scope="weight is named: contract count, billed usage or matched revenue",
  unit_rule="by weight", selector="d549_expiry",
  quality_gate="the weight is always stated; no 'earnings at risk' inferred from usage",
  role="conditional", adapter="form549d"),

M(id="i311_annual_transport_revenue", display="Annual reported transportation revenue",
  meaning="Annual customer revenue for transportation services, reported in the Q4 filing. "
          "Under Order 735-A this EXCLUDES storage services.",
  templates=(T_549D,), regimes=((F549D, "annual"),),
  scope="FERC-reportable 311/Hinshaw transportation services, excluding storage",
  unit_rule=USD, selector="d549_annual_revenue",
  quality_gate="revenue completeness measured over transportation records and the annual "
               "period; unreported storage revenue must NOT depress transportation "
               "coverage; annual revenue may exist for contracts with no Q4 usage",
  role="headline", adapter="form549d"),

M(id="i311_revenue_components", display="Annual revenue components",
  meaning="Reported revenue components compared with the reported total for the same "
          "year and service.",
  templates=(T_549D,), regimes=((F549D, "annual"),),
  scope="same-year, same-service components", unit_rule=USD, selector="d549_components",
  dependencies=("i311_annual_transport_revenue",),
  quality_gate="components reconciled to their own reported total, not to a computed sum",
  role="detail", adapter="form549d"),

M(id="i311_revenue_grain_diagnostic", display="Revenue aggregation diagnostic",
  meaning="Whether the reporting grain is resolved well enough to publish a revenue total.",
  templates=(T_549D,), regimes=((F549D, "annual"),),
  scope="diagnostic", unit_rule="categorical", selector="d549_grain_probe",
  quality_gate="NO 'same amount means duplicate' rule and NO 0.5% divergence threshold. "
               "If the grain is unresolved the aggregate is BLOCKED and alternative "
               "treatments are retained as audit scenarios, not as confidence bounds",
  role="detail", adapter="form549d"),

M(id="i311_component_rates", display="Reported component rates",
  meaning="Filed component rates with their descriptors, units and time bases.",
  templates=(T_549D,), regimes=((F549D, "quarter"),),
  scope="per descriptor; different unit/time bases stay separate", unit_rule="as reported",
  selector="d549_rates",
  quality_gate="validated on matching component/unit/time bases; annual revenue is NEVER "
               "divided by quarterly usage; no all-in tariff invented from missing rates",
  role="detail", adapter="form549d"),

M(id="i311_discounts_and_schedules", display="Discounts, rate schedules and PR dockets",
  meaning="Discounted-rate lists, rate schedules and the PR dockets that authorise them.",
  templates=(T_549D,), regimes=((F549D, "quarter"),),
  scope="per service", unit_rule="categorical", selector="d549_descriptors",
  quality_gate="rate authority applies to particular services; corrected filings are kept "
               "distinct from changed trading activity",
  role="detail", adapter="form549d"),

M(id="i311_points_decoded", display="Decoded receipt and delivery points",
  meaning="Point codes joined to separately filed cross-reference records where the "
          "evidence supports the join.",
  templates=(T_549D,), regimes=((F549D, "quarter"),),
  scope="per contract", unit_rule="codes", selector="d549_points",
  quality_gate="unknown proprietary codes remain codes; no invented names",
  role="detail", adapter="form549d"),

M(id="i311_reporting_state", display="Reporting state",
  meaning="Whether the respondent filed activity or declared no reportable activity.",
  templates=(T_549D,), regimes=((F549D, "quarter"),),
  scope="filing-level", unit_rule="categorical", selector="d549_state",
  quality_gate="'No reportable activity declared' is a valid filing state and is never "
               "displayed as 'no filing' or 'pipeline inactive'; missing rows do not "
               "prove contracts terminated",
  role="detail", adapter="form549d"),

# ============================================================ rates / regulation
M(id="rate_case_status", display="General rate case status",
  meaning="Current stage of the entity's general rate proceeding, from order text.",
  templates=(T_GAS, T_STORAGE, T_LIQ, T_549D), regimes=((DOC, "as_of"),),
  scope="the named docket only", unit_rule="categorical", selector="document_span",
  quality_gate="filings, accepted/suspended rates, settlement approval, rate effectiveness "
               "and refund completion are DISTINCT states; an explicitly sourced legal "
               "date is marked differently from a stage inferred from metadata; the most "
               "recent filing in a docket does not supersede every earlier order",
  role="headline", adapter="elibrary_docs"),

M(id="rate_effective_date", display="Rate effective date",
  meaning="The date on which rates became or become effective, from order text.",
  templates=(T_GAS, T_STORAGE, T_LIQ, T_549D), regimes=((DOC, "as_of"),),
  scope="the named docket", unit_rule="(date)", selector="document_span",
  quality_gate="requires order text; metadata inference alone is insufficient",
  role="detail", adapter="elibrary_docs"),

M(id="refund_exposure_window", display="Refund exposure window",
  meaning="The period over which collections may be subject to refund. Identifies the "
          "WINDOW only.",
  templates=(T_GAS, T_STORAGE, T_LIQ), regimes=((DOC, "as_of"),),
  scope="the named docket", unit_rule="(date range)", selector="document_span",
  quality_gate="NO dollar amount is published without a sourced calculation of the "
               "affected rates and volumes",
  role="conditional", adapter="elibrary_docs"),

M(id="tariff_operative_rate", display="Operative tariff rate",
  meaning="A legally operative numeric rate.",
  templates=(T_GAS, T_LIQ), regimes=((DOC, "as_of"),),
  scope="per rate schedule and service", unit_rule="as filed", selector="document_span",
  quality_gate="published ONLY when record, scope, effective status and the relevant "
               "orders reconcile; otherwise the retrieved source values are retained with "
               "an explicit unresolved-operative-rate status",
  role="gated",
  gate_reason="ungates per filer when a tariff record's effective status and the governing "
              "order are both retrieved and reconciled",
  adapter="elibrary_docs"),

# ============================================================ LNG
M(id="lng_liquefaction_capacity", display="Authorised liquefaction capacity",
  meaning="Liquefaction/export capacity authorised by a FERC order, with its exact basis.",
  templates=(T_LNG,), regimes=((DOC, "as_of"),),
  scope="the authorised facility named in the order; never a terminal-wide total copied "
        "onto a train group", unit_rule="as stated in the order",
  selector="document_span",
  quality_gate="order basis and units cited; never substituted by a regasification or "
               "send-out figure; not actual output; not an export licence",
  role="headline", adapter="lng"),

M(id="lng_regas_sendout_capacity", display="Authorised regasification / send-out capacity",
  meaning="Regasification or send-out capacity authorised by a FERC order.",
  templates=(T_LNG,), regimes=((DOC, "as_of"),),
  scope="the authorised facility named in the order", unit_rule="as stated in the order",
  selector="document_span",
  quality_gate="kept strictly separate from liquefaction capacity",
  role="headline", adapter="lng"),

M(id="lng_storage_capacity", display="Authorised LNG storage",
  meaning="Authorised LNG storage capacity and its amendments.",
  templates=(T_LNG,), regimes=((DOC, "as_of"),),
  scope="shared-facility scope preserved; parent totals never duplicated onto each asset",
  unit_rule="as stated in the order", selector="document_span",
  quality_gate="amendments preserved as a sequence, not overwritten",
  role="profile", adapter="lng"),

#: FOUR states, not three (audit A03). The delivered registry declared
#: requested / authorised / operating, so a FERC order permitting the
#: introduction of hazardous fluids -- a construction and start-up step -- had
#: nowhere of its own to go and was recorded as authorisation to enter service.
#: Six such orders were misread that way, and w5-documents found two more the
#: audit had not listed: an "In-Service Proof of Concept Test Plan" and a
#: construction permission.
#:
#: One order may grant two states at once. 20200618-3040 authorises BOTH
#: commissioning and service for the East Jetty, and it must produce both --
#: which is only expressible once commissioning is a state rather than a
#: synonym for service.
for _mid, _disp, _mean in [
        ("lng_status_requested", "Facility status: requested",
         "An application or request has been filed. A request decides nothing and is "
         "never an approval."),
        ("lng_status_commissioning", "Facility status: commissioning authorised",
         "FERC has permitted the introduction of hazardous fluids, feed gas or "
         "refrigerants into a named component. This is a construction/start-up step. "
         "It is NOT authorisation to enter service and NOT evidence of operation."),
        ("lng_status_authorised", "Facility status: authorised to enter service",
         "FERC has authorised entry into service. This is NOT proof of an actual start."),
        ("lng_status_operating", "Facility status: operation reported",
         "Operation has been reported to FERC. This is NOT implied by an authorisation.")]:
    M(id=_mid, display=_disp, meaning=_mean, templates=(T_LNG,), regimes=((DOC, "as_of"),),
      scope=("the named component of the named facility, and the docket"
             if _mid == "lng_status_commissioning" else "the named facility and docket"),
      unit_rule="categorical", selector="document_span",
      quality_gate="the four states -- requested, commissioning authorised, service "
                   "authorised, operation reported -- are four separate assertions and "
                   "never substitute for one another. One order may grant two of them; "
                   "that order produces both states, not one.",
      role="headline", adapter="lng")

M(id="lng_operational_report", display="Latest operational report",
  meaning="The latest operational report period and filing, plus the facility-specific "
          "reporting obligation that requires it.",
  templates=(T_LNG,), regimes=((DOC, "as_of"),),
  scope="the named facility; joint filer and docket associations retained",
  unit_rule="(period)", selector="document_span",
  quality_gate="the obligation is verified from the facility's own FERC order. The generic "
               "284.126(g) semi-annual storage-report Class/Type is NOT proof of an LNG "
               "reporting obligation",
  role="detail", adapter="lng"),

M(id="lng_inspection", display="Latest inspection and follow-up",
  meaning="The latest inspection, extracted outcomes, follow-up evidence and any "
          "unresolved conditions.",
  templates=(T_LNG,), regimes=((DOC, "as_of"),),
  scope="the named facility", unit_rule="categorical | (date)", selector="document_span",
  quality_gate="if only metadata were read, the outcome is recorded as NOT EXTRACTED; no "
               "closure or compliance status is invented. The actual inspection date is "
               "a date-valued capability; discovery, findings, actions and closure are "
               "categorical capabilities and remain distinct in scope.",
  role="detail", adapter="lng"),

M(id="lng_material_order", display="Material order, amendment or operating restriction",
  meaning="A decision affecting the facility, described by what it decided.",
  templates=(T_LNG,), regimes=((DOC, "as_of"),),
  scope="the named facility and docket", unit_rule="categorical", selector="document_span",
  quality_gate="described as a decision, not merely as 'a new CP filing'; one shared "
               "filing links to several assets without duplicating capacities or events",
  role="headline", adapter="lng"),

# ============================================================ events
M(id="event_reportable_interruption", display="Reportable service interruption",
  meaning="An interruption reported to FERC under the applicable reporting rule.",
  templates=(T_GAS,), regimes=((DOC, "as_of"),),
  scope="Form 576 scope is INTERSTATE GAS, not all gas", unit_rule="categorical",
  selector="document_span",
  quality_gate="FERC-only interruption reports are not comprehensive availability "
               "statistics; classification route must be verified, and zero local records "
               "is not evidence that no source exists",
  role="detail", adapter="elibrary_docs"),

M(id="event_replacement_report", display="Facility replacement report",
  meaning="A filed replacement report.",
  templates=(T_GAS,), regimes=((DOC, "as_of"),),
  scope="the named facility", unit_rule="categorical", selector="document_span",
  quality_gate="a routine replacement report is NOT maintenance-capex data and does not "
               "by itself establish financial materiality",
  role="detail", adapter="elibrary_docs"),



# Verified Form 6 concept local-names, confirmed against live Magellan/Saddlehorn
# filings (60 of 71 candidate concepts carried a real fact; the rest stay unset
# rather than guessed). Applied after construction because several of these
# metrics are built by the loop tables above.
LIQUIDS_CONCEPTS = {
    "liq_trunk_revenue": "TrunkRevenues",
    "liq_delivery_revenue": "DeliveryRevenues",
    "liq_allowance_revenue": "AllowanceOilRevenue",
    "liq_incidental_revenue": "IncidentalRevenue",
    "liq_carrier_property": "CarrierProperty",
    "liq_noncarrier_property": "NoncarrierProperty",
    "liq_accum_depreciation": "AccruedDepreciationCarrierProperty",
    "liq_gross_additions": "CarrierPropertyExcludingUndividedJointInterestProperty",
    "liq_miles_of_pipeline": "MilesOfTrunkLinesForProductsOperated",
    "p700_interstate_operating_revenue": "InterstateOperatingRevenues",
    "p700_throughput_barrels": "NumberOfBarrelsReceivedAndDeliveredOutInterstate",
    "p700_barrel_miles": "NumberOfBarrelMilesReceivedAndDeliveredOutInterstate",
    "p700_total_cost_of_service": "CostOfService",
    "p700_original_cost_rate_base": "OriginalCostIncludedInRateBase",
    "p700_trended_original_cost_rate_base": "TrendedOriginalCostRateBase",
    "p700_return_component": "ReturnOnRateBase",
    "p700_income_tax_allowance": "IncomeTaxAllowance",
    "p700_depreciation_regulatory": "DepreciationExpense",
    "p700_wacc": "WeightedAverageCostOfCapitalRateOfReturn",
    "p700_capital_structure_debt": "AdjustedCapitalStructureRatioForLongTermDebtRateOfReturn",
    "p700_capital_structure_equity": "AdjustedCapitalStructureRatioForStockholdersEquityRateOfReturn",
}

for _mid, _concept in LIQUIDS_CONCEPTS.items():
    _m = next((x for x in REGISTRY if x.id == _mid), None)
    if _m is not None and not _m.concept:
        _m.concept = _concept

# ---------------------------------------------------------------- lookups

BY_ID = {m.id: m for m in REGISTRY}
BY_TEMPLATE: dict[str, list[Metric]] = {}
for _m in REGISTRY:
    for _t in _m.templates:
        BY_TEMPLATE.setdefault(_t, []).append(_m)
BY_ADAPTER: dict[str, list[Metric]] = {}
for _m in REGISTRY:
    BY_ADAPTER.setdefault(_m.adapter, []).append(_m)
BY_CONCEPT: dict[str, list[Metric]] = {}
for _m in REGISTRY:
    if _m.concept:
        BY_CONCEPT.setdefault(_m.concept, []).append(_m)


# ==========================================================================
# THE UNIT CONTRACT                                            (audit A04)
# ==========================================================================
#
# The independent check of 8 September found 1,251 margin observations storing
# a FRACTION while declaring the unit `percent`, and 55 revenue-per-barrel
# observations declaring `ratio` with no dimension at all. A consumer rendering
# those margins would show them 100 times too small.
#
# The root cause was not arithmetic -- the auditor ran 2,109 derived values
# through Decimal and found zero mismatches. It was that the unit was decided
# by a hardcoded ternary in the derivation engine instead of being declared by
# the metric, so the stored number and its stated unit were free to disagree.
#
# So the unit is now a declared, machine-checkable property of the metric:
#
#   canonical_unit  what the STORED number is in. The single source of truth.
#   display_unit    what a consumer should render, via exactly one conversion.
#   display_scale   that conversion's factor, declared here and nowhere else.
#
# Two conventions coexist deliberately, and each metric says which it uses:
#
#   * DERIVED percentages are stored as percent-valued numbers (0.546861 is
#     stored as 54.6861 with unit `percent`). The value is ours to define, so
#     we define it to agree with its name and its unit.
#   * FILED fractions stay exactly as FERC filed them. p700_wacc arrives as
#     xbrli:pure 0.0913 and is stored as 0.0913, because the contract forbids
#     changing a raw source value. Its registry entry previously claimed
#     unit_rule="percent", which was wrong in the same way A04 is wrong; it now
#     declares canonical_unit="fraction" with an explicit display conversion.
#
# Uniformity was rejected in favour of per-metric declaration precisely because
# a single blanket convention is what let a fraction masquerade as a percent.

#: Dimensional families and the scale of each spelling relative to its family's
#: base unit. Built from the 68 distinct unit tokens actually stored by the
#: delivered run, so it describes FERC's real vocabulary rather than an idealised
#: one. Consumed by ferclib/coverage.py (A06): a wrong-unit substitute must fail
#: to satisfy a slot.
#:
#: ENERGY AND VOLUME ARE DELIBERATELY SEPARATE FAMILIES. A dekatherm measures
#: energy and a cubic foot measures gas volume; they are related only through a
#: heat content that varies by stream and is not ours to assume. Merging them
#: would silently license exactly the comparison this table exists to refuse.
#:
#: `scale` converts a value to the family's base unit. Admissibility is decided
#: by FAMILY; comparability additionally requires the scale, which is why the two
#: are recorded separately rather than collapsed into one test.
#:                      family: {spelling(lowercased): scale to base}
UNIT_FAMILIES: dict[str, dict[str, float]] = {
    # base USD
    "currency":     {"iso4217:usd": 1.0, "usd": 1.0},
    # base USD per barrel
    "currency_rate": {"usd/bbl": 1.0, "usd/barrel": 1.0, "usd per barrel": 1.0},
    # base percent (0-100)
    "percent":      {"percent": 1.0},
    # base fraction (0-1). `ratio` is dimensionless but NOT a percent.
    "fraction":     {"fraction": 1.0, "pure": 1.0, "xbrli:pure": 1.0, "ratio": 1.0},
    # A MULTIPLIER is dimensionless like a fraction but is a different quantity
    # and a separate family on purpose. The FERC Oil Pipeline Index publishes
    # both: the multiplier 1.014290 that 18 CFR 342.3(d) directs carriers to
    # apply to the previous ceiling, and the change 0.014290 that FERC calls
    # "the percent change (expressed as a decimal)". They differ by exactly 1.
    # Keeping them in one family would let a slot expecting the change be
    # satisfied by the factor, which is the A04 error in a new dress.
    "multiplier":   {"multiplier": 1.0, "index_factor": 1.0},
    # base Dth. 1 Dth == 1 MMBtu by the standard gas convention.
    # MMDth and BBTU are how storage capacity is stated on the capacity reports:
    # a Plymouth or Jackson Prairie working-gas volume is millions of dekatherms,
    # not dekatherms. Both were absent, so those figures could only be gated --
    # and the gate was doing its job, since publishing 55.75 as though it were
    # 55.75 Dth would be wrong by six orders of magnitude. Added on
    # w5-documents' evidence, with the scale stated so the magnitude is explicit
    # rather than assumed. 1 BBTU = 1,000 MMBtu = 1,000 Dth.
    "energy":       {"utr:dth": 1.0, "ferc:dth": 1.0, "dth": 1.0, "mdth": 1e3,
                     "mmdth": 1e6, "mmbtu": 1.0, "mmbtu|dth": 1.0,
                     "bbtu": 1e3},
    # base Dth/day
    "energy_rate":  {"dth/day": 1.0, "dth/d": 1.0, "mdth/d": 1e3, "mdth/day": 1e3,
                     "mmbtu/day": 1.0, "mmbtu/d": 1.0},
    # base cubic foot -- VOLUME, never interchangeable with energy above.
    # `mcf` is latent: no filer in the current universe reports IOC item g as
    # `F`, but the code path exists and an unrecognised unit refuses a match, so
    # the day one does the value would silently fail to satisfy its slot.
    # Added on w2-ioc's report of the gap rather than waiting for the failure.
    "volume":       {"mcf": 1e3, "mmcf": 1e6, "mmscf": 1e6, "bcf": 1e9,
                     "million cubic feet": 1e6, "billion cubic feet": 1e9},
    "volume_rate":  {"mmcf/day": 1e6, "mmcf/d": 1e6, "mmscf/day": 1e6,
                     "mcf/day": 1e3, "bcf/day": 1e9},
    # base cubic metre -- a third, separate volumetric system
    "volume_metric": {"cubic meter": 1.0, "cubic meters": 1.0},
    # base barrel. `ferc:bbl` is the migrated-era spelling of `utr:bbl`, the same
    # pair `energy` already carries for utr:dth / ferc:dth.
    "liquid_volume": {"utr:bbl": 1.0, "ferc:bbl": 1.0, "bbl": 1.0, "barrel": 1.0,
                      "mbbl": 1e3},
    # base tonne per annum
    "mass_rate":    {"mtpa": 1e6, "mmtpa": 1e6, "million tons per annum": 1e6,
                     "million metric tons per annum": 1e6},
    "distance":     {"utr:mi": 1.0, "mile": 1.0, "miles": 1.0},
    "length":       {"inches": 1.0},
    "work":         {"barrel-miles": 1.0, "utr:bblmi": 1.0},
    # utr:MW is what FERC's tag says for certificated horsepower; the rendered
    # column is horsepower. The mismatch is a documented source warning carried
    # on the observation -- it is recorded here, not silently converted.
    "power":        {"hp": 1.0, "horsepower": 1.0, "utr:hp": 1.0, "utr:mw": 1.0,
                     "units / horsepower": 1.0},
    "count":        {"count": 1.0, "records": 1.0, "tanks": 1.0},
    "categorical":  {"categorical": 1.0, "codes": 1.0, "text": 1.0,
                     "tariff record": 1.0},
    "date":         {"(date)": 1.0, "(date range)": 1.0, "(period)": 1.0},
}

#: Units that are honest as-filed descriptions rather than dimensional units.
#:
#: These are a real category, not a failure. Some FERC fields genuinely have no
#: fixed dimension: Form 549D states its billing determinants per service, and
#: whichever determinant the filer used IS the unit. A metric whose `unit_rule`
#: is itself an as-filed phrase ("as reported", "as filed", "by weight") admits
#: this family -- and admitting it means only that the value is accepted at its
#: requested grain, NEVER that it has been dimensionally checked, because there
#: is nothing to check it against.
UNDIMENSIONED_UNITS = frozenset({
    "as reported per descriptor", "as reported per determinant", "as filed",
    "as stated", "(unit not reported)", "(operating data)", "(order condition)",
    "(statement)", "(period)", "(date)", "(date range)",
})

#: `unit_rule` phrases that declare the metric as-filed rather than dimensional.
_AS_FILED_RULES = frozenset({
    "as reported", "as filed", "as stated", "by weight", "as reported per service",
    "as reported per determinant", "as stated in the order",
    "as stated in the narrative", "(period)", "(date)", "(date range)",
    "(date - no unit)",
})
AS_FILED_FAMILY = "as_filed"

_UNIT_TO_FAMILY: dict[str, str] = {}
_UNIT_SCALE: dict[str, float] = {}
for _fam, _spellings in UNIT_FAMILIES.items():
    for _u, _sc in _spellings.items():
        _UNIT_TO_FAMILY[_u] = _fam
        _UNIT_SCALE[_u] = _sc


def _norm_unit(unit: str | None) -> str:
    return (unit or "").strip().lower()


def _strip_gloss(token: str) -> str:
    """Drop a trailing parenthetical gloss from one alternative in a unit rule.

    `unit_rule` alternatives carry explanations -- "pure (rendered p.512-513
    supplies Dth)" -- and the gloss made the alternative unresolvable, so a rule
    that explicitly admits `pure` was read as admitting only `utr:dth`. The
    result was 55 present observations refused for carrying a unit their own
    metric declares acceptable. The gloss is documentation; the token before it
    is the unit.
    """
    t = token.strip()
    # Only strip a gloss that FOLLOWS a token. Some rules ARE a parenthesised
    # phrase -- "(date)", "(period)", "(date range)" -- and splitting those on
    # "(" leaves the empty string, which resolves to no family and refused 60
    # perfectly good date-valued observations.
    if "(" in t and not t.startswith("("):
        t = t.split("(", 1)[0].strip()
    return t.lower()


def unit_family_of(unit: str | None) -> str:
    """The dimensional family of a stored unit, or '' when it has none.

    '' is returned both for an unrecognised unit and for a recognised but
    undimensioned one. Either way it must NOT be treated as compatible with
    anything: an unknown unit is a reason to refuse a match, never a wildcard.
    """
    u = _norm_unit(unit)
    if not u:
        return ""
    if u in _UNIT_TO_FAMILY:
        return _UNIT_TO_FAMILY[u]
    # A few STORED units state two acceptable spellings joined by a pipe
    # ("MMBtu|Dth", 173 observations). Both must resolve and both must agree.
    #
    # The earlier version discarded alternatives that did not resolve and
    # returned the family of whatever was left, which w4-coverage caught: a
    # caller passing a whole prose `unit_rule` such as
    # "utr:dth | pure (rendered p.512-513 supplies Dth)" got back `energy`,
    # silently dropping the `pure` variant FERC actually files. A partially
    # understood unit is not a unit, and answering anyway is the failure mode
    # this module exists to prevent -- so an unresolved alternative now makes
    # the whole token unknown. Callers that legitimately need to reason about
    # alternatives use unit_family_alternatives() and handle the set themselves.
    if "|" in u:
        fams = {_UNIT_TO_FAMILY.get(p.strip(), "") for p in u.split("|")}
        if "" not in fams and len(fams) == 1:
            return fams.pop()
    return ""


def unit_family_alternatives(unit_rule: str | None) -> list[str]:
    """Every family a prose `unit_rule` admits, in order, '' for unresolvable.

    `unit_rule` is prose describing what a source MAY supply, sometimes naming
    several acceptable vocabularies. Use this when you must reason about those
    alternatives; never use unit_family_of() on a whole rule, which answers only
    when the rule is unambiguous and returns '' otherwise.
    """
    if not unit_rule:
        return []
    out = []
    for part in str(unit_rule).split("|"):
        tok = _strip_gloss(part)
        if tok in _AS_FILED_RULES or tok in UNDIMENSIONED_UNITS:
            out.append(AS_FILED_FAMILY)
        else:
            out.append(_UNIT_TO_FAMILY.get(tok, ""))
    return out


def unit_scale_of(unit: str | None) -> float | None:
    """Factor converting a value to its family's base unit, or None if unknown."""
    u = _norm_unit(unit)
    if u in _UNIT_SCALE:
        return _UNIT_SCALE[u]
    if "|" in u:
        parts = [p.strip() for p in u.split("|")]
        scales = {_UNIT_SCALE[p] for p in parts if p in _UNIT_SCALE}
        if len(scales) == 1:
            return scales.pop()
    return None


def units_compatible(a: str | None, b: str | None) -> bool:
    """True only when both units are recognised and share a dimensional family.

    Admissibility, not comparability: two units in one family may still differ in
    magnitude (Dth vs MDth). Use unit_scale_of() before comparing magnitudes.
    """
    fa = unit_family_of(a)
    return bool(fa) and fa == unit_family_of(b)


def known_unit(unit: str | None) -> bool:
    """Whether the token is one we recognise at all -- dimensional or explicitly
    undimensioned. An unrecognised token is a data-quality signal, not a unit."""
    u = _norm_unit(unit)
    if not u:
        return False
    # Form 549D states its billing determinants as a sentence naming the source
    # columns ("contract rows and Usage_BU, both stated"). That is an honest
    # as-filed descriptor, not a dimensional unit, so it is recognised but stays
    # outside every family and can satisfy no dimensional slot.
    if u.startswith("contract rows and ") and u.endswith(", both stated"):
        return True
    return (u in _UNIT_TO_FAMILY or u in UNDIMENSIONED_UNITS
            or unit_family_of(u) != "")


#: Explicit canonical/display units. Every metric whose unit could be read two
#: ways MUST appear here; the check below refuses to import the module if one is
#: missing, so a new ambiguous metric cannot be added silently.
#:                     metric_id: (canonical_unit, display_unit, display_scale)
_UNIT_CONTRACT: dict[str, tuple[str, str, float]] = {
    # derived percentages -- stored percent-valued, so name, value and unit agree
    "operating_margin_pct":            ("percent", "percent", 1.0),
    "liq_operating_margin_pct":        ("percent", "percent", 1.0),
    "cap_peak_day_ratio":              ("percent", "percent", 1.0),
    # already correct in the adapters that produce them; declared so the check
    # covers them and a future change cannot quietly alter the convention
    "ioc_top5_shipper_concentration":  ("percent", "percent", 1.0),
    "ioc_affiliate_share":             ("percent", "percent", 1.0),
    "ioc_identity_coverage":           ("percent", "percent", 1.0),
    "i311_firm_share":                 ("percent", "percent", 1.0),
    "i311_top5_shipper_share":         ("percent", "percent", 1.0),
    "i311_affiliate_activity":         ("percent", "percent", 1.0),
    # FILED fractions -- never rescaled; the source value is not ours to change
    "p700_wacc":                       ("fraction", "percent", 100.0),
    "p700_capital_structure_debt":     ("fraction", "percent", 100.0),
    "p700_capital_structure_equity":   ("fraction", "percent", 100.0),
    # genuinely dimensionless
    "p700_revenue_to_cost_ratio":      ("fraction", "ratio", 1.0),
    # dimensional rates -- the engine previously flattened these to "ratio"
    "liq_revenue_per_barrel":          ("USD/bbl", "USD/bbl", 1.0),
    "liq_opex_per_barrel":             ("USD/bbl", "USD/bbl", 1.0),
    # THE FERC OIL PIPELINE INDEX (A18), as two metrics rather than one.
    #
    # My first attempt declared a single metric storing the multiplier 1.014290
    # with display_scale=100, which renders 101.4290% -- not the +1.4290% it
    # claimed. That is the A04 defect reproduced in a new metric, and
    # w3-financial caught it with the dimensional analysis below.
    #
    # FERC publishes two quantities in each annual notice and they differ by
    # exactly 1 (Notice of Annual Change in the PPI-FG, Docket RM93-11-000;
    # footnote: "1 + 0.014290 = 1.014290"):
    #
    #   the MULTIPLIER 1.014290 -- a dimensionless ratio of new ceiling to old
    #     ceiling, which 18 CFR 342.3(d) directs carriers to apply. Reading it
    #     as a percent would mean a 1.01% ceiling, which is nonsense.
    #   the CHANGE 0.014290 -- what FERC calls "the percent change (expressed as
    #     a decimal)", a fraction displayed as +1.4290%.
    #
    # The multiplier is operative, the change is display. Neither substitutes
    # for the other, so one metric cannot carry both.
    "liq_oil_pipeline_index_factor":   ("multiplier", "multiplier", 1.0),
    "liq_oil_pipeline_index_change":   ("fraction", "percent", 100.0),
}

#: metrics that must carry an explicit contract because their unit is ambiguous
_AMBIGUOUS_UNIT_RULES = {"percent", "ratio", "USD per barrel", "pure", "xbrli:pure"}

#: Units FERC actually files that are not a metric's canonical dimension.
#: Each entry is a factual claim about the source and carries its evidence.
#:                          metric_id: ((units...), evidence)
_ADMISSIBLE_UNITS: dict[str, tuple[tuple[str, ...], str]] = {
    # Barrel-miles. w3-financial checked the Form 6 taxonomy and found it
    # declares NO barrel-mile unit at all, so filers tag the figure untyped.
    # That makes `xbrli:pure` universal here -- 19/19 and 28/28 entities -- and
    # not a reduced-schedule carve-out, which is why the wording matters.
    #
    # `utr:bbl` is deliberately NOT admitted. Two observations carry it, and
    # they are barrel-MILE concepts wearing a BARRELS unit with values inside
    # the untyped range: a real dimensional error that currently passes
    # validation. Admitting every unit observed would turn coverage green by
    # cementing the defect, which is precisely what rule 6 forbids. Those two
    # stay refused so the error stays visible.
    "liq_barrel_miles": (
        ("xbrli:pure", "pure"),
        "FERC's Form 6 taxonomy declares no barrel-mile unit, so the figure is "
        "filed untyped by every filer (19/19 entities). utr:bbl is excluded: the "
        "2 observations carrying it are a barrel-mile concept tagged with a "
        "barrels unit, a dimensional error that must stay visible."),
    "p700_barrel_miles": (
        ("xbrli:pure", "pure"),
        "As liq_barrel_miles; untyped on Page 700 for 28/28 entities."),
    # The IOC expiry profile carries BOTH contracted transport MDQ, which is a
    # rate (Dth/day), and contracted storage quantity, which is not (Dth). The
    # repair contract requires those two to stay distinct, and they do -- but the
    # distinction lives in the row's SCOPE, not in its unit, so a single
    # dimensional family cannot express it. The registry rule said only
    # "Dth/day by bucket", naming the transport case alone, which refused 965
    # legitimate storage rows. Both are admitted here; keeping them apart remains
    # the adapter's job and the unit gate is not the place it happens.
    "ioc_expiry_profile": (
        ("Dth/day", "Dth/d", "MDth/d", "Mdth/day", "MMBtu/day", "MMBtu/d",
         "Dth", "MDth", "MMBtu"),
        "FERC's Index of Customers profiles transport MDQ (a per-day rate) and "
        "storage quantity (not per-day) in one report; the two are distinguished "
        "by scope, not by unit."),
}

#: `unit_rule` is PROSE describing the source vocabulary a metric may arrive in
#: ("utr:dth | ferc:dth (migrated-era unit vocabulary)"). It is deliberately NOT
#: promoted to canonical_unit: only an explicit declaration counts, so an
#: undeclared metric's stored unit remains whatever the source supplied and is
#: judged by unit_family_of() rather than by a sentence.
for _m in REGISTRY:
    if _m.id in _UNIT_CONTRACT:
        _m.canonical_unit, _m.display_unit, _m.display_scale = _UNIT_CONTRACT[_m.id]
    if _m.id in _ADMISSIBLE_UNITS:
        _m.admissible_units, _m.admissible_units_evidence = _ADMISSIBLE_UNITS[_m.id]


def unit_admissible(metric_id: str, unit: str | None,
                    canonical_unit: str | None = None) -> tuple[bool, str]:
    """May an observation in `unit` answer a slot for `metric_id`?

    True when the unit shares the canonical dimensional family, OR when the
    metric explicitly declares it admissible with evidence. Returns the reason
    either way, so a refusal can be reported rather than merely counted.
    """
    m = BY_ID.get(metric_id)
    if m and unit and _norm_unit(unit) in {_norm_unit(u) for u in m.admissible_units}:
        return True, f"admitted by declared exception: {m.admissible_units_evidence}"
    if not unit:
        return False, "the observation carries no unit"
    got = unit_family_of(unit)
    if not got and known_unit(unit):
        # A recognised but undimensioned token. Legitimate only where the metric
        # itself declares an as-filed rule -- 549D's billing determinants really
        # are "whatever the filer used". Admitted, but never as a dimensional
        # check: there is nothing to check against, and the caller is told so.
        if AS_FILED_FAMILY in unit_family_alternatives(m.unit_rule if m else ""):
            return True, (f"as-filed unit {unit!r} admitted because the metric's "
                          "unit_rule is itself as-filed; NOT dimensionally verified")
        return False, (f"unit {unit!r} is a recognised as-filed descriptor, but "
                       f"{metric_id!r} declares a dimensional unit_rule "
                       f"{(m.unit_rule if m else '')!r}")
    if not got:
        return False, (f"unit {unit!r} belongs to no known dimensional family; an "
                       "unrecognised unit is never a wildcard")
    want = canonical_unit or (m.canonical_unit if m else "")
    if want:
        if units_compatible(unit, want):
            return True, f"unit family {got!r} matches the declared canonical unit"
        return False, (f"unit {unit!r} ({got}) does not match the declared canonical "
                       f"unit {want!r} ({unit_family_of(want) or 'no family'})")
    # No canonical unit declared, so fall back to the prose `unit_rule`, which may
    # name several acceptable source vocabularies. Resolve its ALTERNATIVES rather
    # than the whole string: asking unit_family_of() about a whole rule answers
    # only when the rule is unambiguous, and returns '' otherwise.
    families = [f for f in unit_family_alternatives(m.unit_rule if m else "") if f]
    if AS_FILED_FAMILY in families:
        # "as reported" means exactly that: the filer chooses the unit and we
        # cannot constrain it dimensionally. A capacity report states Dth/d,
        # MDth/d, MMcf/day or MMBtu/D according to the filer. Any RECOGNISED
        # unit is admitted; an unrecognised one still is not, because an
        # unparseable token is a data-quality signal rather than a filer's
        # choice. Admission here is never a dimensional verification.
        return True, (f"the metric's unit_rule is as-filed, so unit {unit!r} "
                      f"({got}) is admitted as the filer's own choice; NOT "
                      "dimensionally verified")
    if not families:
        return False, (f"metric {metric_id!r} declares no canonical unit and its "
                       f"unit_rule resolves to no family, so no unit can be admitted "
                       "without an explicit declaration")
    if got in families:
        return True, f"unit family {got!r} is among those the unit_rule admits"
    return False, (f"unit {unit!r} ({got}) is not among the families the unit_rule "
                   f"admits ({', '.join(sorted(set(families)))})")


def check_unit_contract() -> list[str]:
    """Every ambiguously-united metric must declare its contract explicitly.

    Import fails rather than letting an undeclared percent-or-fraction metric
    reach a consumer, which is precisely how A04 shipped.
    """
    problems = []
    for m in REGISTRY:
        if m.unit_rule in _AMBIGUOUS_UNIT_RULES and m.id not in _UNIT_CONTRACT:
            problems.append(
                f"{m.id}: unit_rule={m.unit_rule!r} is ambiguous (percent or fraction?) "
                "but the metric declares no canonical unit; add it to _UNIT_CONTRACT")
        if m.canonical_unit and not unit_family_of(m.canonical_unit):
            problems.append(f"{m.id}: canonical_unit={m.canonical_unit!r} "
                            "belongs to no declared unit family")
    return problems


_unit_problems = check_unit_contract()
if _unit_problems:                                    # fail loudly, at import
    raise AssertionError("registry unit contract violated:\n  " +
                         "\n  ".join(_unit_problems))


def concepts_for_adapter(adapter: str) -> list[str]:
    out: list[str] = []
    for m in BY_ADAPTER.get(adapter, []):
        if m.concept and m.concept not in out:
            out.append(m.concept)
        for a in m.aliases:
            if a["concept"] not in out:
                out.append(a["concept"])
    return out


def to_rows() -> list[dict]:
    """Flat export of the registry, for the acceptance pack."""
    rows = []
    for m in REGISTRY:
        rows.append({
            "metric_id": m.id, "display": m.display, "meaning": m.meaning,
            "templates": ";".join(m.templates),
            "regimes": ";".join(f"{r}:{b}" for r, b in m.regimes),
            "optional_regimes": ";".join(f"{r}:{b}" for r, b in m.optional_regimes),
            "concept_local": m.concept, "schedule": m.schedule, "scope": m.scope,
            "unit_rule": m.unit_rule, "selector": m.selector, "fallback": m.fallback,
            "derivation": m.derivation, "dependencies": ";".join(m.dependencies),
            "quality_gate": m.quality_gate, "role": m.role, "gate_reason": m.gate_reason,
            "implementation": m.implementation, "adapter": m.adapter,
            # Consumer-visible unit semantics. `unit_rule` is source-facing
            # prose and cannot substitute for the canonical stored unit or its
            # one declared display conversion.
            "canonical_unit": m.canonical_unit,
            "unit_family": unit_family_of(m.canonical_unit) if m.canonical_unit else "",
            "display_unit": m.display_unit,
            "display_scale": m.display_scale,
            "admissible_units": ";".join(m.admissible_units),
            "admissible_units_evidence": m.admissible_units_evidence,
            "registry_version": REGISTRY_VERSION, "notes": m.notes})
    return rows
