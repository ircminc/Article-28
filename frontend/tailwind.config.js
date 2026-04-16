/** @type {import('tailwindcss').Config} */
// IRC Minc APG Analyzer palette + Inter font.
// Semantic color tokens map to the standard statuses used across the UI:
//   brand   = navy for nav/headers       (#1a2e4a)
//   primary = medical blue for CTAs      (#2563eb)
//   success = paid / compliant           (#16a34a)
//   warning = compression / review       (#d97706)
//   danger  = denials / underpayments    (#dc2626)
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: '#1a2e4a',
          50: '#eef2f7',
          100: '#dbe2ec',
          200: '#b4c2d3',
          300: '#8ca2b9',
          400: '#6482a0',
          500: '#3d628a',
          600: '#1a2e4a',
          700: '#162640',
          800: '#111e33',
          900: '#0a1324',
        },
        primary: {
          DEFAULT: '#2563eb',
          50: '#eff6ff',
          100: '#dbeafe',
          200: '#bfdbfe',
          300: '#93c5fd',
          400: '#60a5fa',
          500: '#3b82f6',
          600: '#2563eb',
          700: '#1d4ed8',
          800: '#1e40af',
          900: '#1e3a8a',
        },
        success: { DEFAULT: '#16a34a', 50: '#f0fdf4', 100: '#dcfce7', 600: '#16a34a', 700: '#15803d' },
        warning: { DEFAULT: '#d97706', 50: '#fffbeb', 100: '#fef3c7', 600: '#d97706', 700: '#b45309' },
        danger:  { DEFAULT: '#dc2626', 50: '#fef2f2', 100: '#fee2e2', 600: '#dc2626', 700: '#b91c1c' },
        surface: '#ffffff',
        page:    '#f8fafc',
      },
      fontFamily: {
        sans: [
          'Inter',
          'system-ui',
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'Arial',
          'sans-serif',
        ],
      },
      boxShadow: {
        card: '0 1px 2px 0 rgba(15, 23, 42, 0.06), 0 1px 3px 0 rgba(15, 23, 42, 0.08)',
      },
    },
  },
  plugins: [],
};
