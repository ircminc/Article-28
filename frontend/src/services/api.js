// Thin axios client + typed helpers for every backend endpoint we touch.
// URLs are RELATIVE ('/api/...'); Vite's dev proxy routes them to the FastAPI
// backend (see vite.config.js). In production we expect the frontend and
// backend to be reverse-proxied behind the same host, or VITE_API_URL to be
// set at build time.
import axios from 'axios';

const baseURL = import.meta.env.VITE_API_URL || '';
export const apiClient = axios.create({
  baseURL,
  timeout: 60_000,
  headers: { 'Content-Type': 'application/json' },
});

// Extract the most useful error message the backend returned, or fall back
// to axios defaults. FastAPI normalizes error responses to { detail: "..." }.
export function extractErrorMessage(error) {
  if (!error) return 'Unknown error';
  const data = error?.response?.data;
  if (typeof data?.detail === 'string') return data.detail;
  if (Array.isArray(data?.detail)) {
    return data.detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
  }
  return error.message || String(error);
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------
export async function getHealth() {
  const { data } = await apiClient.get('/api/health');
  return data;
}

// ---------------------------------------------------------------------------
// Provider config
// ---------------------------------------------------------------------------
export async function getProvider() {
  const { data } = await apiClient.get('/api/config/provider');
  return data;
}

export async function upsertProvider(payload) {
  const { data } = await apiClient.post('/api/config/provider', payload);
  return data;
}

// ---------------------------------------------------------------------------
// Claims
// ---------------------------------------------------------------------------
export async function listClaims({ fileType = null, limit = 50, offset = 0 } = {}) {
  const params = { limit, offset };
  if (fileType) params.file_type = fileType;
  const { data } = await apiClient.get('/api/claims', { params });
  return data;
}

export async function getClaim(id) {
  const { data } = await apiClient.get(`/api/claims/${id}`);
  return data;
}

export async function getClaimApg(id) {
  const { data } = await apiClient.get(`/api/claims/${id}/apg`);
  return data;
}

// Wipes every parsed claim + APG result. Admin or analyst only server-side.
// Returns { claims_deleted: N } on success.
export async function clearAllClaims() {
  const { data } = await apiClient.delete('/api/claims');
  return data;
}

// Manual Rate Calculator — POST payload shape:
//   {
//     date_of_service: 'YYYY-MM-DD',
//     service_lines: [{ procedure_code, modifiers: [], units, billed_amount }, ...],
//     principal_diagnosis: 'E119',
//     other_diagnoses: ['I10', ...],
//     target: 'apg' | 'cms' | 'both',
//     cms_locality: '01',        // optional; falls back to provider config
//     cms_use_facility_rate: false,
//   }
export async function calculateRate(payload) {
  const { data } = await apiClient.post('/api/calculator/calculate', payload);
  return data;
}

// Admin: clear the CMS MPFS rate cache (forces live re-fetch on next lookup)
export async function clearCmsCache() {
  const { data } = await apiClient.delete('/api/admin/cms-cache');
  return data;
}

// List CMS Medicare localities for a year — used to populate dropdowns
// instead of forcing users to type 7-digit MAC-locality codes.
export async function listCmsLocalities(year) {
  const { data } = await apiClient.get('/api/reference/cms-localities', {
    params: { year },
  });
  return data;
}

// Admin: upload a new NYS DOH workbook to reload APG reference data
export async function reloadReferenceData(file) {
  const form = new FormData();
  form.append('file', file);
  const { data } = await apiClient.post('/api/admin/reload-reference-data', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300_000,  // workbook parsing can take a few minutes
  });
  return data;
}

// Admin: upload NYS DOH's DTC base-rates file (the periodic .xls inventory).
// Replaces only the DTC rows in apg_base_rates — leaves hospital rates and
// everything else untouched.
export async function reloadDtcBaseRates(file) {
  const form = new FormData();
  form.append('file', file);
  const { data } = await apiClient.post('/api/admin/reload-dtc-rates', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 60_000,   // much smaller file than the full workbook
  });
  return data;
}

// Admin: nuke every APG/CMS reference table so fresh uploads start clean.
// Preserves users, audit log, providers, claims, county/locality tables.
// Returns { ok, rows_deleted_total, by_table, preserved }.
export async function masterResetReferenceData() {
  const { data } = await apiClient.delete('/api/admin/master-reset-reference-data', {
    timeout: 60_000,
  });
  return data;
}

// Admin: upload PMTAC's "Updated APG Fee Calculator" workbook. Replaces
// every source='dtc' row in apg_base_rates from the 'Updated APG Base Rate'
// sheet (all freestanding peer groups × Upstate/Downstate × historical
// effective dates through 2022-04-01). Leaves hospital rates, crosswalks,
// weights, claims, and users untouched.
export async function reloadApgBaseRatesV2(file) {
  const form = new FormData();
  form.append('file', file);
  const { data } = await apiClient.post('/api/admin/reload-apg-base-rates-v2', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120_000,
  });
  return data;
}

// Admin: upload eMedNY's APG Crosswalk .xlsx. Replaces HCPCS->EAPG and
// ICD-10->EAPG tables. Leaves weights, base rates, claims, users untouched.
export async function reloadCrosswalk(file) {
  const form = new FormData();
  form.append('file', file);
  const { data } = await apiClient.post('/api/admin/reload-crosswalk', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300_000,
  });
  return data;
}

