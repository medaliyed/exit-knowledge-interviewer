import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss()],
  base: command === 'build' ? '/assets/workspace/' : '/',
  build: { outDir: '../src/eki/web/static/workspace', emptyOutDir: true },
  server: {
    port: 5173,
    strictPort: true,
    proxy: Object.fromEntries(
      ['/auth', '/runs', '/plans', '/sessions', '/entries', '/runbook', '/healthz', '/follow-up-requests', '/follow-up-proposals'].map(path => [
        path, { target: 'http://127.0.0.1:8000', changeOrigin: false },
      ]),
    ),
  },
  test: { include: ['src/**/*.test.ts', 'src/**/*.test.tsx'], environment: 'node' },
}));
