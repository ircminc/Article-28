import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from './AuthContext.jsx';

// Wraps a route subtree. Redirects to /login when unauthenticated, preserving
// the attempted path so we can bounce back after login. When `role` is set,
// it also enforces that the user has that role (or is an admin, which is
// allowed anywhere).
export default function RequireAuth({ children, role = null }) {
  const { isAuthenticated, bootstrapping, user } = useAuth();
  const location = useLocation();

  if (bootstrapping) {
    return (
      <div className="p-8 text-slate-500 text-sm">
        Checking authentication…
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  if (role && user.role !== role && user.role !== 'admin') {
    return (
      <div className="card p-8 max-w-xl">
        <h2 className="text-lg font-semibold text-brand-900">Not authorized</h2>
        <p className="text-sm text-slate-500 mt-2">
          You need the <code className="px-1 bg-slate-100 rounded">{role}</code> role to
          view this page. You're currently signed in as{' '}
          <code className="px-1 bg-slate-100 rounded">{user.role}</code>.
        </p>
      </div>
    );
  }

  return children;
}
