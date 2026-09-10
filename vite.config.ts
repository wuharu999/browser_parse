import { defineConfig } from 'vitest/config';

export default defineConfig({
  server: { proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false } } },
  test: {
    include: ['tests/**/*.test.ts'],
  },
});
