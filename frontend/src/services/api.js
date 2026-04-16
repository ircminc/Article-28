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
