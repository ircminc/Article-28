import { Menu } from '@headlessui/react';
import { useNavigate } from 'react-router-dom';
import { ChevronDown, LogOut, User as UserIcon } from 'lucide-react';
import { useAuth } from '../auth/AuthContext.jsx';

// Small dropdown in the header: shows the signed-in user's name + role + a
// logout action. Uses @headlessui/react for accessible menu behavior.
export default function UserMenu() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  if (!user) return null;

  async function handleLogout() {
    await logout();
    navigate('/login', { replace: true });
  }

  const displayName = user.full_name || user.username;

  return (
    <Menu as="div" className="relative">
      <Menu.Button className="flex items-center gap-2 px-2 py-1 rounded hover:bg-slate-100 text-sm">
        <div className="w-7 h-7 rounded-full bg-brand-700 text-white grid place-items-center text-xs font-semibold">
          {displayName.slice(0, 2).toUpperCase()}
        </div>
        <div className="text-left leading-tight hidden sm:block">
          <div className="text-slate-800">{displayName}</div>
          <div className="text-[10px] text-slate-500 uppercase tracking-wide">{user.role}</div>
        </div>
        <ChevronDown className="w-4 h-4 text-slate-400" aria-hidden />
      </Menu.Button>
      <Menu.Items className="absolute right-0 mt-2 w-56 origin-top-right rounded-md bg-white shadow-lg ring-1 ring-slate-200 focus:outline-none z-10">
        <div className="px-4 py-3 border-b border-slate-100">
          <div className="text-sm font-semibold text-brand-900">{displayName}</div>
          <div className="text-xs text-slate-500">@{user.username}</div>
          <div className="mt-1 text-[10px] uppercase tracking-wide text-slate-400">{user.role}</div>
        </div>
        <div className="py-1">
          <Menu.Item>
            {({ active }) => (
              <button
                type="button"
                onClick={handleLogout}
                className={[
                  'w-full flex items-center gap-2 px-4 py-2 text-sm text-left',
                  active ? 'bg-slate-50 text-brand-900' : 'text-slate-700',
                ].join(' ')}
              >
                <LogOut className="w-4 h-4" aria-hidden /> Sign out
              </button>
            )}
          </Menu.Item>
        </div>
      </Menu.Items>
    </Menu>
  );
}
