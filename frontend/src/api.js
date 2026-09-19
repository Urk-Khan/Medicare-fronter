// Tiny API client. The login token lives in sessionStorage-like local storage so a refresh keeps you signed in.

const BASE = import.meta.env.VITE_API_URL || '';
const TOKEN_KEY = 'voiceops_token';
const USER_KEY = 'voiceops_user';
const ROLE_KEY = 'voiceops_role';

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}

export function getUsername() {
  try { return localStorage.getItem(USER_KEY) || ''; } catch { return ''; }
}

export function getRole() {
  try { return localStorage.getItem(ROLE_KEY) || 'admin'; } catch { return 'admin'; }
}

export function isSuperAdmin() {
  return getRole() === 'super_admin';
}

export function setSession(token, username, role) {
  try {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, username);
    localStorage.setItem(ROLE_KEY, role || 'admin');
  } catch { /* storage unavailable */ }
}

export function clearSession() {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem(ROLE_KEY);
  } catch { /* storage unavailable */ }
}

const listeners = new Set();
export function onUnauthorized(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export async function api(path, { method = 'GET', body, form } = {}) {
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(BASE + path, { method, headers, body: payload });
  } catch {
    throw new Error("Can't reach the backend. Is it running?");
  }
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (res.status === 401 && !path.startsWith('/api/auth/login')) {
    clearSession();
    listeners.forEach((fn) => fn());
  }
  if (!res.ok) {
    let msg = data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg || JSON.stringify(d)).join('; ');
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return data;
}

export const qs = (params) => {
  const s = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') s.set(k, v);
  });
  const str = s.toString();
  return str ? `?${str}` : '';
};
