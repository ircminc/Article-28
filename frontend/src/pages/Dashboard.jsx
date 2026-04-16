import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  BarChart, Bar,
  LineChart, Line,
  XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer,
} from 'recharts';
import {
  AlertCircle, FileStack, Upload, Link2, TrendingDown, DollarSign,
} from 'lucide-react';
import {
  getAnalyticsCompression,
  getAnalyticsSummary,
  getAnalyticsTrends,
  listClaims,
} from '../services/api.js';
import { fmtCurrency, fmtDate, fmtPercent } from '../utils/format.js';

// Phase 4 Dashboard: pulls aggregate KPIs from /api/analytics/* so numbers
// reflect the full dataset (not just the most recent page of claims). Renders
// two Recharts visualizations: rate compression by EAPG, and a monthly
// billed/paid/variance trend.
export default function Dashboard() {
  const summary = useQuery({
    queryKey: ['analytics', 'summary'],
    queryFn: () => getAnalyticsSummary(),
  });
  const compression = useQuery({
    queryKey: ['analytics', 'compression', 'eapg', 10],
    queryFn: () => getAnalyticsCompression({ groupBy: 'eapg', limit: 10 }),
  });
  const trends = useQuery({
    queryKey: ['analytics', 'trends', 'monthly'],
    queryFn: () => getAnalyticsTrends({ period: 'monthly' }),
  });
  const recent = useQuery({
    queryKey: ['claims', null, 0, 8],
    queryFn: () => listClaims({ limit: 8, offset: 0 }),
  });

  if (summary.isLoading) return <div className="text-slate-500">Loading…</div>;
  const s = summary.data || {};

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-brand-900">Dashboard</h1>
          <p className="text-sm text-slate-500">
            Aggregate analytics across all parsed claims.
          </p>
        </div>
        <div className="flex gap-2">
          <Link to="/upload" className="btn-primary">
            <Upload className="w-4 h-4" aria-hidden /> Upload EDI
          </Link>
          <Link to="/claims" className="btn-secondary">
            <FileStack className="w-4 h-4" aria-hidden /> View claims
          </Link>
        </div>
      </header>

      {/* KPI cards */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <KPICard label="Total claims" value={s.total_claims ?? 0} />
        <KPICard label="Total billed" value={fmtCurrency(s.total_billed)} />
        <KPICard label="Total paid" value={fmtCurrency(s.total_paid)} strong />
        <KPICard
          label="Denial rate"
          value={fmtPercent(s.denial_rate_pct)}
          tone={parseFloat(s.denial_rate_pct || 0) > 10 ? 'warning' : 'neutral'}
        />
        <KPICard
          label="APG underpayment"
          value={fmtCurrency(s.apg_underpayment_total)}
          tone={parseFloat(s.apg_underpayment_total || 0) > 0 ? 'danger' : 'neutral'}
          icon={TrendingDown}
        />
      </div>

      {/* Secondary row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KPICard label="Paid % of billed" value={fmtPercent(s.paid_as_pct_of_billed)} />
        <KPICard label="APG claims" value={s.apg_claims ?? 0} />
        <KPICard label="APG expected total" value={fmtCurrency(s.apg_correct_payment_total)} />
        <KPICard
          label="APG avg compression"
          value={fmtPercent(s.apg_avg_compression_pct)}
          icon={DollarSign}
        />
      </div>

      {/* Compression chart */}
      <div className="card p-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide">
            Rate compression — top 10 EAPGs by variance
          </h2>
          <Link to="/reports" className="text-xs text-primary hover:underline">
            Full report →
          </Link>
        </div>
        {compression.isLoading ? (
          <div className="text-slate-400 text-sm">Loading…</div>
        ) : (
          <CompressionChart rows={compression.data?.rows || []} />
        )}
      </div>

      {/* Trend chart */}
      <div className="card p-5">
        <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide mb-3">
          Monthly billed / paid / variance
        </h2>
        {trends.isLoading ? (
          <div className="text-slate-400 text-sm">Loading…</div>
        ) : (
          <TrendChart series={trends.data?.series || []} />
        )}
      </div>

      {/* Recent claims */}
      <div className="card p-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide">
            Most recent claims
          </h2>
          <Link to="/claims" className="text-xs text-primary hover:underline">
            All claims →
          </Link>
        </div>
        {recent.isLoading ? (
          <div className="text-slate-400 text-sm">Loading…</div>
        ) : (recent.data?.items?.length || 0) === 0 ? (
          <p className="text-sm text-slate-400">
            No claims yet. Head to <Link to="/upload" className="text-primary hover:underline">Upload</Link>.
          </p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {recent.data.items.slice(0, 8).map((c) => (
              <li key={c.id} className="py-2 flex items-center justify-between text-sm">
                <div className="flex items-center gap-3 min-w-0">
                  <span className="pill-brand">{c.file_type}</span>
                  {c.linked_claim_id_fk && (
                    <span className="pill-slate" title="Linked to sibling claim">
                      <Link2 className="w-3 h-3" aria-hidden />
                    </span>
                  )}
                  <Link to={`/claims/${c.id}`} className="font-mono text-xs text-primary hover:underline">
                    {c.claim_id}
                  </Link>
                  <span className="text-slate-500 text-xs truncate">
                    {c.patient_name || '—'} · DOS {fmtDate(c.date_of_service)}
                  </span>
                </div>
                <div className="text-xs tabular-nums text-slate-500 shrink-0">
                  billed {fmtCurrency(c.billed_amount)} · paid {fmtCurrency(c.paid_amount)}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------

function CompressionChart({ rows }) {
  if (!rows.length) {
    return (
      <div className="text-sm text-slate-400 p-6 text-center">
        No APG variance data yet. Upload 835I files to populate.
      </div>
    );
  }
  // Shorten bucket labels so they don't dominate the X axis
  const data = rows.map((r) => ({
    name: String(r.bucket).length > 24
      ? String(r.bucket).slice(0, 22) + '…'
      : String(r.bucket),
    expected: parseFloat(r.expected),
    paid: parseFloat(r.paid),
  }));

  return (
    <div style={{ width: '100%', height: 320 }}>
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 12, right: 16, bottom: 32, left: 40 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis dataKey="name" angle={-20} dy={8} fontSize={11} interval={0} stroke="#64748b" />
          <YAxis tickFormatter={(v) => `$${Math.round(v / 1000)}k`} fontSize={11} stroke="#64748b" />
          <Tooltip
            formatter={(v) => new Intl.NumberFormat('en-US', {
              style: 'currency', currency: 'USD',
            }).format(v)}
          />
          <Legend />
          <Bar dataKey="expected" name="Correct APG" fill="#1a2e4a" />
          <Bar dataKey="paid" name="Actual paid" fill="#2563eb" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function TrendChart({ series }) {
  if (!series.length) {
    return (
      <div className="text-sm text-slate-400 p-6 text-center">
        No trend data yet — upload at least one month's worth of claims.
      </div>
    );
  }
  const data = series.map((s) => ({
    period: s.period,
    billed: parseFloat(s.billed),
    paid: parseFloat(s.paid),
    variance: parseFloat(s.variance),
  }));
  return (
    <div style={{ width: '100%', height: 280 }}>
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 12, right: 16, bottom: 16, left: 40 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis dataKey="period" fontSize={11} stroke="#64748b" />
          <YAxis tickFormatter={(v) => `$${Math.round(v / 1000)}k`} fontSize={11} stroke="#64748b" />
          <Tooltip
            formatter={(v) => new Intl.NumberFormat('en-US', {
              style: 'currency', currency: 'USD',
            }).format(v)}
          />
          <Legend />
          <Line type="monotone" dataKey="billed"   name="Billed"   stroke="#1a2e4a" strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="paid"     name="Paid"     stroke="#2563eb" strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="variance" name="Variance" stroke="#dc2626" strokeWidth={2} dot={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function KPICard({ label, value, strong = false, tone = 'neutral', icon: Icon }) {
  const toneCls = {
    neutral: 'text-brand-900',
    warning: 'text-warning-700',
    danger:  'text-danger-700',
    success: 'text-success-700',
  }[tone];
  return (
    <div className="card p-4">
      <div className="flex items-center justify-between">
        <div className="kpi-label">{label}</div>
        {Icon && <Icon className="w-4 h-4 text-slate-400" aria-hidden />}
      </div>
      <div className={`${strong ? 'kpi-value' : 'text-2xl font-semibold tabular-nums'} ${toneCls}`}>
        {value}
      </div>
    </div>
  );
}
