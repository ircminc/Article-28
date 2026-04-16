import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import {
  Calculator as CalcIcon, Plus, Trash2, Play,
  AlertCircle, CheckCircle2, Info,
} from 'lucide-react';
import { calculateRate, extractErrorMessage } from '../services/api.js';
import { fmtCurrency, fmtPercent, varianceSign } from '../utils/format.js';

// Manual CPT/ICD entry calculator.
// Lets a user — after setting up their provider — type in one or more
// service lines and get both the expected Article 28 APG reimbursement and
// the expected Medicare MPFS payment side by side.

const TODAY = new Date().toISOString().slice(0, 10);

const EMPTY_LINE = {
  procedure_code: '',
  modifiers: ['', '', '', ''],
  units: 1,
  billed_amount: '',
};

export default function Calculator() {
  const [dos, setDos] = useState(TODAY);
  const [principalDx, setPrincipalDx] = useState('');
  const [otherDx, setOtherDx] = useState('');
  const [target, setTarget] = useState('both');
  const [cmsLocality, setCmsLocality] = useState('');
  const [useFacilityRate, setUseFacilityRate] = useState(false);
  const [lines, setLines] = useState([{ ...EMPTY_LINE }]);

  const mut = useMutation({ mutationFn: calculateRate });

  function updateLine(idx, patch) {
    setLines((prev) => prev.map((l, i) => (i === idx ? { ...l, ...patch } : l)));
  }

  function updateModifier(lineIdx, modIdx, value) {
    setLines((prev) =>
      prev.map((l, i) => {
        if (i !== lineIdx) return l;
        const mods = [...l.modifiers];
        mods[modIdx] = value.toUpperCase();
        return { ...l, modifiers: mods };
      })
    );
  }

  function addLine() {
    setLines((prev) => [...prev, { ...EMPTY_LINE, modifiers: ['', '', '', ''] }]);
  }

  function removeLine(idx) {
    setLines((prev) => (prev.length <= 1 ? prev : prev.filter((_, i) => i !== idx)));
  }

  function handleSubmit(e) {
    e.preventDefault();
    const payload = {
      date_of_service: dos,
      principal_diagnosis: principalDx.trim().toUpperCase() || null,
      other_diagnoses: otherDx
        .split(/[,\s]+/)
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean),
      target,
      service_lines: lines.map((l) => ({
        procedure_code: l.procedure_code.trim().toUpperCase(),
        modifiers: l.modifiers.map((m) => m.trim().toUpperCase()).filter(Boolean),
        units: parseInt(l.units, 10) || 1,
        billed_amount: l.billed_amount ? String(l.billed_amount) : null,
      })),
    };
    if (cmsLocality.trim()) payload.cms_locality = cmsLocality.trim();
    if (useFacilityRate) payload.cms_use_facility_rate = true;
    mut.mutate(payload);
  }

  return (
    <div className="max-w-6xl space-y-5">
      <header className="flex items-center gap-3">
        <div className="w-10 h-10 bg-primary-50 text-primary rounded-md grid place-items-center">
          <CalcIcon className="w-5 h-5" aria-hidden />
        </div>
        <div>
          <h1 className="text-xl font-semibold text-brand-900">Rate Calculator</h1>
          <p className="text-sm text-slate-500">
            Enter CPT/HCPCS + ICD-10 codes to see the expected APG (Article 28) and
            CMS MPFS (Medicare professional) rates for a date of service.
          </p>
        </div>
      </header>

      <form onSubmit={handleSubmit} className="card p-6 space-y-5">
        {/* Context row */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div>
            <label className="label" htmlFor="dos">Date of service *</label>
            <input
              id="dos" type="date" className="input" required
              value={dos} onChange={(e) => setDos(e.target.value)}
            />
          </div>
          <div>
            <label className="label" htmlFor="target">Calculate</label>
            <select
              id="target" className="select"
              value={target} onChange={(e) => setTarget(e.target.value)}
            >
              <option value="both">Both (APG + CMS)</option>
              <option value="apg">APG only (Article 28)</option>
              <option value="cms">CMS MPFS only</option>
            </select>
          </div>
          <div>
            <label className="label" htmlFor="pdx">Principal diagnosis (ICD-10)</label>
            <input
              id="pdx" className="input font-mono" placeholder="E119"
              value={principalDx} onChange={(e) => setPrincipalDx(e.target.value)}
              maxLength={12}
            />
          </div>
          <div>
            <label className="label" htmlFor="odx">Other diagnoses (comma-sep)</label>
            <input
              id="odx" className="input font-mono" placeholder="I10, Z00.00"
              value={otherDx} onChange={(e) => setOtherDx(e.target.value)}
            />
          </div>
        </div>

        {/* CMS-specific row — only visible when CMS is part of target */}
        {(target === 'cms' || target === 'both') && (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 border-t border-slate-200 pt-4">
            <div>
              <label className="label" htmlFor="loc">CMS locality (optional)</label>
              <input
                id="loc" className="input"
                placeholder="Falls back to provider config"
                value={cmsLocality} onChange={(e) => setCmsLocality(e.target.value)}
              />
              <p className="text-xs text-slate-500 mt-1">
                e.g. <code>01</code> for NYC. Leave blank to use the provider's default.
              </p>
            </div>
            <div className="md:col-span-2">
              <label className="label block">Place of service for MPFS</label>
              <label className="inline-flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="accent-primary"
                  checked={useFacilityRate}
                  onChange={(e) => setUseFacilityRate(e.target.checked)}
                />
                Use facility rate (e.g. hospital outpatient) instead of non-facility (office)
              </label>
            </div>
          </div>
        )}

        {/* Service lines */}
        <div className="border-t border-slate-200 pt-4">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide">
              Service lines ({lines.length})
            </h2>
            <button
              type="button" className="btn-secondary"
              onClick={addLine}
            >
              <Plus className="w-4 h-4" aria-hidden /> Add line
            </button>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
                <tr>
                  <th className="text-left font-semibold px-3 py-2 w-8">#</th>
                  <th className="text-left font-semibold px-3 py-2">CPT / HCPCS *</th>
                  <th className="text-left font-semibold px-3 py-2">Modifiers</th>
                  <th className="text-right font-semibold px-3 py-2 w-20">Units</th>
                  <th className="text-right font-semibold px-3 py-2 w-32">Billed $</th>
                  <th className="w-10"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {lines.map((line, i) => (
                  <tr key={i}>
                    <td className="px-3 py-2 text-xs font-mono text-slate-400">{i + 1}</td>
                    <td className="px-3 py-2">
                      <input
                        required
                        className="input font-mono uppercase" placeholder="99213"
                        maxLength={8}
                        value={line.procedure_code}
                        onChange={(e) =>
                          updateLine(i, { procedure_code: e.target.value })
                        }
                      />
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex gap-1">
                        {line.modifiers.map((mod, mi) => (
                          <input
                            key={mi}
                            className="input font-mono w-14 px-2 py-1 text-xs uppercase"
                            placeholder="--"
                            maxLength={2}
                            value={mod}
                            onChange={(e) => updateModifier(i, mi, e.target.value)}
                          />
                        ))}
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <input
                        type="number" min={1} max={999} step={1}
                        className="input text-right"
                        value={line.units}
                        onChange={(e) => updateLine(i, { units: e.target.value })}
                      />
                    </td>
                    <td className="px-3 py-2">
                      <input
                        type="number" step="0.01" min="0"
                        className="input text-right"
                        placeholder="0.00"
                        value={line.billed_amount}
                        onChange={(e) => updateLine(i, { billed_amount: e.target.value })}
                      />
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        type="button"
                        className="text-slate-400 hover:text-danger disabled:opacity-40"
                        title="Remove line"
                        onClick={() => removeLine(i)}
                        disabled={lines.length <= 1}
                      >
                        <Trash2 className="w-4 h-4" aria-hidden />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="flex justify-end pt-2 border-t border-slate-200">
          <button type="submit" className="btn-primary" disabled={mut.isPending}>
            <Play className="w-4 h-4" aria-hidden />
            {mut.isPending ? 'Calculating…' : 'Calculate'}
          </button>
        </div>
      </form>

      {/* Error */}
      {mut.isError && (
        <div className="card p-4 border-l-4 border-l-danger">
          <span className="pill-danger">
            <AlertCircle className="w-3 h-3" aria-hidden /> {extractErrorMessage(mut.error)}
          </span>
        </div>
      )}

      {/* Results */}
      {mut.isSuccess && <ResultsPanel data={mut.data} target={target} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Results — APG panel + CMS panel + per-line side-by-side table
// ---------------------------------------------------------------------------
function ResultsPanel({ data, target }) {
  const { apg, cms_lines, cms_locality_used, warnings } = data;

  return (
    <div className="space-y-4">
      {warnings?.length > 0 && (
        <div className="card p-3 border-l-4 border-l-warning">
          <div className="flex items-start gap-2 text-sm text-warning-700">
            <Info className="w-4 h-4 mt-0.5 shrink-0" aria-hidden />
            <ul className="space-y-0.5">
              {warnings.map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          </div>
        </div>
      )}

      {apg && <APGResultCard apg={apg} />}

      {cms_lines && (
        <CMSResultCard cms_lines={cms_lines} cms_locality_used={cms_locality_used} />
      )}

      {apg && cms_lines && (
        <SideBySideCard apg={apg} cms_lines={cms_lines} />
      )}
    </div>
  );
}

function APGResultCard({ apg }) {
  const sign = varianceSign(apg.variance);
  const varClass =
    sign === 'under' ? 'text-danger-700'
    : sign === 'over' ? 'text-warning-700'
    : 'text-slate-700';

  return (
    <div className="card p-5">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide">
            APG result — {apg.peer_group} · {apg.region}
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Base rate: {fmtCurrency(apg.base_rate_applied)}
          </p>
        </div>
        <div className="flex flex-wrap gap-1">
          {apg.discounting_applied && <span className="pill-warning">Multi-proc discount</span>}
          {apg.u6_applied && <span className="pill-warning">U6</span>}
          {apg.capital_applied && <span className="pill-brand">Capital add-on</span>}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
        <Stat label="Correct APG payment" value={fmtCurrency(apg.correct_apg_payment)} strong />
        <Stat label="Billed total" value={fmtCurrency(apg.actual_paid)} />
        <div>
          <div className="kpi-label">Variance</div>
          <div className={`kpi-value ${varClass}`}>{fmtCurrency(apg.variance)}</div>
        </div>
        <div>
          <div className="kpi-label">Compression</div>
          <div className={`kpi-value ${varClass}`}>{fmtPercent(apg.compression_pct, 2)}</div>
        </div>
      </div>

      <table className="w-full text-sm">
        <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
          <tr>
            <th className="text-left px-3 py-2 font-semibold w-8">#</th>
            <th className="text-left px-3 py-2 font-semibold">Proc</th>
            <th className="text-left px-3 py-2 font-semibold">EAPG</th>
            <th className="text-left px-3 py-2 font-semibold">Type</th>
            <th className="text-right px-3 py-2 font-semibold">Weight</th>
            <th className="text-right px-3 py-2 font-semibold">Expected</th>
            <th className="text-left px-3 py-2 font-semibold">Flags</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {apg.line_details.map((ld) => (
            <tr key={ld.line_seq}>
              <td className="px-3 py-2 text-xs font-mono text-slate-400">{ld.line_seq}</td>
              <td className="px-3 py-2 font-mono text-xs">{ld.procedure_code}</td>
              <td className="px-3 py-2 text-xs">
                {ld.eapg ? `${ld.eapg} — ${ld.eapg_desc || ''}` : <span className="text-slate-400">—</span>}
              </td>
              <td className="px-3 py-2 text-xs text-slate-600">{ld.eapg_type || '—'}</td>
              <td className="px-3 py-2 text-right tabular-nums text-xs">
                {ld.weight !== null && ld.weight !== undefined
                  ? Number(ld.weight).toFixed(4)
                  : '—'}
              </td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCurrency(ld.expected_payment)}</td>
              <td className="px-3 py-2 text-xs text-slate-500">
                <div className="flex flex-wrap gap-1">
                  {ld.packaged && <span className="pill-slate">Packaged</span>}
                  {ld.discounted && <span className="pill-warning">50%</span>}
                  {ld.u6_applied && <span className="pill-warning">U6</span>}
                  {ld.denied && <span className="pill-danger">Denied</span>}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CMSResultCard({ cms_lines, cms_locality_used }) {
  return (
    <div className="card p-5">
      <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide mb-3">
        CMS MPFS result — locality {cms_locality_used}
      </h2>
      <table className="w-full text-sm">
        <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
          <tr>
            <th className="text-left px-3 py-2 font-semibold w-8">#</th>
            <th className="text-right px-3 py-2 font-semibold">Non-facility</th>
            <th className="text-right px-3 py-2 font-semibold">Facility</th>
            <th className="text-right px-3 py-2 font-semibold">Work RVU</th>
            <th className="text-right px-3 py-2 font-semibold">PE RVU</th>
            <th className="text-right px-3 py-2 font-semibold">MP RVU</th>
            <th className="text-right px-3 py-2 font-semibold">Total RVU</th>
            <th className="text-right px-3 py-2 font-semibold">Expected</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {cms_lines.map((l, i) => l.error ? (
            <tr key={i}>
              <td className="px-3 py-2 text-xs font-mono text-slate-400">{i + 1}</td>
              <td colSpan={7} className="px-3 py-2 text-xs text-danger-700">
                <AlertCircle className="inline w-3 h-3 mr-1" aria-hidden /> {l.error}
              </td>
            </tr>
          ) : (
            <tr key={i}>
              <td className="px-3 py-2 text-xs font-mono text-slate-400">{i + 1}</td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCurrency(l.non_facility_rate)}</td>
              <td className="px-3 py-2 text-right tabular-nums">{fmtCurrency(l.facility_rate)}</td>
              <td className="px-3 py-2 text-right tabular-nums text-xs">{fmtRvu(l.work_rvu)}</td>
              <td className="px-3 py-2 text-right tabular-nums text-xs">{fmtRvu(l.pe_rvu)}</td>
              <td className="px-3 py-2 text-right tabular-nums text-xs">{fmtRvu(l.mp_rvu)}</td>
              <td className="px-3 py-2 text-right tabular-nums text-xs">{fmtRvu(l.total_rvu)}</td>
              <td className="px-3 py-2 text-right tabular-nums font-semibold">
                {fmtCurrency(l.expected_payment)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SideBySideCard({ apg, cms_lines }) {
  return (
    <div className="card p-5">
      <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide mb-3">
        Side-by-side comparison
      </h2>
      <table className="w-full text-sm">
        <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
          <tr>
            <th className="text-left px-3 py-2 font-semibold w-8">#</th>
            <th className="text-left px-3 py-2 font-semibold">Proc</th>
            <th className="text-right px-3 py-2 font-semibold">APG expected</th>
            <th className="text-right px-3 py-2 font-semibold">CMS expected</th>
            <th className="text-right px-3 py-2 font-semibold">Difference</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {apg.line_details.map((ld, i) => {
            const cms = cms_lines[i];
            const apgExp = parseFloat(ld.expected_payment || 0);
            const cmsExp = cms && !cms.error
              ? parseFloat(cms.expected_payment || 0) : null;
            const diff = cmsExp !== null ? apgExp - cmsExp : null;
            const diffCls = diff === null ? 'text-slate-400'
              : diff > 0 ? 'text-success-700'
              : diff < 0 ? 'text-danger-700'
              : 'text-slate-700';
            return (
              <tr key={i}>
                <td className="px-3 py-2 text-xs font-mono text-slate-400">{ld.line_seq}</td>
                <td className="px-3 py-2 font-mono text-xs">{ld.procedure_code}</td>
                <td className="px-3 py-2 text-right tabular-nums">{fmtCurrency(apgExp)}</td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {cmsExp !== null ? fmtCurrency(cmsExp) : (
                    <span className="text-slate-400 text-xs">{cms?.error ? '—' : 'n/a'}</span>
                  )}
                </td>
                <td className={`px-3 py-2 text-right tabular-nums font-semibold ${diffCls}`}>
                  {diff !== null ? fmtCurrency(diff) : '—'}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="text-xs text-slate-500 mt-3">
        Difference = APG expected − CMS expected. Positive means the APG rate is higher; negative means CMS is higher.
      </p>
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

function fmtRvu(v) {
  if (v === null || v === undefined || v === '') return '—';
  return Number(v).toFixed(2);
}
