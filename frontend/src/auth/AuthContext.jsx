import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { apiClient } from '../services/api.js';

// AuthContext holds the auth state and token management for the whole app.
// Design notes:
//   * Token persists in localStorage under 'apg:token'. On cold boot we try
//     /api/auth/me to confirm it's still valid; on 401 we discard and show
//     the login screen.
//   * Token is installed on the shared axios client's default headers so
//     every subsequent request carries it. No per-call plumbing needed.
//   * A 401 interceptor (installed once below) clears the token and redirects
//     to /login — this handles server-side expiration + disabled-user cases.

const AuthContext = createContext(null);
const STORAGE_KEY = 'apg:token';

let _interceptorInstalled = false;

export function AuthProvider({ children }) {
  const [token, setTokenState] = useState(() => localStorage.getItem(STORAGE_KEY));
  const [user, setUser] = useState(null);
  const [bootstrapping, setBootstrapping] = useState(!!token);

  // Keep the axios default + localStorage in sync with token changes.
  useEffect(() => {
    if (token) {
      apiClient.defaults.headers.common.Authorization = `Bearer ${token}`;
      localStorage.setItem(STORAGE_KEY, token);
    } else {
      delete apiClient.defaults.headers.common.Authorization;
      localStorage.removeItem(STORAGE_KEY);
    }
  }, [token]);

  // Install the 401 interceptor exactly once per app load. Shared across
  // AuthProvider remounts (React StrictMode mounts twice in dev).
  useEffect(() => {
    if (_interceptorInstalled) return;
    _interceptorInstalled = true;
    apiClient.interceptors.response.use(
      (resp) => resp,
      (error) => {
        const status = error?.response?.status;
        // 401 = token invalid/missing/expired. 403 = authenticated but lacks
        // permission — keep the session, just let the component handle it.
        if (status === 401) {
          localStorage.removeItem(STORAGE_KEY);
          delete apiClient.defaults.headers.common.Authorization;
          // Only redirect if we're not already on /login to avoid loops
          if (window.location.pathname !== '/login') {
            window.location.href = '/login';
          }
        }
        return Promise.reject(error);
      },
    );
  }, []);

  // On cold boot with a persisted token, validate it by calling /me.
  useEffect(() => {
    let cancelled = false;
    async function bootstrap() {
      if (!token) {
        setBootstrapping(false);
        return;
      }
      try {
        const { data } = await apiClient.get('/api/auth/me');
        if (!cancelled) setUser(data);
      } catch {
        if (!cancelled) {
          setTokenState(null);
          setUser(null);
        }
      } finally {
        if (!cancelled) setBootstrapping(false);
      }
    }
    bootstrap();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function login(username, password) {
    const { data } = await apiClient.post('/api/auth/login', { username, password });
    setTokenState(data.access_token);
    setUser(data.user);
    return data.user;
  }

  async function logout() {
    try {
      await apiClient.post('/api/auth/logout');
    } catch {
      // Even if the API call fails (expired token, network), clear local state.
    }
    setTokenState(null);
    setUser(null);
  }

  const value = useMemo(
    () => ({ token, user, bootstrapping, login, logout, isAuthenticated: !!user }),
    [token, user, bootstrapping],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
