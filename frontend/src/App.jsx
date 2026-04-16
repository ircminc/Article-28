import { Navigate, Route, Routes } from 'react-router-dom';
import Layout from './components/Layout.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Upload from './pages/Upload.jsx';
import Calculator from './pages/Calculator.jsx';
import Claims from './pages/Claims.jsx';
import ClaimDetail from './pages/ClaimDetail.jsx';
import Reports from './pages/Reports.jsx';
import Settings from './pages/Settings.jsx';
import Login from './pages/Login.jsx';
import Users from './pages/Users.jsx';
import RequireAuth from './auth/RequireAuth.jsx';
import { AuthProvider } from './auth/AuthContext.jsx';

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        {/* Public route: the login screen */}
        <Route path="/login" element={<Login />} />

        {/* All other routes require authentication. */}
        <Route
          path="/"
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="upload" element={<Upload />} />
          <Route path="calculator" element={<Calculator />} />
          <Route path="claims" element={<Claims />} />
          <Route path="claims/:id" element={<ClaimDetail />} />
          <Route path="reports" element={<Reports />} />
          <Route path="settings" element={<Settings />} />
          <Route
            path="users"
            element={
              <RequireAuth role="admin">
                <Users />
              </RequireAuth>
            }
          />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </AuthProvider>
  );
}

function NotFound() {
  return (
    <div className="card p-8 text-center">
      <h2 className="text-lg font-semibold text-brand-900">Page not found</h2>
      <p className="text-slate-500 mt-2">Check the URL or return to the dashboard.</p>
    </div>
  );
}
