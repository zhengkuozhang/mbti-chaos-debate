import { defineConfig, loadEnv } from 'vite';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath, URL } from 'node:url';

/**
 * ============================================================================
 * Vite 配置
 * ============================================================================
 *
 * 关键点:
 *   1. /api/* 与 /ws/* 走 proxy 到 backend (容器内 backend:8000 / 本机 localhost:8000)
 *   2. WebSocket proxy 启用 ws:true,保持长连接
 *   3. Build 输出到 dist/,供 frontend Dockerfile 拷贝到 nginx
 *   4. 严禁开启 sourcemap externals (本地完全离线运行,不联网拉资源)
 *   5. 自定义 alias '@' → src/
 * ============================================================================
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');

  // 开发模式下后端地址 (Docker compose 内部用 backend:8000;本机调试用 localhost:8000)
  const backendHost = env.VITE_BACKEND_HOST || 'localhost';
  const backendPort = env.VITE_BACKEND_PORT || '8000';
  const backendUrl = `http://${backendHost}:${backendPort}`;
  const backendWsUrl = `ws://${backendHost}:${backendPort}`;

  return {
    plugins: [vue()],

    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },

    server: {
      host: '0.0.0.0',
      port: 5173,
      strictPort: true,
      // HMR 在容器内需要明确告知客户端走 ws://
      hmr: {
        host: 'localhost',
        port: 5173,
        protocol: 'ws',
      },
      proxy: {
        '/api': {
          target: backendUrl,
          changeOrigin: true,
          ws: false,
        },
        '/ws': {
          target: backendWsUrl,
          changeOrigin: true,
          ws: true,
        },
      },
    },

    preview: {
      host: '0.0.0.0',
      port: 5173,
      strictPort: true,
    },

    build: {
      target: 'es2022',
      outDir: 'dist',
      emptyOutDir: true,
      sourcemap: false,
      cssCodeSplit: true,
      assetsInlineLimit: 4096,
      rollupOptions: {
        output: {
          manualChunks: {
            'vendor-vue': ['vue', 'pinia'],
            'vendor-fonts': [
              '@fontsource/inter/400.css',
              '@fontsource/inter/500.css',
              '@fontsource/inter/600.css',
              '@fontsource/jetbrains-mono/400.css',
              '@fontsource/jetbrains-mono/500.css',
              '@fontsource/jetbrains-mono/700.css',
            ],
          },
        },
      },
    },

    optimizeDeps: {
      include: ['vue', 'pinia', '@vueuse/core'],
    },

    define: {
      __APP_VERSION__: JSON.stringify('1.0.0'),
      __APP_NAME__: JSON.stringify('MBTI Chaos Debate Sandbox'),
    },
  };
});