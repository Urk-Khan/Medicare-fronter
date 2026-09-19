import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// In development the dashboard runs on :5173 and forwards API calls to the backend on :8000.
// In production the backend serves the built dashboard itself (same address), so no URL is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  build: { outDir: 'dist', sourcemap: false },
});
