export const formatDate = (value?: string) => {
  if (!value) return 'Unavailable';
  const calendarDate = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  const date = calendarDate
    ? new Date(
        Date.UTC(
          Number(calendarDate[1]),
          Number(calendarDate[2]) - 1,
          Number(calendarDate[3]),
        ),
      )
    : new Date(value);
  if (Number.isNaN(date.getTime())) return 'Unavailable';
  return new Intl.DateTimeFormat('en-GB', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(date);
};

const comparisonUnits = {
  currency: { label: 'US dollars (USD)', valueUnit: 'iso4217:USD' },
  currency_rate: {
    label: 'US dollars per barrel (USD/bbl)',
    valueUnit: 'USD/bbl',
  },
  percent: { label: 'Percent', valueUnit: 'percent' },
  fraction: { label: 'Decimal ratio', valueUnit: null },
  multiplier: { label: 'Index multiplier', valueUnit: 'multiplier' },
  energy: { label: 'Dekatherms (Dth)', valueUnit: 'Dth' },
  energy_rate: { label: 'Dekatherms per day (Dth/day)', valueUnit: 'Dth/day' },
  volume: { label: 'Cubic feet (ft³)', valueUnit: 'ft³' },
  volume_rate: {
    label: 'Cubic feet per day (ft³/day)',
    valueUnit: 'ft³/day',
  },
  volume_metric: { label: 'Cubic metres (m³)', valueUnit: 'm³' },
  liquid_volume: { label: 'Barrels (bbl)', valueUnit: 'bbl' },
  mass_rate: { label: 'Metric tonnes per year', valueUnit: 't/year' },
  distance: { label: 'Miles', valueUnit: 'miles' },
  length: { label: 'Inches', valueUnit: 'inches' },
  work: { label: 'Barrel-miles', valueUnit: 'barrel-miles' },
  power: { label: 'Horsepower (hp)', valueUnit: 'hp' },
  count: { label: 'Count', valueUnit: null },
  categorical: { label: 'Categorical value', valueUnit: null },
  date: { label: 'Date', valueUnit: null },
} as const;

const metricPresentationUnits: Record<
  string,
  { label: string; valueUnit: string | null }
> = {
  certificated_horsepower: { label: 'Horsepower (hp)', valueUnit: 'hp' },
  compressor_units: {
    label: 'Compressor units',
    valueUnit: 'compressor units',
  },
  discounted_rate_volumes: {
    label: 'Dekatherms (Dth)',
    valueUnit: 'Dth',
  },
  liq_barrel_miles: { label: 'Barrel-miles', valueUnit: 'barrel-miles' },
  negotiated_rate_volumes: {
    label: 'Dekatherms (Dth)',
    valueUnit: 'Dth',
  },
  p700_barrel_miles: { label: 'Barrel-miles', valueUnit: 'barrel-miles' },
};

export const supportedComparisonUnitFamilies = new Set(
  Object.keys(comparisonUnits),
);

const unitsWithoutValueSuffix = new Set([
  '',
  '(date)',
  '(date range)',
  '(operating data)',
  '(order condition)',
  '(period)',
  '(statement)',
  'as filed',
  'as reported',
  'as reported per descriptor',
  'as reported per determinant',
  'as stated',
  'categorical',
  'codes',
  'count',
  'fraction',
  'pure',
  'records',
  'tariff record',
  'text',
  'xbrli:pure',
]);

const numericText = /^[+-]?(?:\d+(?:,\d{3})*|\d*\.\d+)(?:e[+-]?\d+)?$/i;

export const humanizeFercText = (value: string) => {
  const spaced = value
    .trim()
    .replaceAll('_', ' ')
    .replace(/\s*:\s*/g, ': ')
    .replace(/\s+/g, ' ');
  return spaced ? `${spaced[0].toUpperCase()}${spaced.slice(1)}` : spaced;
};

export const humanizeFercReason = (value: string) => {
  const rendered = humanizeFercText(value);
  return rendered && !/[.!?]$/.test(rendered) ? `${rendered}.` : rendered;
};

export const isStructuredFercValue = (value: unknown) => {
  if (typeof value !== 'string') return false;
  const trimmed = value.trim();
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return false;
  try {
    const parsed = JSON.parse(trimmed);
    return parsed !== null && typeof parsed === 'object';
  } catch {
    return false;
  }
};

const structuredValueLabel = (value: string) => {
  const parsed = JSON.parse(value) as unknown;
  if (Array.isArray(parsed)) {
    return `Structured record · ${parsed.length.toLocaleString('en-US')} ${parsed.length === 1 ? 'item' : 'items'}`;
  }
  const count = Object.keys(parsed as Record<string, unknown>).length;
  return `Structured record · ${count.toLocaleString('en-US')} ${count === 1 ? 'field' : 'fields'}`;
};

export const unitLabel = (unit: string | null | undefined) => {
  if (!unit) return '';
  const normalized = unit.trim().toLowerCase();
  if (normalized === 'iso4217:usd' || normalized === 'usd') return 'USD';
  if (
    normalized === 'usd/bbl' ||
    normalized === 'usd/barrel' ||
    normalized === 'usd per barrel'
  )
    return 'USD/bbl';
  if (normalized === 'utr:dth' || normalized === 'ferc:dth') return 'Dth';
  if (normalized === 'utr:bbl' || normalized === 'ferc:bbl') return 'bbl';
  if (normalized === 'utr:mi') return 'miles';
  if (normalized === 'utr:mw') return 'MW';
  if (normalized === 'dth/d') return 'Dth/day';
  if (normalized === 'mdth/d' || normalized === 'mdth/day') return 'MDth/day';
  if (normalized === 'mmbtu/d' || normalized === 'mmbtu/day')
    return 'MMBtu/day';
  if (normalized === 'mmbtu') return 'MMBtu';
  if (normalized === 'mmbtu|dth' || normalized === 'dth|mmbtu')
    return 'MMBtu or Dth (as reported)';
  if (normalized === 'mmcf/d' || normalized === 'mmcf/day') return 'MMcf/day';
  if (normalized === 'mmscf/d' || normalized === 'mmscf/day')
    return 'MMscf/day';
  if (normalized === 'cubic meter' || normalized === 'cubic meters')
    return 'm³';
  if (normalized === 'percent') return '%';
  if (normalized === 'ratio') return 'ratio';
  if (
    normalized === 'fraction' ||
    normalized === 'pure' ||
    normalized === 'xbrli:pure'
  )
    return 'Unitless';
  if (normalized === '(unit not reported)') return 'Unit not reported';
  return unit;
};

export const sourceUnitLabel = (unit: string | null | undefined) => {
  if (!unit) return '';
  const friendly = unitLabel(unit);
  return friendly && friendly !== unit ? `${unit} (${friendly})` : unit;
};

type PresentationContext = {
  configuredDisplayUnit?: string | null;
  displayScale?: number | null;
  origin?: string | null;
  validation?: string | null;
};

const isPureUnit = (unit: string | null | undefined) =>
  unit === 'pure' || unit === 'xbrli:pure';

export const isMetricPresentationBlocked = (
  metricId: string | null | undefined,
  filedOrDisplayUnit: string | null | undefined,
  validation: string | null | undefined,
) =>
  (metricId === 'liq_barrel_miles' || metricId === 'p700_barrel_miles') &&
  filedOrDisplayUnit?.trim().toLowerCase() === 'utr:bbl' &&
  validation === 'unit_warning';

const canUseMetricPresentationOverride = (
  metricId: string | null | undefined,
  filedOrDisplayUnit: string | null | undefined,
  context: PresentationContext,
) => {
  if (context.configuredDisplayUnit) return false;
  if (
    context.displayScale !== null &&
    context.displayScale !== undefined &&
    context.displayScale !== 1
  ) {
    return false;
  }
  const normalizedUnit = filedOrDisplayUnit?.trim().toLowerCase() || '';
  if (metricId === 'certificated_horsepower') {
    return normalizedUnit === 'utr:mw';
  }
  if (metricId === 'compressor_units') return isPureUnit(normalizedUnit);
  if (metricId === 'liq_barrel_miles' || metricId === 'p700_barrel_miles') {
    return isPureUnit(normalizedUnit) && context.validation !== 'unit_warning';
  }
  if (
    metricId === 'discounted_rate_volumes' ||
    metricId === 'negotiated_rate_volumes'
  ) {
    return normalizedUnit === 'pure' && context.origin === 'ferc_migrated';
  }
  return false;
};

export const presentationUnit = (
  metricId: string | null | undefined,
  filedOrDisplayUnit: string | null | undefined,
  context: PresentationContext = {},
) => {
  const definition = metricPresentationUnits[metricId || ''];
  return definition &&
    canUseMetricPresentationOverride(metricId, filedOrDisplayUnit, context)
    ? definition.valueUnit
    : filedOrDisplayUnit;
};

export const presentationUnitOverrideNote = (
  metricId: string | null | undefined,
  filedOrDisplayUnit: string | null | undefined,
  context: PresentationContext = {},
) => {
  const definition = metricPresentationUnits[metricId || ''];
  if (
    !definition ||
    !canUseMetricPresentationOverride(metricId, filedOrDisplayUnit, context)
  ) {
    return null;
  }
  const presented = unitLabel(definition.valueUnit);
  const supplied = sourceUnitLabel(filedOrDisplayUnit);
  if (presented === supplied || (!presented && supplied === '')) return null;
  if (metricId === 'certificated_horsepower') {
    return `Presentation override: the registry identifies the rendered Form 2 column as horsepower. The anomalous ${supplied || 'unitless'} source tag is retained as the filed/stored unit, no numeric conversion was applied, and cross-asset comparison is disabled for this metric.`;
  }
  if (
    metricId === 'discounted_rate_volumes' ||
    metricId === 'negotiated_rate_volumes'
  ) {
    return `Presentation override: the registry identifies migrated Page 313 volumes as Dth. The ${supplied || 'unitless'} source tag is retained below, and no numeric conversion was applied.`;
  }
  if (metricId === 'compressor_units') {
    return `Presentation override: the registry defines this pure value as a count. The ${supplied || 'unitless'} source tag is retained below.`;
  }
  return `Presentation override: the registry defines this metric in ${definition.label}. The ${supplied || 'unitless'} source tag is retained below, and no numeric conversion was applied.`;
};

type DisplayValueRecord = {
  as_filed?: string | null;
  normalized_iso?: string | null;
  display_value?: number | string | null;
  display_unit?: string | null;
  unit?: string | null;
};

const dateUnits = new Set(['date', '(date)', '(date range)']);

export const selectBackendDisplayValue = (value: DisplayValueRecord) => {
  if (value.display_value !== null && value.display_value !== undefined) {
    return value.display_value;
  }
  const normalizedUnit = (value.display_unit || value.unit || '')
    .trim()
    .toLowerCase();
  if (dateUnits.has(normalizedUnit)) {
    return value.normalized_iso || value.as_filed || null;
  }
  return value.as_filed || value.normalized_iso || null;
};

const formatNumber = (value: number, exact: boolean) =>
  new Intl.NumberFormat('en-US', {
    notation: exact ? 'standard' : 'compact',
    maximumFractionDigits: exact ? 6 : 2,
  }).format(value);

const formatCurrency = (
  value: number,
  exact: boolean,
  maximumFractionDigits = 2,
) =>
  new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    notation: exact ? 'standard' : 'compact',
    minimumFractionDigits: 0,
    maximumFractionDigits,
  }).format(value);

