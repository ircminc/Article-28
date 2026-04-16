import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  Upload as UploadIcon,
  FileStack,
  FileText,
  Calculator as CalcIcon,
  Settings as SettingsIcon,
  Users as UsersIcon,
  Stethoscope,
} from 'lucide-react';
import { useAuth } from '../auth/AuthContext.jsx';

const NAV = [
  { to: '/dashboard',  label: 'Dashboard',       icon: LayoutDashboard },
  { to: '/upload',     label: 'Upload Files',    icon: UploadIcon },
  { to: '/calculator', label: 'Rate Calculator', icon: CalcIcon },
  { to: '/claims',     label: 'Claims',          icon: FileStack },
  { to: '/reports',    label: 'Reports',         icon: FileText },
  { to: '/settings',   label: 'Settings',        icon: SettingsIcon },
];

const ADMIN_NAV = [
  { to: '/users',     label: 'Users',        icon: UsersIcon },
];

export default function Sidebar() {
  const { user } = useAuth();
  const showAdmin = user?.role === 'admin';

  return (
    <aside className="w-60 bg-brand-700 text-white flex flex-col shrink-0">
      <div className="h-14 px-5 flex items-center gap-2.5 border-b border-brand-800">
        <img
          src="/pmtac-logo.png"
          alt="PMTAC Pvt Ltd"
          className="h-8 w-auto"
        />
        <div className="leading-tight">
          <div className="text-sm font-semibold tracking-tight">PMTAC Pvt Ltd</div>
          <div className="text-[11px] text-brand-200">APG Analyzer</div>
        </div>
      </div>
      <nav className="flex-1 px-2 py-4 space-y-1">
        {NAV.map(({ to, label, icon: Icon }) => (
          <NavItem key={to} to={to} label={label} Icon={Icon} />
        ))}

        {showAdmin && (
          <>
            <div className="px-3 pt-4 pb-1 text-[10px] font-semibold uppercase tracking-wider text-brand-200">
              Admin
            </div>
            {ADMIN_NAV.map(({ to, label, icon: Icon }) => (
              <NavItem key={to} to={to} label={label} Icon={Icon} />
            ))}
          </>
        )}
      </nav>
      <div className="px-4 py-3 border-t border-brand-800 text-[11px] text-brand-200">
        <div>Phase 5 · auth</div>
        <div>v0.5.0</div>
      </div>
    </aside>
  );
}

function NavItem({ to, label, Icon }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        [
          'flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors',
          isActive
            ? 'bg-white/10 text-white font-medium'
            : 'text-brand-100 hover:bg-white/5 hover:text-white',
        ].join(' ')
      }
    >
      <Icon className="w-4 h-4 shrink-0" aria-hidden />
      <span>{label}</span>
    </NavLink>
  );
}
