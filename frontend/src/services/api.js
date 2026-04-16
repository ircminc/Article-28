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
