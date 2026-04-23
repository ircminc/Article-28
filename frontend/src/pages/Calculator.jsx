import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import {
  Calculator as CalcIcon, Plus, Trash2, Play,
  AlertCircle, Info,
} from 'lucide-react';
import { calculateRate, extractErrorMessage } from '../services/api.js';
import { fmtCurrency, fmtPercent, varianceSign } from '../utils/format.js';
import LocalitySelect from '../components/LocalitySelect.jsx';

// Manual CPT/ICD entry calculator with full math-chain transparency.
// Each APG line shows: EAPG assignment → weight → base rate → modifiers →
// final rounded payment, so a billing analyst can audit exactly how the
// number was arrived at. CMS side shows per-line non-facility / facility /
// (optional) professional / (optional) technical rates plus all RVUs.

const TODAY = new Date().toISOString().slice(0, 10);

const EMPTY_LINE = {
  procedure_code: '',
  modifiers: ['', '', '', ''],
  units: 1,
  billed_amount: '',   // semantically "paid", but keeps the API field name
};

export default function Calculator() {
  const [dos, setDos] = useState(TODAY);
  const [principalDx, setPrincipalDx] = useState('');
  const [otherDx, setOtherDx] = useState('');
  // CMS MPFS integration rebuilt against the new pfs.data.cms.gov API
  // (DKAN datastore + RVU × GPCI × CF formula). 'Both' is a reasonable
  // default again.
  const [target, setTarget] = useState('both');
  const [cmsLocality, setCmsLocality] = useState('');
  const [useFacilityRate, setUseFacilityRate] = useState(false);
  const [includePcTc, setIncludePcTc] = useState(false);
  const [lines, setLines] = useState([{ ...EMPTY_LINE, modifiers: ['', '', '', ''] }]);

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
    if (includePcTc) payload.cms_include_pc_tc = true;
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
            CMS MPFS (Medicare professional) rates for a date of service, with the
            full calculation broken down so you can audit every number.
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

        {/* CMS-specific row */}
        {(target === 'cms' || target === 'both') && (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 border-t border-slate-200 pt-4">
            <LocalitySelect
              year={dos ? parseInt(dos.slice(0, 4), 10) : new Date().getFullYear()}
              value={cmsLocality}
              onChange={setCmsLocality}
              allowProviderDefault
              id="cms-locality"
            />
            <div className="md:col-span-2 space-y-2">
              <label className="label block">CMS options</label>
              <label className="inline-flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="accent-primary"
                  checked={useFacilityRate}
                  onChange={(e) => setUseFacilityRate(e.target.checked)}
                />
                Use facility rate (hospital outpatient) instead of non-facility (office)
              </label>
              <label className="inline-flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="accent-primary"
                  checked={includePcTc}
                  onChange={(e) => setIncludePcTc(e.target.checked)}
                />
                Show professional (-26) + technical (-TC) component split
                <span className="text-xs text-slate-400">(adds 2 API calls per line)</span>
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
            <button type="button" className="btn-secondary" onClick={addLine}>
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
                  <th className="text-right font-semibold px-3 py-2 w-32">
                    Paid $ <span className="text-slate-400 normal-case font-normal">(optional)</span>
                  </th>
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
                        onChange={(e) => updateLine(i, { procedure_code: e.target.value })}
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
          <p className="text-xs text-slate-500 mt-2">
            <strong>Paid $</strong> is what the payer actually paid you — used only to
            show variance (correct APG − paid). Leave blank if you just want to see
            the expected rate.
          </p>
        </div>

        <div className="flex justify-end pt-2 border-t border-slate-200">
          <button type="submit" className="btn-primary" disabled={mut.isPending}>
            <Play className="w-4 h-4" aria-hidden />
            {mut.isPending ? 'Calculating…' : 'Calculate'}
          </button>
        </div>
      </form>

      {mut.isError && (
        <div className="card p-4 border-l-4 border-l-danger">
          <span className="pill-danger">
            <AlertCircle className="w-3 h-3" aria-hidden /> {extractErrorMessage(mut.error)}
          </span>
        </div>
      )}

      {mut.isSuccess && <ResultsPanel data={mut.data} useFacilityRate={useFacilityRate} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------
function ResultsPanel({ data, useFacilityRate }) {
  const { apg, icd_based_eapg, cms_lines, cms_locality_used, warnings } = data;

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

      {apg && <APGResultCard apg={apg} icdBased={icd_based_eapg} />}
      {cms_lines && (
        <CMSResultCard
          cms_lines={cms_lines}
          cms_locality_used={cms_locality_used}
          useFacilityRate={useFacilityRate}
        />
      )}
      {apg && cms_lines && <SideBySideCard apg={apg} cms_lines={cms_lines} useFacilityRate={useFacilityRate} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// APG result — provider context + per-line math chain
// ---------------------------------------------------------------------------
function APGResultCard({ apg, icdBased }) {
  const sign = varianceSign(apg.variance);
  const varClass =
    sign === 'under' ? 'text-danger-700'
    : sign === 'over' ? 'text-warning-700'
    : 'text-slate-700';

  const totalWeight = apg.line_details.reduce(
    (acc, ld) => acc + (ld.weight ? parseFloat(ld.weight) : 0), 0,
  );

  return (
    <div className="card p-5 space-y-5">
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide">
            APG result — Article 28
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Results computed using NYS DOH APG methodology and reference data loaded from
            the workbook.
          </p>
        </div>
        <div className="flex flex-wrap gap-1">
          {apg.discounting_applied && <span className="pill-warning">Multi-proc discount</span>}
          {apg.u6_applied && <span className="pill-warning">U6 modifier</span>}
          {apg.capital_applied && <span className="pill-brand">Capital add-on</span>}
        </div>
      </div>

      {/* Provider context block — the inputs that drove the calculation */}
      <section className="bg-slate-50 rounded-md p-4 text-sm">
        <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">
          Factors used in this calculation
        </div>
        <dl className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Factor label="Peer group" value={apg.peer_group} />
          <Factor label="Region" value={apg.region} />
          <Factor label="Base rate" value={fmtCurrency(apg.base_rate_applied)} strong />
          <Factor label="Total weight (paid lines)" value={totalWeight.toFixed(4)} />
          <Factor label="Multi-proc discount" value={apg.discounting_applied ? 'Yes (50% on secondary)' : 'No'} />
          <Factor label="U6 modifier" value={apg.u6_applied ? 'Yes' : 'No'} />
          <Factor label="Capital add-on" value={
            apg.capital_applied
              ? fmtCurrency(apg.capital_addon_amount || 0)
              : 'Not eligible'} />
          <Factor label="Rounding" value="Half-up, 2dp" />
        </dl>
        <p className="text-xs text-slate-500 mt-3">
          Formula per line: <code>Line payment = EAPG weight × base rate × (0.5 if discounted) × (U6 factor if applied) × units</code>.
          Incidental-type EAPGs and zero-weight codes are <em>packaged</em> (bundled into the primary significant procedure — no separate payment).
        </p>
      </section>

      {/* Totals band */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Stat label="Correct APG payment" value={fmtCurrency(apg.correct_apg_payment)} strong />
        <Stat label="Paid total" value={fmtCurrency(apg.actual_paid)} />
        <div>
          <div className="kpi-label">Variance</div>
          <div className={`kpi-value ${varClass}`}>{fmtCurrency(apg.variance)}</div>
        </div>
        <div>
          <div className="kpi-label">Compression</div>
          <div className={`kpi-value ${varClass}`}>{fmtPercent(apg.compression_pct, 2)}</div>
        </div>
      </div>

      {/* Primary ICD-derived EAPG (informational — not in the payment total) */}
      {icdBased && <ICDBasedSection icd={icdBased} />}

      {/* Per-line math chain */}
      <div>
        <h3 className="text-xs uppercase tracking-wide text-slate-500 mb-2">
          Per-line math (HCPCS-driven — official APG payment)
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left px-3 py-2 font-semibold w-8">#</th>
                <th className="text-left px-3 py-2 font-semibold">Proc · EAPG</th>
                <th className="text-left px-3 py-2 font-semibold">Calculation</th>
                <th className="text-right px-3 py-2 font-semibold">Expected</th>
                <th className="text-right px-3 py-2 font-semibold">Paid</th>
                <th className="text-right px-3 py-2 font-semibold">Variance</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {apg.line_details.map((ld) => (
                <APGLineRow key={ld.line_seq} ld={ld} baseRate={apg.base_rate_applied} />
              ))}
              {/* Capital add-on row, if applied */}
              {apg.capital_applied && (
                <tr className="bg-brand-50/30">
                  <td className="px-3 py-2"></td>
                  <td className="px-3 py-2 text-xs font-semibold text-brand-900">Capital add-on</td>
                  <td className="px-3 py-2 text-xs text-slate-500">Per-provider flat amount added to claim total</td>
                  <td className="px-3 py-2 text-right tabular-nums font-semibold">
                    {fmtCurrency(apg.capital_addon_amount || 0)}
                  </td>
                  <td></td><td></td>
                </tr>
              )}
              {/* Totals row */}
              <tr className="bg-slate-100 font-semibold">
                <td className="px-3 py-2"></td>
                <td className="px-3 py-2 text-xs uppercase tracking-wide">Totals</td>
                <td className="px-3 py-2"></td>
                <td className="px-3 py-2 text-right tabular-nums">{fmtCurrency(apg.correct_apg_payment)}</td>
                <td className="px-3 py-2 text-right tabular-nums">{fmtCurrency(apg.actual_paid)}</td>
                <td className={`px-3 py-2 text-right tabular-nums ${varClass}`}>
                  {fmtCurrency(apg.variance)}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ICD-derived EAPG — informational block shown above the HCPCS-driven math.
// The payment is never sourced from this block; per NYS DOH methodology the
// per-line HCPCS assignments drive payment. This section exists to let users
// verify the system recognizes the principal diagnosis and to see what that
// diagnosis would pay if the claim were priced purely on a medical-visit
// basis.
// ---------------------------------------------------------------------------
function ICDBasedSection({ icd }) {
  const resolved = icd.eapg !== null && icd.eapg !== undefined;
  const displayDx =
    icd.input_dx_code && icd.input_dx_code !== icd.dx_code
      ? `${icd.input_dx_code} → ${icd.dx_code}`
      : (icd.input_dx_code || icd.dx_code);

  return (
    <section className="bg-brand-50/40 border border-brand-100 rounded-md p-4 text-sm">
      <div className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <h3 className="text-xs uppercase tracking-wide text-brand-900 font-semibold">
          Primary ICD-derived EAPG (informational)
        </h3>
        <span className="text-[11px] text-slate-500 italic">
          Not included in the payment total — shown for transparency
        </span>
      </div>

      {!resolved ? (
        <div className="text-slate-600">
          <div className="font-medium">Principal ICD-10: <code>{displayDx}</code></div>
          <div className="text-xs text-slate-500 mt-1">
            {icd.note || 'No EAPG mapping found for this diagnosis on the given date of service.'}
          </div>
        </div>
      ) : (
        <>
          <dl className="grid grid-cols-2 md:grid-cols-5 gap-3">
            <Factor label="Principal ICD-10" value={<code className="text-[13px]">{displayDx}</code>} strong />
            <Factor label="Resolved EAPG" value={`${icd.eapg}${icd.eapg_desc ? ` · ${icd.eapg_desc}` : ''}`} />
            <Factor label="EAPG type" value={icd.eapg_type_raw || icd.eapg_type || '—'} />
            <Factor
              label="Weight"
              value={icd.weight !== null && icd.weight !== undefined
                ? parseFloat(icd.weight).toFixed(4)
                : '—'}
            />
            <Factor
              label="Indicative payment"
              value={icd.indicative_payment !== null && icd.indicative_payment !== undefined
                ? fmtCurrency(icd.indicative_payment)
                : '—'}
              strong
            />
          </dl>
          {icd.weight !== null && icd.weight !== undefined ? (
            <p className="text-xs text-slate-500 mt-3">
              Indicative payment = <code>{parseFloat(icd.weight).toFixed(4)} × {fmtCurrency(icd.base_rate)} = {fmtCurrency(icd.indicative_payment)}</code>.
              What the claim would pay if billed as a single medical-visit line under this EAPG. Actual payment is driven by the per-line HCPCS math below.
            </p>
          ) : (
            <p className="text-xs text-slate-500 mt-3">
              {icd.note || 'Weight lookup returned no row for this EAPG — indicative rate unavailable.'}
            </p>
          )}
        </>
      )}
    </section>
  );
}


function APGLineRow({ ld, baseRate }) {
  // Build the human-readable calculation string, e.g.:
  //   "2.4000 × $169.02 × 1 unit = $405.65"
  //   "Packaged — Incidental EAPG"
  //   "2.4000 × $169.02 × 50% (secondary) = $202.82"
  const weight = ld.weight !== null && ld.weight !== undefined
    ? Number(ld.weight) : null;
  const expected = parseFloat(ld.expected_payment || 0);
  const paid = parseFloat(ld.actual_paid || 0);
  const variance = expected - paid;
  const varClass = variance > 0 ? 'text-danger-700'
                 : variance < 0 ? 'text-warning-700' : 'text-slate-700';

  let calcParts = [];
  if (ld.packaged || expected === 0) {
    if (ld.packaged) {
      calcParts.push(<em key="pkg">Packaged — no separate payment.</em>);
    } else if (weight === 0) {
      calcParts.push(<em key="zw">Weight 0 for this DOS — not separately payable.</em>);
    } else {
      calcParts.push(<em key="noeapg">No EAPG match — cannot price.</em>);
    }
  } else if (weight !== null) {
    const parts = [];
    parts.push(<span key="w" className="font-mono">{weight.toFixed(4)}</span>);
    parts.push(<span key="mul1"> × </span>);
    parts.push(<span key="br" className="font-mono">{fmtCurrency(baseRate)}</span>);
    if (ld.discounted) {
      parts.push(<span key="mul2"> × </span>);
      parts.push(<span key="disc" className="font-mono text-warning-700">50%</span>);
    }
    if (ld.u6_applied) {
      parts.push(<span key="mul3"> × </span>);
      parts.push(<span key="u6" className="font-mono text-warning-700">U6 factor</span>);
    }
    parts.push(<span key="eq"> = </span>);
    parts.push(<span key="ans" className="font-semibold">{fmtCurrency(expected)}</span>);
    calcParts = parts;
  }

  return (
    <tr>
      <td className="px-3 py-2 text-xs font-mono text-slate-400">{ld.line_seq}</td>
      <td className="px-3 py-2 text-xs">
        <div className="font-mono">{ld.procedure_code}</div>
        <div className="text-slate-500 text-[11px] mt-0.5">
          {ld.eapg ? `EAPG ${ld.eapg} · ${ld.eapg_type || ''}` : <span className="text-slate-400">no EAPG match</span>}
        </div>
        {ld.eapg_desc && (
          <div className="text-slate-400 text-[11px] italic">{ld.eapg_desc}</div>
        )}
      </td>
      <td className="px-3 py-2 text-xs text-slate-700">
        {calcParts}
        {/* Engine notes (packaging reasons, etc.) */}
        {ld.notes?.length > 0 && (
          <ul className="mt-1 space-y-0.5 text-[11px] text-slate-500 list-disc pl-4">
            {ld.notes.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        )}
        {(ld.modifiers || []).length > 0 && (
          <div className="mt-1 text-[11px] text-slate-500">
            Modifiers: <span className="font-mono">{ld.modifiers.join(', ')}</span>
          </div>
        )}
      </td>
      <td className="px-3 py-2 text-right tabular-nums">{fmtCurrency(expected)}</td>
      <td className="px-3 py-2 text-right tabular-nums">{fmtCurrency(paid)}</td>
      <td className={`px-3 py-2 text-right tabular-nums ${varClass}`}>
        {paid > 0 ? fmtCurrency(variance) : <span className="text-slate-400">—</span>}
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// CMS result — per-line 4-column rate grid + RVUs + conversion factor
// ---------------------------------------------------------------------------
function CMSResultCard({ cms_lines, cms_locality_used, useFacilityRate }) {
  // Show the conversion factor from the first non-error line (constant per year/locality)
  const firstRate = cms_lines.find((l) => !l.error);
  const cf = firstRate?.conversion_factor;

  // Hide PC/TC columns entirely if no line has them populated (the user didn't
  // opt in, or none of their codes have a PC/TC split)
  const anyPcTc = cms_lines.some((l) => l.professional_rate != null || l.technical_rate != null);

  return (
    <div className="card p-5 space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide">
          CMS MPFS result — Medicare professional
        </h2>
        <p className="text-xs text-slate-500 mt-0.5">
          Rates from the CMS data.cms.gov Physician Fee Schedule dataset.
          Cached locally for 24h per (HCPCS, modifier, locality, year).
        </p>
      </div>

      <section className="bg-slate-50 rounded-md p-4 text-sm">
        <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">
          Factors used in this calculation
        </div>
        <dl className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Factor label="Locality" value={cms_locality_used || '—'} strong />
          <Factor label="Place of service" value={useFacilityRate ? 'Facility' : 'Non-facility'} />
          <Factor label="Conversion factor (CF)" value={cf ? fmtCurrency(cf) : '—'} />
          <Factor label="Expected per line" value={useFacilityRate ? 'Facility rate × units' : 'Non-facility rate × units'} />
        </dl>
        <p className="text-xs text-slate-500 mt-3">
          Formula: <code>Payment = (Work RVU + PE RVU + MP RVU) × GPCI factors × CF × units</code>.
          CMS returns the computed total rate; the RVU columns below show the breakdown for audit.
          {anyPcTc && <> Professional (-26) and technical (-TC) columns show the PC/TC split where applicable.</>}
        </p>
      </section>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
            <tr>
              <th className="text-left px-3 py-2 font-semibold w-8">#</th>
              <th className="text-left px-3 py-2 font-semibold">Proc</th>
              <th className="text-right px-3 py-2 font-semibold">Non-facility</th>
              <th className="text-right px-3 py-2 font-semibold">Facility</th>
              {anyPcTc && <th className="text-right px-3 py-2 font-semibold">Professional</th>}
              {anyPcTc && <th className="text-right px-3 py-2 font-semibold">Technical</th>}
              <th className="text-right px-3 py-2 font-semibold">Work</th>
              <th className="text-right px-3 py-2 font-semibold">PE</th>
              <th className="text-right px-3 py-2 font-semibold">MP</th>
              <th className="text-right px-3 py-2 font-semibold">Total RVU</th>
              <th className="text-right px-3 py-2 font-semibold">Expected</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {cms_lines.map((l, i) => l.error ? (
              <tr key={i}>
                <td className="px-3 py-2 text-xs font-mono text-slate-400">{i + 1}</td>
                <td colSpan={anyPcTc ? 10 : 8} className="px-3 py-2 text-xs text-danger-700">
                  <AlertCircle className="inline w-3 h-3 mr-1" aria-hidden /> {l.error}
                </td>
              </tr>
            ) : (
              <tr key={i}>
                <td className="px-3 py-2 text-xs font-mono text-slate-400">{i + 1}</td>
                <td className="px-3 py-2 text-xs font-mono">{l.procedure_code || '—'}</td>
                <td className={`px-3 py-2 text-right tabular-nums ${!useFacilityRate ? 'font-semibold' : ''}`}>
                  {fmtCurrency(l.non_facility_rate)}
                </td>
                <td className={`px-3 py-2 text-right tabular-nums ${useFacilityRate ? 'font-semibold' : ''}`}>
                  {fmtCurrency(l.facility_rate)}
                </td>
                {anyPcTc && (
                  <td className="px-3 py-2 text-right tabular-nums">
                    {l.professional_rate != null ? fmtCurrency(l.professional_rate) : <span className="text-slate-300">—</span>}
                  </td>
                )}
                {anyPcTc && (
                  <td className="px-3 py-2 text-right tabular-nums">
                    {l.technical_rate != null ? fmtCurrency(l.technical_rate) : <span className="text-slate-300">—</span>}
                  </td>
                )}
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
      {!anyPcTc && (
        <p className="text-xs text-slate-500">
          Tip: enable the <strong>professional (-26) + technical (-TC)</strong> checkbox above
          to see the PC/TC component split for codes that have one (typically radiology, pathology, some cardio).
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Side-by-side comparison
// ---------------------------------------------------------------------------
function SideBySideCard({ apg, cms_lines, useFacilityRate }) {
  return (
    <div className="card p-5">
      <h2 className="text-sm font-semibold text-brand-900 uppercase tracking-wide mb-3">
        APG vs CMS — side by side
      </h2>
      <table className="w-full text-sm">
        <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
          <tr>
            <th className="text-left px-3 py-2 font-semibold w-8">#</th>
            <th className="text-left px-3 py-2 font-semibold">Proc</th>
            <th className="text-right px-3 py-2 font-semibold">APG expected</th>
            <th className="text-right px-3 py-2 font-semibold">
              CMS expected
              <div className="font-normal text-[10px] text-slate-400">
                ({useFacilityRate ? 'facility' : 'non-facility'})
              </div>
            </th>
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
        Difference = APG expected − CMS expected. Positive: APG pays more than Medicare for this service. Negative: Medicare pays more.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------
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

function Factor({ label, value, strong = false }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className={strong ? 'text-sm font-semibold text-brand-900 tabular-nums' : 'text-sm text-slate-700 tabular-nums'}>
        {value || '—'}
      </dd>
    </div>
  );
}

function fmtRvu(v) {
  if (v === null || v === undefined || v === '') return '—';
  return Number(v).toFixed(2);
}
