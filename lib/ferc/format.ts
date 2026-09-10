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