// Admin: upload NYS DOH's history_and_fee_schedule.xls. Replaces APG
// weight history, Px-based weight overrides, and the flat-fee schedule.
export async function reloadWeightsHistory(file) {
  const form = new FormData();
  form.append('file', file);
  const { data } = await apiClient.post('/api/admin/reload-weights-history', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300_000,
  });
  return data;
}

// ---------------------------------------------------------------------------
// Uploads
// ---------------------------------------------------------------------------
function _buildFormData(files) {
  const form = new FormData();
  for (const f of files) form.append('files', f);
  return form;
}

export async function upload835I(files) {
  const { data } = await apiClient.post('/api/upload/835i', _buildFormData(files), {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function upload835P(files) {
  const { data } = await apiClient.post('/api/upload/835p', _buildFormData(files), {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function upload837(files) {
  const { data } = await apiClient.post('/api/upload/837', _buildFormData(files), {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

// ---------------------------------------------------------------------------
// Reference lookups
// ---------------------------------------------------------------------------
export async function lookupHcpcs(code, dos) {
  const { data } = await apiClient.get(`/api/reference/hcpcs/${encodeURIComponent(code)}`, {
    params: { dos },
  });
  return data;
}

export async function lookupIcd10(code, dos) {
  const { data } = await apiClient.get(`/api/reference/icd10/${encodeURIComponent(code)}`, {
    params: { dos },
  });
  return data;
}

export async function lookupApgWeight(apg, dos) {
  const { data } = await apiClient.get(`/api/reference/apg/${apg}`, { params: { dos } });
  return data;
}

export async function listBaseRates(filters = {}) {
  const { data } = await apiClient.get('/api/reference/base-rates', { params: filters });
  return data;
}

export async function lookupCmsRate({ code, dos, modifier = '', locality, forceRefresh = false }) {
  const params = { dos, modifier };
  if (locality) params.locality = locality;
  if (forceRefresh) params.force_refresh = true;
  const { data } = await apiClient.get(`/api/reference/cms/${encodeURIComponent(code)}`, { params });
  return data;
}

// ---------------------------------------------------------------------------
// Analytics (Phase 4)
// ---------------------------------------------------------------------------

// Shared filter shape: { dateFrom, dateTo, payerName, fileType, providerNpi }.
// Converted to the snake_case query params the backend expects.
function _toAnalyticsParams(f = {}) {
  const out = {};
  if (f.dateFrom) out.date_from = f.dateFrom;
  if (f.dateTo) out.date_to = f.dateTo;
  if (f.payerName) out.payer_name = f.payerName;
  if (f.fileType) out.file_type = f.fileType;
  if (f.providerNpi) out.provider_npi = f.providerNpi;
  return out;
}

export async function getAnalyticsSummary(filters = {}) {
  const { data } = await apiClient.get('/api/analytics/summary', {
    params: _toAnalyticsParams(filters),
  });
  return data;
}

export async function getAnalyticsCompression({ groupBy = 'eapg', limit = 20, ...filters } = {}) {
  const params = { group_by: groupBy, limit, ..._toAnalyticsParams(filters) };
  const { data } = await apiClient.get('/api/analytics/compression', { params });
  return data;
}

export async function getAnalyticsDenials({ limit = 20, ...filters } = {}) {
  const params = { limit, ..._toAnalyticsParams(filters) };
  const { data } = await apiClient.get('/api/analytics/denials', { params });
  return data;
}

export async function getAnalyticsTrends({ period = 'monthly', ...filters } = {}) {
  const params = { period, ..._toAnalyticsParams(filters) };
  const { data } = await apiClient.get('/api/analytics/trends', { params });
  return data;
}

export async function getPayerScorecard(filters = {}) {
  const { data } = await apiClient.get('/api/analytics/payer-scorecard', {
    params: _toAnalyticsParams(filters),
  });
  return data;
}

// ---------------------------------------------------------------------------
// Export (Phase 4)
// ---------------------------------------------------------------------------
//
// Both export endpoints return the binary file in the response body. We
// request `responseType: 'blob'` and leave saving to the caller — usually
// by creating an object URL and triggering an <a download> click.

function _toExportBody(opts = {}) {
  const out = { include_835i: true, include_835p: true, pdf_max_claims: 25 };
  if (opts.dateFrom) out.date_from = opts.dateFrom;
  if (opts.dateTo) out.date_to = opts.dateTo;
  if (opts.payerName) out.payer_name = opts.payerName;
  if (opts.include835I !== undefined) out.include_835i = opts.include835I;
  if (opts.include835P !== undefined) out.include_835p = opts.include835P;
  if (opts.pdfMaxClaims) out.pdf_max_claims = opts.pdfMaxClaims;
  return out;
}

export async function downloadExcelReport(opts = {}) {
  const resp = await apiClient.post('/api/export/excel', _toExportBody(opts), {
    responseType: 'blob',
  });
  return { blob: resp.data, filename: _extractFilename(resp, 'apg_report.xlsx') };
}

export async function downloadPdfReport(opts = {}) {
  const resp = await apiClient.post('/api/export/pdf', _toExportBody(opts), {
    responseType: 'blob',
  });
  return { blob: resp.data, filename: _extractFilename(resp, 'apg_report.pdf') };
}

function _extractFilename(resp, fallback) {
  const dispo = resp.headers['content-disposition'] || resp.headers['Content-Disposition'];
  if (!dispo) return fallback;
  const m = /filename="([^"]+)"/.exec(dispo);
  return m ? m[1] : fallback;
}

// Utility: trigger a browser download for a Blob + filename.
export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke on next tick so browsers that are slow to read still get the blob
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
