import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Search, ArrowRight, Link2 } from 'lucide-react';
import { listClaims } from '../services/api.js';
import { fmtCurrency, fmtDate } from '../utils/format.js';

const TYPES = [
  { value: null, label: 'All' },
  { value: '835I', label: '835I' },
  { value: '835P', label: '835P' },
  { value: '837I', label: '837I' },
  { value: '837P', label: '837P' },
];

export default function Claims() {
  const [fileType, setFileType] = useState(null);
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const limit = 50;

  const { data, isLoading } = useQuery({
    queryKey: ['claims', fileType, offset, limit],
    queryFn: () => listClaims({ fileType, offset, limit }),
    keepPreviousData: true,
  });

  // Client-side text filter on claim_id / patient_name / provider_npi — fine
  // for up to a few hundred claims. For larger datasets we'd add a server-side
  // search param.
  const q = search.trim().toLowerCase();
  const visible = (data?.items || []).filter((c) => {
    if (!q) return true;
    return (
      (c.claim_id || '').toLowerCase().includes(q) ||
      (c.patient_name || '').toLowerCase().includes(q) ||
      (c.provider_npi || '').toLowerCase().includes(q)
    );
  });

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-brand-900">Claims</h1>
          <p className="text-sm text-slate-500">
            {data ? `${data.count} claim${data.count === 1 ? '' : 's'} in this page` : 'Loading…'}
          </p>
        </div>
      </header>

      <div className="card p-3 flex flex-wrap items-center gap-2">
        {/* File-type tabs */}
        <div className="flex rounded-md border border-slate-300 overflow-hidden">
          {TYPES.map((t) => (
            <button
              key={t.label}
              type="button"
              onClick={() => {
                setFileType(t.value);
                setOffset(0);
              }}
              className={[
                'px-3 py-1.5 text-sm border-r last:border-r-0 border-slate-300',
                fileType === t.value
                  ? 'bg-primary text-white'
                  : 'bg-white text-slate-700 hover:bg-slate-50',
              ].join(' ')}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="relative flex-1 min-w-[200px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" aria-hidden />
          <input
            className="input pl-9"
            placeholder="Search claim ID, patient, NPI…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
            <tr>
              <th className="text-left font-semibold px-4 py-2.5">Claim ID</th>
              <th className="text-left font-semibold px-4 py-2.5">Type</th>
              <th className="text-left font-semibold px-4 py-2.5">DOS</th>
              <th className="text-left font-semibold px-4 py-2.5">Patient</th>
              <th className="text-left font-semibold px-4 py-2.5">NPI</th>
              <th className="text-right font-semibold px-4 py-2.5">Billed</th>
              <th className="text-right font-semibold px-4 py-2.5">Paid</th>
              <th className="text-left font-semibold px-4 py-2.5">Status</th>
              <th className="w-10"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {isLoading && (
              <tr><td colSpan={9} className="px-4 py-8 text-center text-slate-400">Loading…</td></tr>
            )}
            {!isLoading && visible.length === 0 && (
              <tr>
                <td colSpan={9} className="px-4 py-8 text-center text-slate-400">
                  No claims{fileType ? ` for ${fileType}` : ''}. Upload an EDI file to begin.
                </td>
              </tr>
            )}
            {visible.map((c) => (
              <tr key={c.id} className="hover:bg-slate-50">
                <td className="px-4 py-2.5 font-mono text-xs">
                  <Link to={`/claims/${c.id}`} className="text-primary hover:underline">
                    {c.claim_id}
                  </Link>
                  {c.linked_claim_id_fk && (
                    <span className="ml-1 pill-brand" title={`Linked to claim #${c.linked_claim_id_fk}`}>
                      <Link2 className="w-3 h-3" aria-hidden /> linked
                    </span>
                  )}
                </td>
                <td className="px-4 py-2.5">
                  <FileTypePill type={c.file_type} />
                </td>
                <td className="px-4 py-2.5 text-slate-600">{fmtDate(c.date_of_service)}</td>
                <td className="px-4 py-2.5 text-slate-600">{c.patient_name || '—'}</td>
                <td className="px-4 py-2.5 text-slate-600 font-mono text-xs">{c.provider_npi || '—'}</td>
                <td className="px-4 py-2.5 text-right tabular-nums">{fmtCurrency(c.billed_amount)}</td>
                <td className="px-4 py-2.5 text-right tabular-nums">{fmtCurrency(c.paid_amount)}</td>
                <td className="px-4 py-2.5 text-xs">
                  <ClaimStatusPill status={c.claim_status} />
                </td>
                <td className="px-4 py-2.5 text-right">
                  <Link to={`/claims/${c.id}`} className="text-slate-400 hover:text-primary">
                    <ArrowRight className="w-4 h-4" aria-hidden />
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {/* Pagination */}
        <div className="flex items-center justify-between px-4 py-3 border-t border-slate-200 text-sm">
          <span className="text-slate-500">
            Showing {offset + 1}–{offset + (data?.count || 0)}
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setOffset(Math.max(0, offset - limit))}
              disabled={offset === 0}
            >
              Previous
            </button>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setOffset(offset + limit)}
              disabled={(data?.count || 0) < limit}
            >
              Next
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function FileTypePill({ type }) {
  const classes = {
    '835I': 'pill-brand',
    '835P': 'pill bg-primary-50 text-primary-700',
    '837I': 'pill-success',
    '837P': 'pill bg-amber-50 text-amber-700',
  };
  return <span className={classes[type] || 'pill-slate'}>{type}</span>;
}

function ClaimStatusPill({ status }) {
  // CLP02 codes: 1=processed/paid, 2=adjusted, 3=forwarded, 4=denied, 19=processed interchange, 22=reversal
  const map = {
    '1': { cls: 'pill-success', label: 'Paid' },
    '2': { cls: 'pill-warning', label: 'Adjusted' },
    '3': { cls: 'pill-slate',   label: 'Forwarded' },
    '4': { cls: 'pill-danger',  label: 'Denied' },
    '19': { cls: 'pill-slate',  label: 'Processed' },
    '22': { cls: 'pill-warning', label: 'Reversal' },
  };
  const hit = map[status];
  if (!hit) return <span className="pill-slate">{status || '—'}</span>;
  return <span className={hit.cls}>{hit.label}</span>;
}
