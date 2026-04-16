import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Vite config for the APG Analyzer frontend.
// - Dev server on :3000, reachable from Docker on 0.0.0.0
// - /api proxied to the FastAPI backend (default :8000) so we can use
//   relative URLs in the React code and dodge CORS during dev.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 3000,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.VITE_API_URL || 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: '0.0.0.0',
    port: 3000,
    strictPort: true,
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
