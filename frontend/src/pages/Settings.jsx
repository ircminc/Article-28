import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Save, AlertCircle, CheckCircle2, Trash2, RefreshCw, Upload as UploadIcon } from 'lucide-react';
import {
  clearAllClaims,
  clearCmsCache,
  extractErrorMessage,
  getProvider,
  reloadReferenceData,
  upsertProvider,
} from '../services/api.js';
import { useAuth } from '../auth/AuthContext.jsx';

// Lists mirror the reference-data categorical domains. Keep them in sync with
// the backend: peer_group values must match rows in apg_base_rates.peer_group,
// provider_type is 'dtc' | 'hospital', region is derived from county_code.
const PEER_GROUPS = [
  'Clinic*',
  'Clinic Episode*',
  'Amb Surg',
  'Renal',
  'Renal Episode',
  'SBHC*',
  'SBHC Episode*',
  'Clinic MR/DD/TBI',
  'Clinic MR/DD/TBI Episode',
  'Clinic MR/DD/TBI*',
  'Clinic MR/DD/TBI Episode*',
  'Academic Dental',
  'Academic Dental Episode',
  'Emergency Department (ED)',
];

const PROVIDER_TYPES = [
  { value: 'dtc', label: 'DTC / Freestanding Clinic' },
  { value: 'hospital', label: 'Hospital Outpatient' },
];

// NY counties hardcoded here as a fallback for the dropdown. At runtime we
// could pull these from the backend, but since the list rarely changes and
// has 62 items, a static list keeps the form simple. county_code values
// must match provider_county.county_code in the backend.
const NY_COUNTIES = [
  { code: 1, name: 'Albany' }, { code: 2, name: 'Allegany' }, { code: 58, name: 'Bronx' },
  { code: 4, name: 'Broome' }, { code: 5, name: 'Cattaraugus' }, { code: 6, name: 'Cayuga' },
  { code: 7, name: 'Chautauqua' }, { code: 8, name: 'Chemung' }, { code: 9, name: 'Chenango' },
  { code: 10, name: 'Clinton' }, { code: 11, name: 'Columbia' }, { code: 12, name: 'Cortland' },
  { code: 13, name: 'Delaware' }, { code: 14, name: 'Dutchess' }, { code: 15, name: 'Erie' },
  { code: 16, name: 'Essex' }, { code: 17, name: 'Franklin' }, { code: 18, name: 'Fulton' },
  { code: 19, name: 'Genesee' }, { code: 20, name: 'Greene' }, { code: 21, name: 'Hamilton' },
  { code: 22, name: 'Herkimer' }, { code: 23, name: 'Jefferson' }, { code: 24, name: 'Kings' },
  { code: 25, name: 'Lewis' }, { code: 26, name: 'Livingston' }, { code: 27, name: 'Madison' },
  { code: 28, name: 'Nassau' }, { code: 29, name: 'New York' }, { code: 30, name: 'Niagara' },
  { code: 31, name: 'Oneida' }, { code: 32, name: 'Onondaga' }, { code: 33, name: 'Ontario' },
  { code: 34, name: 'Orange' }, { code: 35, name: 'Orleans' }, { code: 36, name: 'Oswego' },
  { code: 37, name: 'Otsego' }, { code: 38, name: 'Putnam' }, { code: 59, name: 'Rensselaer' },
  { code: 60, name: 'Richmond' }, { code: 41, name: 'Rockland' }, { code: 42, name: 'Saratoga' },
  { code: 43, name: 'Schenectady' }, { code: 44, name: 'Schoharie' }, { code: 45, name: 'Schuyler' },
  { code: 46, name: 'Seneca' }, { code: 47, name: 'Suffolk' }, { code: 48, name: 'Sullivan' },
  { code: 49, name: 'Tioga' }, { code: 50, name: 'Tompkins' }, { code: 51, name: 'Ulster' },
  { code: 52, name: 'Warren' }, { code: 53, name: 'Washington' }, { code: 54, name: 'Wayne' },
  { code: 55, name: 'Westchester' }, { code: 56, name: 'Wyoming' }, { code: 57, name: 'Yates' },
  { code: 61, name: 'Queens' }, { code: 62, name: 'St. Lawrence' }, { code: 63, name: 'Chenango' },
].sort((a, b) => a.name.localeCompare(b.name));

const EMPTY = {
  provider_name: '',
  npi: '',
  county_code: '',
  peer_group: 'Clinic*',
  provider_type: 'dtc',
  capital_addon_eligible: false,
  capital_addon_rate: '',
  cms_locality: '',
  rate_code_override: '',
};

