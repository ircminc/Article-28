import { useEffect, useState } from 'react';
import { Sun, Moon } from 'lucide-react';

// Toggle between light and dark themes. Persists to localStorage and applies
// the `dark` class on <html> (Tailwind's `darkMode: 'class'` strategy).
// On first load, respects the user's OS-level preference via prefers-color-scheme.
const STORAGE_KEY = 'apg:theme';

function getInitialTheme() {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'dark' || saved === 'light') return saved;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export default function ThemeToggle() {
  const [theme, setTheme] = useState(getInitialTheme);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'dark') {
      root.classList.add('dark');
    } else {
      root.classList.remove('dark');
    }
    localStorage.setItem(STORAGE_KEY, theme);
  }, [theme]);

  function toggle() {
    setTheme((prev) => (prev === 'dark' ? 'light' : 'dark'));
  }

  return (
    <button
      type="button"
      onClick={toggle}
      className="p-1.5 rounded-md text-slate-500 hover:text-brand-900 hover:bg-slate-100
                 dark:text-slate-400 dark:hover:text-white dark:hover:bg-slate-700
                 transition-colors"
      aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
      title={theme === 'dark' ? 'Light mode' : 'Dark mode'}
    >
      {theme === 'dark' ? (
        <Sun className="w-4 h-4" aria-hidden />
      ) : (
        <Moon className="w-4 h-4" aria-hidden />
      )}
    </button>
  );
}
