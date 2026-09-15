import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath } from 'node:url';

export default defineConfig(({ command }) => ({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: command === 'build' ? '/static/' : '/',
  plugins: [vue()],
  build: { outDir: '../frontend-dist', emptyOutDir: true },
  server: {
    host: '127.0.0.1', port: 5173, strictPort: true,
    proxy: Object.fromEntries(['/api', '/qoj', '/healthz', '/static/math-config.js'].map(path => [path, { target: process.env.QOJ_BACKEND || 'http://127.0.0.1:8080', changeOrigin: false }])),
  },
}));