export function formatBackendValue(
  value: number | string | null | undefined,
  unit?: string | null,
  exact = false,
) {
  if (value === null || value === undefined || value === '') {
    return 'Unavailable';
  }

  const normalizedUnit = unit?.trim().toLowerCase() || '';
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (isStructuredFercValue(trimmed)) return structuredValueLabel(trimmed);
    if (/^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(trimmed)) {
      return formatDate(trimmed);
    }
    if (numericText.test(trimmed)) {
      const numeric = Number(trimmed.replaceAll(',', ''));
      if (
        Number.isFinite(numeric) &&
        (!Number.isInteger(numeric) || Number.isSafeInteger(numeric))
      ) {
        return formatBackendValue(numeric, unit, exact);
      }
    }
    const rendered = unitsWithoutValueSuffix.has(normalizedUnit)
      ? humanizeFercText(trimmed)
      : trimmed;
    const label = unitLabel(unit);
    return label && !unitsWithoutValueSuffix.has(normalizedUnit)
      ? `${rendered} ${label}`
      : rendered;
  }

  if (normalizedUnit === 'iso4217:usd' || normalizedUnit === 'usd') {
    return formatCurrency(value, exact);
  }
  if (
    normalizedUnit === 'usd/bbl' ||
    normalizedUnit === 'usd/barrel' ||
    normalizedUnit === 'usd per barrel'
  ) {
    return `${formatCurrency(value, exact, exact ? 6 : 2)}/bbl`;
  }
  if (normalizedUnit === 'percent') {
    return `${formatNumber(value, exact)}%`;
  }
  if (normalizedUnit === 'ratio' || normalizedUnit === 'multiplier') {
    return `${formatNumber(value, exact)}×`;
  }

  const rendered = formatNumber(value, exact);
  if (unitsWithoutValueSuffix.has(normalizedUnit)) return rendered;
  const label = unitLabel(unit);
  return label ? `${rendered} ${label}` : rendered;
}

