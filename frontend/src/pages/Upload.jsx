import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Play, AlertCircle, CheckCircle2, ExternalLink } from 'lucide-react';
import FileDropZone from '../components/FileDropZone.jsx';
import { extractErrorMessage, upload835I, upload835P, upload837 } from '../services/api.js';

// Upload page coordinates three drop zones, one per supported EDI type.
// When the user clicks "Analyze all", each non-empty zone fires its own
// mutation in parallel. Results are kept per-zone so partial failures are
// visible.
export default function Upload() {
  const queryClient = useQueryClient();
  const [files835I, setFiles835I] = useState([]);
  const [files835P, setFiles835P] = useState([]);
  const [files837, setFiles837] = useState([]);
  const [results, setResults] = useState({});

  const mut835I = useMutation({ mutationFn: upload835I });
  const mut835P = useMutation({ mutationFn: upload835P });
  const mut837  = useMutation({ mutationFn: upload837 });

  async function runUploads() {
    const next = {};
    const jobs = [];
    if (files835I.length) {
      jobs.push(mut835I.mutateAsync(files835I).then(
        (r) => { next['835I'] = { ok: true, data: r }; },
        (e) => { next['835I'] = { ok: false, error: extractErrorMessage(e) }; },
      ));
    }
    if (files835P.length) {
      jobs.push(mut835P.mutateAsync(files835P).then(
        (r) => { next['835P'] = { ok: true, data: r }; },
        (e) => { next['835P'] = { ok: false, error: extractErrorMessage(e) }; },
      ));
    }
    if (files837.length) {
      jobs.push(mut837.mutateAsync(files837).then(
        (r) => { next['837'] = { ok: true, data: r }; },
        (e) => { next['837'] = { ok: false, error: extractErrorMessage(e) }; },
      ));
    }
    await Promise.all(jobs);
    setResults(next);
    queryClient.invalidateQueries({ queryKey: ['claims'] });
  }

  const pending = mut835I.isPending || mut835P.isPending || mut837.isPending;
  const totalStaged = files835I.length + files835P.length + files837.length;

  return (
    <div className="max-w-5xl space-y-6">
      <header>
        <h1 className="text-xl font-semibold text-brand-900">Upload EDI files</h1>
        <p className="text-sm text-slate-500 mt-1">
          Drop institutional remittances (835I), professional remittances (835P), or
          claim submissions (837). Matching 837 ↔ 835 pairs are linked automatically
          and the APG engine re-runs on the enriched remittance.
        </p>
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="card p-4">
          <FileDropZone
            label="835I — Institutional Remittance"
            description="Article 28. Will run the APG engine on each claim."
            files={files835I}
            onFilesChange={setFiles835I}
            accent="brand"
            disabled={pending}
          />
        </div>
        <div className="card p-4">
          <FileDropZone
            label="835P — Professional Remittance"
            description="Non-Article 28. CMS MPFS comparison available per service line."
            files={files835P}
            onFilesChange={setFiles835P}
            accent="primary"
            disabled={pending}
          />
        </div>
        <div className="card p-4">
          <FileDropZone
            label="837 — Claim Submission"
            description="Institutional or professional, auto-detected. Diagnosis codes enrich matching 835s."
            files={files837}
            onFilesChange={setFiles837}
            accent="success"
            disabled={pending}
          />
        </div>
      </div>

      <div className="card p-4 flex items-center justify-between">
        <div className="text-sm text-slate-500">
          {totalStaged === 0
            ? 'No files staged.'
            : `${totalStaged} file${totalStaged > 1 ? 's' : ''} staged across ${
                [files835I, files835P, files837].filter((x) => x.length).length
              } zone${
                [files835I, files835P, files837].filter((x) => x.length).length > 1 ? 's' : ''
              }.`}
        </div>
        <button
          type="button"
          className="btn-primary"
          onClick={runUploads}
          disabled={pending || totalStaged === 0}
        >
          <Play className="w-4 h-4" aria-hidden />
          {pending ? 'Analyzing…' : 'Analyze all'}
        </button>
      </div>

      {Object.keys(results).length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide">
            Results
          </h2>
          {['835I', '835P', '837'].map((type) => {
            const r = results[type];
            if (!r) return null;
            return (
              <UploadResultCard
                key={type}
                type={type}
                result={r}
                onClear={() => {
                  if (type === '835I') setFiles835I([]);
                  if (type === '835P') setFiles835P([]);
                  if (type === '837') setFiles837([]);
                  setResults((prev) => {
                    const copy = { ...prev };
                    delete copy[type];
                    return copy;
                  });
                }}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function UploadResultCard({ type, result, onClear }) {
  if (!result.ok) {
    return (
      <div className="card p-4 border-l-4 border-l-danger">
        <div className="flex items-center justify-between">
          <span className="pill-danger">
            <AlertCircle className="w-3 h-3" aria-hidden /> {type} — failed
          </span>
          <button className="btn-secondary" onClick={onClear}>Dismiss</button>
        </div>
        <pre className="text-xs text-danger-700 mt-2 whitespace-pre-wrap">{result.error}</pre>
      </div>
    );
  }

  const data = result.data;
  return (
    <div className="card p-4 border-l-4 border-l-success">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <span className="pill-success">
            <CheckCircle2 className="w-3 h-3" aria-hidden /> {type} — parsed
          </span>
          <span className="text-sm text-slate-600">
            {data.files_processed} file(s), {data.total_claims} claim(s)
          </span>
          {data.enrichment && (
            <span className="pill-brand">
              Enriched: {data.enrichment.linked_pairs} pair(s), {data.enrichment.apg_recalcs} recalc(s)
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Link to="/claims" className="btn-secondary">
            <ExternalLink className="w-3.5 h-3.5" aria-hidden /> View claims
          </Link>
          <button className="btn-secondary" onClick={onClear}>Clear</button>
        </div>
      </div>
      <ul className="text-xs text-slate-600 space-y-1">
        {(data.results || []).map((r) => (
          <li key={r.file_id} className="flex gap-3">
            <span className="font-mono text-slate-400">{r.file_id}</span>
            <span className="font-medium">{r.file}</span>
            <span className="text-slate-500">
              {r.claims_parsed} claims
              {r.payer ? ` · ${r.payer}` : ''}
              {r.payment_amount ? ` · paid ${r.payment_amount}` : ''}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
