/// <reference types="vitest" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': '/src' } },
  server: {
    port: 5173,
    proxy: {
      // Inside the docker compose web container `localhost` is the container
      // itself, not the host. BFF lives in a sibling container named `bff`
      // on the same `foundation` network. Default target uses the in-cluster
      // service name; override with VITE_BFF_PROXY=http://localhost:8005 for
      // running `npm run dev` directly on the host.
      '/api': {
        target: process.env.VITE_BFF_PROXY ?? 'http://bff:8005',
        changeOrigin: true,
      },
    },
  },
  build: {
    // Don't ship source maps to production browsers — they'd let anyone
    // download the un-minified React + business logic.
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/main.tsx', 'src/test/**', '**/*.d.ts'],
    },
  },
});
