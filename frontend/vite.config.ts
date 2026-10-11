import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://backend:8000', '/health': 'http://backend:8000' } },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('@mui/') || id.includes('@emotion/')) return 'ui-vendor';
          if (id.includes('react-hook-form') || id.includes('@hookform/') || id.includes('/zod/')) {
            return 'forms-vendor';
          }
          if (id.includes('@tanstack/')) return 'query-vendor';
          if (id.includes('react-router')) return 'router-vendor';
          if (id.includes('/react/') || id.includes('/react-dom/') || id.includes('/scheduler/')) {
            return 'react-vendor';
          }
          return undefined;
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test-setup.ts',
  },
});
