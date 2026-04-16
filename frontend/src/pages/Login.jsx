import { useState } from 'react';
import { useNavigate, useLocation, Navigate } from 'react-router-dom';
import { LogIn, AlertCircle, Stethoscope } from 'lucide-react';
import { useAuth } from '../auth/AuthContext.jsx';
import { extractErrorMessage } from '../services/api.js';

// Standalone login screen — no sidebar, full-page centered card.
// Post-login, bounce back to the page the user was trying to reach.
export default function Login() {
  const { login, isAuthenticated, bootstrapping } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const intended = location.state?.from || '/dashboard';

  // Already signed in? Skip the form.
  if (bootstrapping) {
    return (
      <div className="min-h-screen grid place-items-center bg-page text-slate-500 text-sm">
        Loading…
      </div>
    );
  }
  if (isAuthenticated) {
    return <Navigate to={intended} replace />;
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(username.trim(), password);
      navigate(intended, { replace: true });
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen grid place-items-center bg-gradient-to-br from-brand-700 to-brand-900 px-4">
      <div className="w-full max-w-md card p-8 space-y-5 shadow-xl">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-md bg-brand-700 grid place-items-center">
            <Stethoscope className="w-5 h-5 text-white" aria-hidden />
          </div>
          <div>
            <div className="text-sm font-semibold text-brand-900">PMTAC Pvt Ltd</div>
            <div className="text-xs text-slate-500">APG 835/837 Rate Analyzer</div>
          </div>
        </div>

        <div>
          <h1 className="text-xl font-semibold text-brand-900">Sign in</h1>
          <p className="text-sm text-slate-500 mt-1">
            Use the credentials your administrator provided.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="label" htmlFor="username">Username</label>
            <input
              id="username"
              name="username"
              autoComplete="username"
              className="input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              required
            />
          </div>
          <div>
            <label className="label" htmlFor="password">Password</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              className="input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>

          {error && (
            <div className="pill-danger w-full justify-start">
              <AlertCircle className="w-3.5 h-3.5" aria-hidden /> {error}
            </div>
          )}

          <button type="submit" className="btn-primary w-full justify-center" disabled={busy}>
            <LogIn className="w-4 h-4" aria-hidden />
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <div className="text-xs text-slate-400 border-t border-slate-200 pt-4">
          This application handles protected health information. Unauthorized
          access is prohibited and audited.
        </div>
      </div>
    </div>
  );
}
