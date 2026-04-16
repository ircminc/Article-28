import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar.jsx';
import HealthBadge from './HealthBadge.jsx';

// Two-column app shell: sidebar nav on the left, page content on the right.
// The header strip inside the content column carries a health badge that
// surfaces backend connectivity + reference-data load status at a glance.
export default function Layout() {
  return (
    <div className="flex min-h-screen bg-page">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-14 bg-white border-b border-slate-200 flex items-center justify-between px-6 shrink-0">
          <div className="text-sm text-slate-500">
            APG 835/837 Rate Analyzer · <span className="text-brand-700">Article 28 Compliance</span>
          </div>
          <HealthBadge />
        </header>
        <main className="flex-1 p-6 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