export default function Settings() {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const { data: existing, isLoading } = useQuery({
    queryKey: ['provider'],
    queryFn: getProvider,
    retry: false,
  });

  const [form, setForm] = useState(EMPTY);

  useEffect(() => {
    if (existing) {
      setForm({
        provider_name: existing.provider_name ?? '',
        npi: existing.npi ?? '',
        county_code: existing.county_code ?? '',
        peer_group: existing.peer_group ?? 'Clinic*',
        provider_type: existing.provider_type ?? 'dtc',
        capital_addon_eligible: existing.capital_addon_eligible ?? false,
        capital_addon_rate: existing.capital_addon_rate ?? '',
        cms_locality: existing.cms_locality ?? '',
        rate_code_override: existing.rate_code_override ?? '',
      });
    }
  }, [existing]);

  const mutation = useMutation({
    mutationFn: (payload) => upsertProvider(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['provider'] });
    },
  });

  function handleChange(field, value) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  function handleSubmit(e) {
    e.preventDefault();
    const payload = {
      provider_name: form.provider_name.trim(),
      npi: form.npi.trim() || null,
      county_code: form.county_code ? Number(form.county_code) : null,
      peer_group: form.peer_group,
      provider_type: form.provider_type,
      capital_addon_eligible: form.capital_addon_eligible,
      capital_addon_rate: form.capital_addon_rate
        ? String(form.capital_addon_rate)
        : null,
      cms_locality: form.cms_locality.trim() || null,
      rate_code_override: form.rate_code_override.trim() || null,
    };
    mutation.mutate(payload);
  }

  if (isLoading) return <div className="text-slate-500">Loading…</div>;

  const derivedRegion =
    NY_COUNTIES.find((c) => c.code === Number(form.county_code))?.region || null;

  return (
    <div className="max-w-3xl space-y-4">
      <header>
        <h1 className="text-xl font-semibold text-brand-900">Provider configuration</h1>
        <p className="text-sm text-slate-500 mt-1">
          This drives the APG engine. Peer group + county (→ region) + date of
          service selects the base rate. Capital add-on and CMS locality apply to
          eligible providers.
        </p>
      </header>

      <form onSubmit={handleSubmit} className="card p-6 space-y-5">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="md:col-span-2">
            <label className="label" htmlFor="provider_name">Provider name *</label>
            <input
              id="provider_name"
              className="input"
              required
              value={form.provider_name}
              onChange={(e) => handleChange('provider_name', e.target.value)}
              placeholder="Sample Clinic LLC"
            />
          </div>

          <div>
            <label className="label" htmlFor="npi">NPI</label>
            <input
              id="npi"
              className="input"
              value={form.npi}
              onChange={(e) => handleChange('npi', e.target.value)}
              placeholder="1234567890"
              maxLength={10}
              pattern="\d{10}"
              title="10-digit NPI"
            />
          </div>

          <div>
            <label className="label" htmlFor="county">County</label>
            <select
              id="county"
              className="select"
              value={form.county_code}
              onChange={(e) => handleChange('county_code', e.target.value)}
            >
              <option value="">— Select —</option>
              {NY_COUNTIES.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="label" htmlFor="peer_group">Peer group *</label>
            <select
              id="peer_group"
              className="select"
              value={form.peer_group}
              onChange={(e) => handleChange('peer_group', e.target.value)}
            >
              {PEER_GROUPS.map((pg) => (
                <option key={pg} value={pg}>{pg}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="label" htmlFor="provider_type">Provider type *</label>
            <select
              id="provider_type"
              className="select"
              value={form.provider_type}
              onChange={(e) => handleChange('provider_type', e.target.value)}
            >
              {PROVIDER_TYPES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>

          <div className="md:col-span-2 border-t border-slate-200 pt-5 mt-1">
            <label className="inline-flex items-center gap-2 text-sm font-medium text-slate-700">
              <input
                type="checkbox"
                className="accent-primary"
                checked={form.capital_addon_eligible}
                onChange={(e) => handleChange('capital_addon_eligible', e.target.checked)}
              />
              Eligible for capital add-on
            </label>
          </div>

          {form.capital_addon_eligible && (
            <div>
              <label className="label" htmlFor="capital_addon_rate">Capital add-on rate ($)</label>
              <input
                id="capital_addon_rate"
                type="number"
                step="0.01"
                min="0"
                className="input"
                value={form.capital_addon_rate}
                onChange={(e) => handleChange('capital_addon_rate', e.target.value)}
                placeholder="e.g. 25.00"
              />
            </div>
          )}

          <div>
            <label className="label" htmlFor="cms_locality">CMS locality (for 835P)</label>
            <input
              id="cms_locality"
              className="input"
              value={form.cms_locality}
              onChange={(e) => handleChange('cms_locality', e.target.value)}
              placeholder="e.g. 01"
            />
          </div>

          <div>
            <label className="label" htmlFor="rate_code_override">Rate code override (optional)</label>
            <input
              id="rate_code_override"
              className="input"
              value={form.rate_code_override}
              onChange={(e) => handleChange('rate_code_override', e.target.value)}
              placeholder="e.g. 1407"
            />
          </div>
        </div>

        {derivedRegion && (
          <div className="text-xs text-slate-500">
            Region will be derived from county at save time.
          </div>
        )}

        <div className="flex items-center justify-between pt-4 border-t border-slate-200">
          <StatusLine mutation={mutation} existing={existing} />
          <button type="submit" className="btn-primary" disabled={mutation.isPending}>
            <Save className="w-4 h-4" aria-hidden />
            {mutation.isPending ? 'Saving…' : 'Save configuration'}
          </button>
        </div>
      </form>

      {/* Reference data management — admin + analyst */}
      {user && (user.role === 'admin' || user.role === 'analyst') && (
        <ReferenceDataTools queryClient={queryClient} isAdmin={user.role === 'admin'} />
      )}

      {/* Danger zone — admin + analyst only */}
      {user && (user.role === 'admin' || user.role === 'analyst') && (
        <DangerZone queryClient={queryClient} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Reference data management — CMS cache refresh + APG workbook upload
// ---------------------------------------------------------------------------

function ReferenceDataTools({ queryClient, isAdmin }) {
  const cmsMut = useMutation({
    mutationFn: clearCmsCache,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['health'] }),
  });

  const [workbookFile, setWorkbookFile] = useState(null);
  const uploadMut = useMutation({
    mutationFn: (file) => reloadReferenceData(file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['health'] });
      queryClient.invalidateQueries({ queryKey: ['analytics'] });
      setWorkbookFile(null);
    },
  });

  return (
    <div className="card p-6 space-y-5">
      <div>
        <h2 className="text-sm font-semibold text-brand-900 dark:text-white uppercase tracking-wide">
          Reference data management
        </h2>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          Keep APG and CMS rate data current when NYS DOH or CMS publish updates.
        </p>
      </div>

      {/* CMS cache refresh */}
      <div className="border-t border-slate-200 dark:border-slate-700 pt-4">
        <h3 className="text-sm font-medium text-slate-700 dark:text-slate-300">
          CMS MPFS rate cache
        </h3>
        <div className="mt-2 p-3 rounded-md bg-warning-50 border border-warning text-xs text-warning-700">
          <strong>Heads up:</strong> the CMS MPFS integration is currently being
          updated. CMS migrated their Physician Fee Schedule API to a new
          subdomain (<code>pfs.data.cms.gov</code>) with a new schema, and our
          dataset reference has been retired. APG / Article 28 calculations
          are unaffected. CMS rates will return once the integration is
          rewritten against the new API.
        </div>
        <p className="text-xs text-slate-500 dark:text-slate-400 mt-3">
          When CMS integration is back: the app caches Medicare fee schedule
          rates for 24 hours. If CMS publishes a mid-year update and you want
          fresh rates immediately, flush the cache below. The next lookup will
          fetch live from the new CMS API.
        </p>
        <div className="mt-3 flex items-center gap-3">
          <button
            type="button"
            className="btn-secondary"
            onClick={() => cmsMut.mutate()}
            disabled={cmsMut.isPending}
          >
            <RefreshCw className="w-4 h-4" aria-hidden />
            {cmsMut.isPending ? 'Clearing…' : 'Refresh CMS cache'}
          </button>
          {cmsMut.isSuccess && (
            <span className="pill-success">
              <CheckCircle2 className="w-3 h-3" aria-hidden />
              Cleared {cmsMut.data?.cached_rates_cleared ?? 0} cached rate(s).
            </span>
          )}
          {cmsMut.isError && (
            <span className="pill-danger">
              <AlertCircle className="w-3 h-3" aria-hidden />
              {extractErrorMessage(cmsMut.error)}
            </span>
          )}
        </div>
      </div>

      {/* APG workbook upload (admin only) */}
      {isAdmin && (
        <div className="border-t border-slate-200 dark:border-slate-700 pt-4">
          <h3 className="text-sm font-medium text-slate-700 dark:text-slate-300">
            APG reference data (NYS DOH workbook)
          </h3>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
            When NYS DOH publishes updated APG weights, base rates, or
            HCPCS/ICD-10 crosswalks, download their updated Excel workbook and
            upload it here. This replaces the 5 reference tables (HCPCS→EAPG,
            ICD-10→EAPG, APG weights, base rates, provider county) while
            preserving all users, claims, and settings.
          </p>
          <div className="mt-3 flex items-center gap-3 flex-wrap">
            <label className="btn-secondary cursor-pointer">
              <UploadIcon className="w-4 h-4" aria-hidden />
              {workbookFile ? workbookFile.name : 'Choose workbook (.xlsx)'}
              <input
                type="file"
                accept=".xlsx,.xlsm"
                className="hidden"
                onChange={(e) => setWorkbookFile(e.target.files[0] || null)}
              />
            </label>
            {workbookFile && (
              <button
                type="button"
                className="btn-primary"
                onClick={() => uploadMut.mutate(workbookFile)}
                disabled={uploadMut.isPending}
              >
                {uploadMut.isPending ? 'Loading workbook…' : 'Upload & reload'}
              </button>
            )}
            {uploadMut.isSuccess && (
              <span className="pill-success">
                <CheckCircle2 className="w-3 h-3" aria-hidden />
                Reference data reloaded from {uploadMut.data?.filename}.
              </span>
            )}
            {uploadMut.isError && (
              <span className="pill-danger">
                <AlertCircle className="w-3 h-3" aria-hidden />
                {extractErrorMessage(uploadMut.error)}
              </span>
            )}
          </div>
          {uploadMut.isPending && (
            <p className="text-xs text-slate-500 mt-2">
              This takes 1–3 minutes for a full workbook (~90,000 rows).
              Please don't close the page.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Danger zone — clear all uploaded claims
// ---------------------------------------------------------------------------

function DangerZone({ queryClient }) {
  const mutation = useMutation({
    mutationFn: clearAllClaims,
    onSuccess: () => {
      // Invalidate everything that could show claim data
      queryClient.invalidateQueries({ queryKey: ['claims'] });
      queryClient.invalidateQueries({ queryKey: ['analytics'] });
    },
  });

  function handleClear() {
    const ok = window.confirm(
      'This will permanently delete ALL uploaded claims, service lines, ' +
      'adjustments, and APG results.\n\n' +
      'Users, settings, and reference data are preserved.\n\n' +
      'Are you sure you want to continue?'
    );
    if (!ok) return;
    mutation.mutate();
  }

  return (
    <div className="card p-6 border-l-4 border-l-danger">
      <h2 className="text-sm font-semibold text-danger-700 uppercase tracking-wide">
        Danger zone
      </h2>
      <p className="text-sm text-slate-600 mt-2">
        Delete every uploaded claim, service line, adjustment, and APG result
        from the database. Useful for clearing out test data. Users, team
        accounts, reference data (HCPCS / ICD-10 / APG weights / base rates),
        and the audit log are all preserved. This action is logged to the
        audit log.
      </p>

      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          className="btn-danger"
          onClick={handleClear}
          disabled={mutation.isPending}
        >
          <Trash2 className="w-4 h-4" aria-hidden />
          {mutation.isPending ? 'Clearing…' : 'Clear all uploaded claims'}
        </button>

        {mutation.isError && (
          <span className="pill-danger">
            <AlertCircle className="w-3 h-3" aria-hidden />{' '}
            {extractErrorMessage(mutation.error)}
          </span>
        )}
        {mutation.isSuccess && (
          <span className="pill-success">
            <CheckCircle2 className="w-3 h-3" aria-hidden /> Cleared{' '}
            {mutation.data?.claims_deleted ?? 0} claim(s).
          </span>
        )}
      </div>
    </div>
  );
}

function StatusLine({ mutation, existing }) {
  if (mutation.isError) {
    return (
      <span className="pill-danger">
        <AlertCircle className="w-3 h-3" aria-hidden /> {extractErrorMessage(mutation.error)}
      </span>
    );
  }
  if (mutation.isSuccess) {
    return (
      <span className="pill-success">
        <CheckCircle2 className="w-3 h-3" aria-hidden /> Saved. Active region: {mutation.data.region || '—'}
      </span>
    );
  }
  if (existing) {
    return (
      <span className="pill-slate">
        Current region: {existing.region || '—'}
      </span>
    );
  }
  return <span className="pill-slate">No provider configured yet.</span>;
}
