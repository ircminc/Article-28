// Centralized formatters so the whole UI agrees on currency/date/pct styling.
//
// Monetary values come off the API as strings ("320.00") — they were Decimal
// on the backend and JSON-serialized without loss. We parse as Number for
// display but never for calculations (backend does all math).

import { format as dateFormat, parseISO } from 'date-fns';

export function fmtCurrency(v) {
  if (v === null || v === undefined || v === '') return '—';
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (Number.isNaN(n)) return String(v);
  return n.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function fmtPercent(v, digits = 2) {
  if (v === null || v === undefined || v === '') return '—';
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (Number.isNaN(n)) return String(v);
  return `${n.toFixed(digits)}%`;
}

export function fmtDate(isoString) {
  if (!isoString) return '—';
  try {
    return dateFormat(parseISO(isoString), 'MMM d, yyyy');
  } catch {
    return isoString;
  }
}

export function fmtNumber(v) {
  if (v === null || v === undefined || v === '') return '—';
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (Number.isNaN(n)) return String(v);
  return n.toLocaleString('en-US');
}

// Variance display: sign-sensitive. Positive = underpaid (bad for provider),
// negative = overpaid (unusual, worth reviewing).
export function varianceSign(v) {
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (Number.isNaN(n)) return 'neutral';
  if (n > 0) return 'under';    // payer paid less than expected
  if (n < 0) return 'over';     // payer paid more than expected
  return 'neutral';
}
