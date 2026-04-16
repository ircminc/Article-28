import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { TrendingUp, TrendingDown, AlertCircle, FileStack, Upload, Link2 } from 'lucide-react';
import { listClaims } from '../services/api.js';
import { fmtCurrency, fmtDate, fmtPercent } from '../utils/format.js';

// Phase 3 Dashboard: derives KPIs client-side from the paginated /api/claims
// endpoint. Phase 4 will add dedicated /api/analytics/* endpoints that
// aggregate across all claims (with charts). For now we show what's visible
// and call out the limit explicitly.
//
// Pulls a larger page (500) so small-to-medium sessions get useful numbers.
// Larger datasets will need the Phase 4 analytics endpoints.
const KPI_PAGE = 500;

export default function Dashboard() {
  const { data, isLoading } = useQuery({
    queryKey: ['claims', null, 0, KPI_PAGE],
    queryFn: () => listClaims({ limit: KPI_PAGE, offset: 0 }),
  });

  if (isLoading) return <div className="text-slate-500">Loading…</div>;
  const items = data?.items || [];

  const kpis = computeKPIs(items);

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-brand-900">Dashboard</h1>
          <p className="text-sm text-slate-500">
            Showing KPIs across the {items.length} most recent claim
            {items.length === 1 ? '' : 's'}
            {items.length === KPI_PAGE ? ' (capped — use Phase 4 analytics for full aggregates)' : ''}.
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
        <KPICard label="Claims" value={kpis.count} />
        <KPICard label="Total billed" value={fmtCurrency(kpis.totalBilled)} />
        <KPICard label="Total paid"   value={fmtCurrency(kpis.totalPaid)}   strong />
        <KPICard
          label="Paid as % of billed"
          value={fmtPercent(kpis.paidPct)}
          tone={kpis.paidPct < 70 ? 'warning' : 'neutral'}
        />
        <KPICard
          label="Linked 837↔835 pairs"
          value={kpis.linkedPairs}
          icon={Link2}
        />
      </div>

      {/* Claim-type mix */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="card p-5">
          <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide mb-3">
            Claim type mix
          </h2>
          <ul className="space-y-2">
            {Object.entries(kpis.byType)
              .sort((a, b) => b[1].count - a[1].count)
              .map(([t, v]) => (
                <li key={t} className="flex items-center justify-between text-sm">
                  <span className="flex items-center gap-2">
                    <span className="pill-slate">{t}</span>
                    <span className="text-slate-600">
                      {v.count} claim{v.count === 1 ? '' : 's'}
                    </span>
                  </span>
                  <span className="text-slate-500 tabular-nums">
                    {fmtCurrency(v.billed)} billed · {fmtCurrency(v.paid)} paid
                  </span>
                </li>
              ))}
            {Object.keys(kpis.byType).length === 0 && (
              <li className="text-sm text-slate-400">No claims yet. Upload an EDI file.</li>
            )}
          </ul>
        </div>

        {/* Most recent uploads */}
        <div className="card p-5 lg:col-span-2">
          <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide mb-3">
            Most recent claims
          </h2>
          {items.length === 0 ? (
            <p className="text-sm text-slate-400">
              No claims yet. Head to <Link to="/upload" className="text-primary hover:underline">Upload</Link>.
            </p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {items.slice(0, 8).map((c) => (
                <li key={c.id} className="py-2 flex items-center justify-between text-sm">
                  <div className="flex items-center gap-3 min-w-0">
                    <span className="pill-brand">{c.file_type}</span>
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

      {/* Placeholder card for Phase 4 analytics */}
      <div className="card p-5 border-dashed border-slate-300">
        <div className="flex items-center gap-3 text-slate-500">
          <AlertCircle className="w-4 h-4 text-slate-400" aria-hidden />
          <div className="text-sm">
            Rate-compression charts, denial analysis, and payer scorecards land in
            <span className="font-medium"> Phase 4</span>. The underlying APG math is
            already running on every 835I claim — browse
            <Link to="/claims" className="text-primary mx-1 hover:underline">Claims</Link>
            to see per-claim variance and compression %.
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// KPI aggregation
// ---------------------------------------------------------------------------
function computeKPIs(items) {
  let totalBilled = 0;
  let totalPaid = 0;
  const byType = {};
  let linkedPairs = 0;

  for (const c of items) {
    const b = parseFloat(c.billed_amount || 0);
    const p = parseFloat(c.paid_amount || 0);
    if (!Number.isNaN(b)) totalBilled += b;
    if (!Number.isNaN(p)) totalPaid += p;
    const t = c.file_type || 'unknown';
    byType[t] ||= { count: 0, billed: 0, paid: 0 };
    byType[t].count += 1;
    byType[t].billed += Number.isNaN(b) ? 0 : b;
    byType[t].paid   += Number.isNaN(p) ? 0 : p;
    if (c.linked_claim_id_fk) linkedPairs += 1;
  }

  // linkedPairs counts both sides of each link, so divide by 2
  linkedPairs = Math.floor(linkedPairs / 2);

  const paidPct = totalBilled > 0 ? (totalPaid / totalBilled) * 100 : 0;

  return {
    count: items.length,
    totalBilled,
    totalPaid,
    paidPct,
    byType,
    linkedPairs,
  };
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
