import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar.jsx';
import HealthBadge from './HealthBadge.jsx';
import ThemeToggle from './ThemeToggle.jsx';
import UserMenu from './UserMenu.jsx';

// Two-column app shell: sidebar nav on the left, page content on the right.
// The header strip inside the content column carries a health badge,
// theme toggle, and signed-in user menu.
export default function Layout() {
  return (
    <div className="flex min-h-screen bg-page dark:bg-slate-900">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-14 bg-white dark:bg-slate-800 border-b border-slate-200 dark:border-slate-700 flex items-center justify-between px-6 shrink-0">
          <div className="text-sm text-slate-500 dark:text-slate-400">
            APG 835/837 Rate Analyzer · <span className="text-brand-700 dark:text-blue-400">Article 28 Compliance</span>
          </div>
          <div className="flex items-center gap-3">
            <HealthBadge />
            <ThemeToggle />
            <UserMenu />
          </div>
        </header>
        <main className="flex-1 p-6 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
