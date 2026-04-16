import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { FileSpreadsheet, FileText, Download, AlertCircle, CheckCircle2 } from 'lucide-react';
import {
  downloadExcelReport,
  downloadPdfReport,
  extractErrorMessage,
  getAnalyticsSummary,
  getPayerScorecard,
  saveBlob,
} from '../services/api.js';
import { fmtCurrency, fmtPercent } from '../utils/format.js';

// Reports page: export options + Excel/PDF download buttons + a payer
// scorecard preview so the user can see the shape of what's in the report
// before downloading.
export default function Reports() {
  const [options, setOptions] = useState({
    dateFrom: '', dateTo: '', payerName: '',
    include835I: true, include835P: true,
    pdfMaxClaims: 25,
  });

  const summary = useQuery({
    queryKey: ['analytics', 'summary', options.dateFrom, options.dateTo, options.payerName],
    queryFn: () => getAnalyticsSummary({
      dateFrom: options.dateFrom || undefined,
      dateTo: options.dateTo || undefined,
      payerName: options.payerName || undefined,
    }),
  });
  const scorecard = useQuery({
    queryKey: ['analytics', 'payer-scorecard', options.dateFrom, options.dateTo],
    queryFn: () => getPayerScorecard({
      dateFrom: options.dateFrom || undefined,
      dateTo: options.dateTo || undefined,
    }),
  });

  const excelMut = useMutation({
    mutationFn: () => downloadExcelReport(options),
    onSuccess: ({ blob, filename }) => saveBlob(blob, filename),
  });
  const pdfMut = useMutation({
    mutationFn: () => downloadPdfReport(options),
    onSuccess: ({ blob, filename }) => saveBlob(blob, filename),
  });

  function setField(k, v) {
    setOptions((prev) => ({ ...prev, [k]: v }));
  }

  return (
    <div className="max-w-5xl space-y-5">
      <header>
        <h1 className="text-xl font-semibold text-brand-900">Reports</h1>
        <p className="text-sm text-slate-500 mt-1">
          Generate formatted Excel and PDF reports. Both honor the filters below.
        </p>
      </header>

      {/* Options */}
      <div className="card p-5">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="label" htmlFor="from">Date from</label>
            <input id="from" type="date" className="input"
              value={options.dateFrom} onChange={(e) => setField('dateFrom', e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="to">Date to</label>
            <input id="to" type="date" className="input"
              value={options.dateTo} onChange={(e) => setField('dateTo', e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="payer">Payer (optional)</label>
            <input id="payer" className="input"
              placeholder="e.g. NEW YORK STATE MEDICAID"
              value={options.payerName} onChange={(e) => setField('payerName', e.target.value)} />
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-4">
          <label className="inline-flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              className="accent-primary"
              checked={options.include835I}
              onChange={(e) => setField('include835I', e.target.checked)}
            />
            Include 835I claims
          </label>
          <label className="inline-flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              className="accent-primary"
              checked={options.include835P}
              onChange={(e) => setField('include835P', e.target.checked)}
            />
            Include 835P claims
          </label>
          <label className="inline-flex items-center gap-2 text-sm">
            <span className="text-slate-600">PDF max claim details:</span>
            <input
              type="number" min={1} max={500}
              className="input w-24"
              value={options.pdfMaxClaims}
              onChange={(e) => setField('pdfMaxClaims', parseInt(e.target.value, 10) || 25)}
            />
          </label>
        </div>

        <div className="mt-5 pt-4 border-t border-slate-200 flex items-center gap-3">
          <button type="button" className="btn-primary" disabled={excelMut.isPending}
            onClick={() => excelMut.mutate()}>
            <FileSpreadsheet className="w-4 h-4" aria-hidden />
            {excelMut.isPending ? 'Generating…' : 'Download Excel (.xlsx)'}
          </button>
          <button type="button" className="btn-secondary" disabled={pdfMut.isPending}
            onClick={() => pdfMut.mutate()}>
            <FileText className="w-4 h-4" aria-hidden />
            {pdfMut.isPending ? 'Generating…' : 'Download PDF'}
          </button>
          <DownloadStatus mut={excelMut} label="Excel" />
          <DownloadStatus mut={pdfMut} label="PDF" />
        </div>
      </div>

      {/* Scope preview */}
      <div className="card p-5">
        <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide mb-3">
          Report scope
        </h2>
        {summary.isLoading ? (
          <div className="text-sm text-slate-400">Loading…</div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <Kv label="Claims in scope" value={summary.data?.total_claims ?? 0} />
            <Kv label="Total billed" value={fmtCurrency(summary.data?.total_billed)} />
            <Kv label="Total paid" value={fmtCurrency(summary.data?.total_paid)} />
            <Kv label="APG underpayment" value={fmtCurrency(summary.data?.apg_underpayment_total)} />
          </div>
        )}
      </div>

      {/* Payer scorecard */}
      <div className="card">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide">
            Payer scorecard
          </h2>
          <span className="text-xs text-slate-400">
            Shown in the PDF executive summary and the Excel Summary sheet.
          </span>
        </div>
        {scorecard.isLoading ? (
          <div className="p-5 text-sm text-slate-400">Loading…</div>
        ) : (scorecard.data?.rows?.length || 0) === 0 ? (
          <div className="p-5 text-sm text-slate-400">No payer data yet.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left px-4 py-2 font-semibold">Payer</th>
                <th className="text-right px-4 py-2 font-semibold">Claims</th>
                <th className="text-right px-4 py-2 font-semibold">Billed</th>
                <th className="text-right px-4 py-2 font-semibold">Paid</th>
                <th className="text-right px-4 py-2 font-semibold">Paid %</th>
                <th className="text-right px-4 py-2 font-semibold">Denial %</th>
                <th className="text-right px-4 py-2 font-semibold">APG variance</th>
                <th className="text-right px-4 py-2 font-semibold">APG comp %</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {scorecard.data.rows.map((r) => (
                <tr key={r.payer_name}>
                  <td className="px-4 py-2">{r.payer_name}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{r.claims}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{fmtCurrency(r.billed)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{fmtCurrency(r.paid)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{fmtPercent(r.paid_as_pct_of_billed)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{fmtPercent(r.denial_rate_pct)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{fmtCurrency(r.apg_variance_total)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{fmtPercent(r.apg_avg_compression_pct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function Kv({ label, value }) {
  return (
    <div>
      <div className="kpi-label">{label}</div>
      <div className="text-lg font-semibold text-slate-800 tabular-nums">{value}</div>
    </div>
  );
}

function DownloadStatus({ mut, label }) {
  if (mut.isError) {
    return (
      <span className="pill-danger">
        <AlertCircle className="w-3 h-3" aria-hidden /> {label} failed: {extractErrorMessage(mut.error)}
      </span>
    );
  }
  if (mut.isSuccess) {
    return (
      <span className="pill-success">
        <CheckCircle2 className="w-3 h-3" aria-hidden /> {label} downloaded
      </span>
    );
  }
  return null;
}