export const comparisonUnitLabel = (
  family: string | null | undefined,
  metricId?: string | null,
) => {
  const metricDefinition = metricPresentationUnits[metricId || ''];
  if (metricDefinition) return metricDefinition.label;
  if (!family) return 'Base unit not supplied';
  return (
    comparisonUnits[family.toLowerCase() as keyof typeof comparisonUnits]
      ?.label || 'Unrecognised base unit'
  );
};

export const formatComparisonValue = (
  value: number | null | undefined,
  family: string | null | undefined,
  exact = false,
  metricId?: string | null,
) => {
  if (value === null || value === undefined) return 'Unavailable';
  const metricDefinition = metricPresentationUnits[metricId || ''];
  const definition = family
    ? comparisonUnits[family.toLowerCase() as keyof typeof comparisonUnits]
    : undefined;
  const valueUnit = metricDefinition
    ? metricDefinition.valueUnit
    : definition?.valueUnit;
  return formatBackendValue(value, valueUnit, exact);
};

export const formatCompact = (
  value: number | null,
  kind: 'currency' | 'volume' = 'currency',
) => {
  if (value === null) return 'Unavailable';
  const prefix = kind === 'currency' ? '$' : '';
  if (Math.abs(value) >= 1_000_000_000)
    return `${prefix}${(value / 1_000_000_000).toFixed(2)}bn`;
  if (Math.abs(value) >= 1_000_000)
    return `${prefix}${(value / 1_000_000).toFixed(1)}m`;
  return `${prefix}${value.toLocaleString('en-US')}`;
};

export const margin = (income: number | null, revenue: number | null) =>
  income !== null && revenue !== null && revenue !== 0
    ? (income / revenue) * 100
    : null;

export const percentChange = (
  current: number | null,
  baseline: number | null,
) =>
  current !== null && baseline !== null && baseline > 0
    ? ((current - baseline) / baseline) * 100
    : null;

export const percentagePointChange = (
  current: number | null,
  baseline: number | null,
) => (current !== null && baseline !== null ? current - baseline : null);
