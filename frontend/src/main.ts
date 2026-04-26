/**
 * ============================================================================
 * frontend/src/main.ts
 * ----------------------------------------------------------------------------
 * Vue 3 应用入口 · 字体导入 · Pinia 注册 · 全局错误处理
 * ============================================================================
 */

import { createApp } from 'vue';
import { createPinia } from 'pinia';

// ── 字体导入 (完全离线,不走 Google Fonts CDN) ──
import '@fontsource/inter/400.css';
import '@fontsource/inter/500.css';
import '@fontsource/inter/600.css';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import '@fontsource/jetbrains-mono/700.css';

// ── 全局样式 (Tailwind base + 自定义 keyframes) ──
import './assets/styles/main.css';
import './assets/styles/keyframes.css';

// ── 根组件 ──
import App from './App.vue';

// ── 应用启动 ──
const app = createApp(App);
const pinia = createPinia();

app.use(pinia);

// ── 全局错误处理器 (开发期暴露,生产期降噪) ──
app.config.errorHandler = (err, instance, info) => {
  // eslint-disable-next-line no-console
  console.error('[App] 全局未捕获错误:', err, info);
};

app.config.warnHandler = (msg, instance, trace) => {
  // 过滤掉 Vue Devtools 噪音
  if (msg.includes('Extraneous non-props attributes')) return;
  // eslint-disable-next-line no-console
  console.warn('[App] Vue 警告:', msg, trace);
};

// ── 性能模式 (生产环境关闭 devtools) ──
if (import.meta.env.PROD) {
  app.config.performance = false;
}

// ── 挂载 ──
app.mount('#app');

// ── 全局诊断接口 (仅开发) ──
if (import.meta.env.DEV) {
  // @ts-expect-error global debug surface
  window.__APP_VERSION__ = __APP_VERSION__;
  // @ts-expect-error global debug surface
  window.__APP_NAME__ = __APP_NAME__;
}