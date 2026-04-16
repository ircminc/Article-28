import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { UserPlus, UserX, KeyRound, AlertCircle, CheckCircle2 } from 'lucide-react';
import { apiClient, extractErrorMessage } from '../services/api.js';
import { fmtDate } from '../utils/format.js';
import { useAuth } from '../auth/AuthContext.jsx';

// Admin-only user management. Supports:
//   - Listing users (active + disabled)
//   - Creating a new user with role
//   - Disabling / re-enabling a user
//   - Resetting a user's password
export default function Users() {
  const qc = useQueryClient();
  const { user: me } = useAuth();
  const { data: users = [], isLoading } = useQuery({
    queryKey: ['users'],
    queryFn: async () => (await apiClient.get('/api/admin/users')).data,
  });

  const [showCreate, setShowCreate] = useState(false);
  const [resetTarget, setResetTarget] = useState(null);

  const createMut = useMutation({
    mutationFn: async (payload) => (await apiClient.post('/api/admin/users', payload)).data,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['users'] });
      setShowCreate(false);
    },
  });

  const toggleDisabledMut = useMutation({
    mutationFn: async ({ id, disabled }) =>
      (await apiClient.patch(`/api/admin/users/${id}`, { disabled })).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['users'] }),
  });

  const resetMut = useMutation({
    mutationFn: async ({ id, password }) =>
      (await apiClient.post(`/api/admin/users/${id}/password`, { new_password: password })).data,
    onSuccess: () => setResetTarget(null),
  });

  return (
    <div className="max-w-5xl space-y-5">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-brand-900">Users</h1>
          <p className="text-sm text-slate-500 mt-1">
            Admin-only. Create analysts and viewers, disable departed team members,
            reset passwords.
          </p>
        </div>
        <button className="btn-primary" onClick={() => setShowCreate((s) => !s)}>
          <UserPlus className="w-4 h-4" aria-hidden /> New user
        </button>
      </header>

      {showCreate && (
        <CreateUserCard
          onSubmit={(payload) => createMut.mutate(payload)}
          onCancel={() => setShowCreate(false)}
          mutation={createMut}
        />
      )}

      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
            <tr>
              <th className="text-left px-4 py-2 font-semibold">Username</th>
              <th className="text-left px-4 py-2 font-semibold">Full name</th>
              <th className="text-left px-4 py-2 font-semibold">Role</th>
              <th className="text-left px-4 py-2 font-semibold">Status</th>
              <th className="text-left px-4 py-2 font-semibold">Last login</th>
              <th className="text-right px-4 py-2 font-semibold">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {isLoading && (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-slate-400">Loading…</td></tr>
            )}
            {!isLoading && users.length === 0 && (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-slate-400">No users.</td></tr>
            )}
            {users.map((u) => (
              <tr key={u.id} className="hover:bg-slate-50">
                <td className="px-4 py-2 font-mono">{u.username}{u.id === me?.id && (
                  <span className="ml-2 pill-brand">you</span>
                )}</td>
                <td className="px-4 py-2 text-slate-600">{u.full_name || '—'}</td>
                <td className="px-4 py-2">
                  <RolePill role={u.role} />
                </td>
                <td className="px-4 py-2">
                  {u.disabled ? (
                    <span className="pill-danger">Disabled</span>
                  ) : (
                    <span className="pill-success">Active</span>
                  )}
                </td>
                <td className="px-4 py-2 text-slate-500 text-xs">
                  {u.last_login_at ? fmtDate(u.last_login_at) : '—'}
                </td>
                <td className="px-4 py-2 text-right whitespace-nowrap">
                  <button
                    className="text-slate-500 hover:text-primary mr-3"
                    title="Reset password"
                    onClick={() => setResetTarget(u)}
                  >
                    <KeyRound className="w-4 h-4 inline" aria-hidden />
                  </button>
                  <button
                    className="text-slate-500 hover:text-danger"
                    title={u.disabled ? 'Enable' : 'Disable'}
                    onClick={() => toggleDisabledMut.mutate({ id: u.id, disabled: !u.disabled })}
                    disabled={u.id === me?.id && !u.disabled}
                  >
                    <UserX className="w-4 h-4 inline" aria-hidden />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {toggleDisabledMut.isError && (
        <div className="pill-danger">
          <AlertCircle className="w-3 h-3" aria-hidden /> {extractErrorMessage(toggleDisabledMut.error)}
        </div>
      )}

      {resetTarget && (
        <ResetPasswordCard
          user={resetTarget}
          onCancel={() => setResetTarget(null)}
          onSubmit={(password) => resetMut.mutate({ id: resetTarget.id, password })}
          mutation={resetMut}
        />
      )}
    </div>
  );
}

function RolePill({ role }) {
  const cls = {
    admin:   'pill-danger',
    analyst: 'pill-brand',
    viewer:  'pill-slate',
  }[role] || 'pill-slate';
  return <span className={cls}>{role}</span>;
}

function CreateUserCard({ onSubmit, onCancel, mutation }) {
  const [username, setUsername] = useState('');
  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [role, setRole] = useState('analyst');
  const [password, setPassword] = useState('');

  function submit(e) {
    e.preventDefault();
    onSubmit({
      username: username.trim(),
      full_name: fullName.trim() || null,
      email: email.trim() || null,
      role,
      password,
    });
  }

  return (
    <form onSubmit={submit} className="card p-5 space-y-4 border-l-4 border-l-primary">
      <h2 className="text-sm font-semibold text-brand-900">New user</h2>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label className="label" htmlFor="nu">Username *</label>
          <input id="nu" required className="input" value={username}
            onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="fn">Full name</label>
          <input id="fn" className="input" value={fullName}
            onChange={(e) => setFullName(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="em">Email</label>
          <input id="em" type="email" className="input" value={email}
            onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="rl">Role *</label>
          <select id="rl" className="select" value={role}
            onChange={(e) => setRole(e.target.value)}>
            <option value="admin">admin</option>
            <option value="analyst">analyst</option>
            <option value="viewer">viewer</option>
          </select>
        </div>
        <div className="md:col-span-2">
          <label className="label" htmlFor="pw">Initial password * (min 10 chars)</label>
          <input id="pw" type="password" minLength={10} required className="input" value={password}
            onChange={(e) => setPassword(e.target.value)} />
          <p className="text-xs text-slate-500 mt-1">
            Share the password securely. The user should change it on first login.
          </p>
        </div>
      </div>
      {mutation.isError && (
        <div className="pill-danger">
          <AlertCircle className="w-3 h-3" aria-hidden /> {extractErrorMessage(mutation.error)}
        </div>
      )}
      <div className="flex justify-end gap-2">
        <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn-primary" disabled={mutation.isPending}>
          {mutation.isPending ? 'Creating…' : 'Create user'}
        </button>
      </div>
    </form>
  );
}

function ResetPasswordCard({ user, onCancel, onSubmit, mutation }) {
  const [password, setPassword] = useState('');
  function submit(e) { e.preventDefault(); onSubmit(password); }
  return (
    <form onSubmit={submit} className="card p-5 space-y-4 border-l-4 border-l-warning">
      <h2 className="text-sm font-semibold text-brand-900">
        Reset password for <span className="font-mono">{user.username}</span>
      </h2>
      <div>
        <label className="label" htmlFor="np">New password * (min 10 chars)</label>
        <input id="np" type="password" minLength={10} required className="input" value={password}
          onChange={(e) => setPassword(e.target.value)} autoFocus />
      </div>
      {mutation.isError && (
        <div className="pill-danger">
          <AlertCircle className="w-3 h-3" aria-hidden /> {extractErrorMessage(mutation.error)}
        </div>
      )}
      {mutation.isSuccess && (
        <div className="pill-success">
          <CheckCircle2 className="w-3 h-3" aria-hidden /> Password reset.
        </div>
      )}
      <div className="flex justify-end gap-2">
        <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn-primary" disabled={mutation.isPending}>
          {mutation.isPending ? 'Resetting…' : 'Reset password'}
        </button>
      </div>
    </form>
  );
}
