import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { ArrowLeft, Link2 } from 'lucide-react';
import { getClaim } from '../services/api.js';
import { fmtCurrency, fmtDate, fmtPercent, varianceSign } from '../utils/format.js';

// Full detail view for a single parsed claim. Renders:
//   - Header identifying payer/provider/patient/DOS and totals
//   - Service lines table (billed / allowed / paid per line, modifiers)
//   - Adjustments (CAS segments with CARC codes)
//   - APG result panel (when the claim is a 835I/835P): base rate, correct
//     payment, variance, per-line expected vs paid with packaging/discount flags
//   - Linked sibling panel (if an 837 enriched an 835 or vice-versa)
export default function ClaimDetail() {
  const { id } = useParams();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['claim', id],
    queryFn: () => getClaim(id),
  });

  if (isLoading) return <div className="text-slate-500">Loading…</div>;
  if (isError) {
    return (
      <div className="card p-6">
        <Link to="/claims" className="text-sm text-primary hover:underline inline-flex items-center gap-1">
          <ArrowLeft className="w-3.5 h-3.5" aria-hidden /> Back to claims
        </Link>
        <p className="mt-4 text-danger-700">Failed to load claim: {error?.message || 'Unknown error'}</p>
      </div>
    );
  }

  const c = data;
  const apg = c.apg_result;

  return (
    <div className="max-w-6xl space-y-5">
      <div>
        <Link to="/claims" className="text-sm text-primary hover:underline inline-flex items-center gap-1">
          <ArrowLeft className="w-3.5 h-3.5" aria-hidden /> Back to claims
        </Link>
      </div>

      {/* Header */}
      <div className="card p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <h1 className="text-xl font-semibold text-brand-900 font-mono">
                {c.claim_id}
              </h1>
              <span className="pill-brand">{c.file_type}</span>
              {c.linked_claim && (
                <Link
                  to={`/claims/${c.linked_claim.id}`}
                  className="pill bg-slate-100 text-slate-700 hover:bg-slate-200"
                  title="Open linked claim"
                >
                  <Link2 className="w-3 h-3" aria-hidden />
                  Linked to {c.linked_claim.claim_id} ({c.linked_claim.file_type})
                </Link>
              )}
            </div>
            <div className="text-sm text-slate-500">
              DOS {fmtDate(c.date_of_service)} · Payer {c.payer_name || '—'} · Provider{' '}
              {c.provider_name || '—'}
              {c.provider_npi ? ` (NPI ${c.provider_npi})` : ''}
            </div>
          </div>
          <div className="grid grid-cols-3 gap-6 text-right">
            <Stat label="Billed" value={fmtCurrency(c.billed_amount)} />
            <Stat label="Paid"   value={fmtCurrency(c.paid_amount)}   strong />
            <Stat label="Pt resp" value={fmtCurrency(c.patient_responsibility)} />
          </div>
        </div>

        <div className="mt-4 grid grid-cols-1 md:grid-cols-4 gap-4 text-sm">
          <Field label="Patient" value={c.patient_name} />
          <Field label="Patient ID" value={c.patient_id} mono />
          <Field label="Filing indicator" value={c.claim_filing_indicator} />
          <Field label="Principal DX" value={c.principal_diagnosis} mono />
        </div>
        {(c.other_diagnoses?.length > 0) && (
          <div className="mt-2 text-sm text-slate-600">
            <span className="text-slate-400">Other DX:</span>{' '}
            <span className="font-mono text-xs">{c.other_diagnoses.join(', ')}</span>
          </div>
        )}
      </div>

      {/* APG panel */}
      {apg && <APGPanel apg={apg} />}

      {/* Service lines */}
      <div className="card">
        <div className="px-5 py-3 border-b border-slate-200">
          <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide">
            Service lines ({c.service_lines.length})
          </h2>
        </div>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
            <tr>
              <th className="text-left font-semibold px-4 py-2">#</th>
              <th className="text-left font-semibold px-4 py-2">Procedure</th>
              <th className="text-left font-semibold px-4 py-2">Modifiers</th>
              <th className="text-left font-semibold px-4 py-2">Rev</th>
              <th className="text-right font-semibold px-4 py-2">Units</th>
              <th className="text-right font-semibold px-4 py-2">Billed</th>
              <th className="text-right font-semibold px-4 py-2">Allowed</th>
              <th className="text-right font-semibold px-4 py-2">Paid</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {c.service_lines.map((sl) => (
              <tr key={sl.line_seq}>
                <td className="px-4 py-2 font-mono text-xs text-slate-400">{sl.line_seq}</td>
                <td className="px-4 py-2 font-mono">{sl.procedure_code || '—'}</td>
                <td className="px-4 py-2 font-mono text-xs text-slate-600">
                  {(sl.modifiers || []).join(', ') || '—'}
                </td>
                <td className="px-4 py-2 font-mono text-xs text-slate-600">
                  {sl.revenue_code || '—'}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">{sl.units}</td>
                <td className="px-4 py-2 text-right tabular-nums">{fmtCurrency(sl.billed_amount)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{fmtCurrency(sl.allowed_amount)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{fmtCurrency(sl.paid_amount)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Adjustments */}
      {c.adjustments?.length > 0 && (
        <div className="card">
          <div className="px-5 py-3 border-b border-slate-200">
            <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide">
              Claim-level adjustments
            </h2>
          </div>
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left px-4 py-2 font-semibold">Group</th>
                <th className="text-left px-4 py-2 font-semibold">Reason (CARC)</th>
                <th className="text-right px-4 py-2 font-semibold">Amount</th>
                <th className="text-right px-4 py-2 font-semibold">Qty</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {c.adjustments.map((a, i) => (
                <tr key={i}>
                  <td className="px-4 py-2 font-mono">{a.group_code}</td>
                  <td className="px-4 py-2 font-mono">{a.reason_code}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{fmtCurrency(a.amount)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{a.quantity ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, strong = false }) {
  return (
    <div>
      <div className="kpi-label">{label}</div>
      <div className={strong ? 'kpi-value' : 'text-lg font-semibold text-slate-700 tabular-nums'}>
        {value}
      </div>
    </div>
  );
}

function Field({ label, value, mono = false }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={mono ? 'font-mono text-sm' : 'text-sm'}>{value || '—'}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// APG result panel
// ---------------------------------------------------------------------------
function APGPanel({ apg }) {
  const sign = varianceSign(apg.variance);
  const varClass =
    sign === 'under' ? 'text-danger-700'
    : sign === 'over' ? 'text-warning-700'
    : 'text-slate-700';

  return (
    <div className="card p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide">
          APG calculation
        </h2>
        <div className="flex flex-wrap gap-2">
          {apg.discounting_applied && <span className="pill-warning">Multi-procedure discount</span>}
          {apg.u6_applied && <span className="pill-warning">U6 modifier</span>}
          {apg.capital_applied && <span className="pill-brand">Capital add-on</span>}
          <span className="pill-slate">{apg.peer_group}</span>
          <span className="pill-slate">{apg.region}</span>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-5">
        <Stat label="Base rate" value={fmtCurrency(apg.base_rate_applied)} />
        <Stat label="Correct APG payment" value={fmtCurrency(apg.correct_apg_payment)} strong />
        <Stat label="Actual paid" value={fmtCurrency(apg.actual_paid)} />
        <div>
          <div className="kpi-label">Variance</div>
          <div className={`kpi-value ${varClass}`}>{fmtCurrency(apg.variance)}</div>
        </div>
        <div>
          <div className="kpi-label">Compression</div>
          <div className={`kpi-value ${varClass}`}>{fmtPercent(apg.compression_pct, 2)}</div>
        </div>
      </div>

      {/* Per-line breakdown */}
      <div>
        <h3 className="text-xs uppercase tracking-wide text-slate-500 mb-2">Per-line breakdown</h3>
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
            <tr>
              <th className="text-left px-3 py-2 font-semibold">#</th>
              <th className="text-left px-3 py-2 font-semibold">Proc</th>
              <th className="text-left px-3 py-2 font-semibold">EAPG</th>
              <th className="text-left px-3 py-2 font-semibold">Type</th>
              <th className="text-right px-3 py-2 font-semibold">Weight</th>
              <th className="text-right px-3 py-2 font-semibold">Expected</th>
              <th className="text-right px-3 py-2 font-semibold">Paid</th>
              <th className="text-right px-3 py-2 font-semibold">Variance</th>
              <th className="text-left px-3 py-2 font-semibold">Notes</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {(apg.line_details || []).map((ld) => (
              <tr key={ld.line_seq}>
                <td className="px-3 py-2 text-xs font-mono text-slate-400">{ld.line_seq}</td>
                <td className="px-3 py-2 font-mono text-xs">{ld.procedure_code || '—'}</td>
                <td className="px-3 py-2 text-xs">{ld.eapg ? `${ld.eapg} — ${ld.eapg_desc || ''}` : '—'}</td>
                <td className="px-3 py-2 text-xs">
                  <EapgTypePill type={ld.eapg_type} />
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-xs">
                  {ld.weight !== null && ld.weight !== undefined ? Number(ld.weight).toFixed(4) : '—'}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">{fmtCurrency(ld.expected_payment)}</td>
                <td className="px-3 py-2 text-right tabular-nums">{fmtCurrency(ld.actual_paid)}</td>
                <td className="px-3 py-2 text-right tabular-nums">{fmtCurrency(ld.variance)}</td>
                <td className="px-3 py-2 text-xs text-slate-500">
                  <div className="flex flex-wrap gap-1">
                    {ld.packaged && <span className="pill-slate">Packaged</span>}
                    {ld.discounted && <span className="pill-warning">50% discount</span>}
                    {ld.u6_applied && <span className="pill-warning">U6</span>}
                    {ld.denied && <span className="pill-danger">Denied</span>}
                  </div>
                  {ld.notes?.length > 0 && (
                    <ul className="mt-1 space-y-0.5 text-[11px] text-slate-500 list-disc pl-4">
                      {ld.notes.map((n, i) => <li key={i}>{n}</li>)}
                    </ul>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function EapgTypePill({ type }) {
  const map = {
    'Significant Procedure': 'pill-success',
    'Medical Visit':         'pill bg-primary-50 text-primary-700',
    'Ancillary':             'pill-slate',
    'Incidental':            'pill-slate',
    'Add-On':                'pill-brand',
    'Unknown':               'pill-slate',
  };
  return <span className={map[type] || 'pill-slate'}>{type || '—'}</span>;
}
