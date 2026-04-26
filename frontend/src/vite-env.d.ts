/// <reference types="vite/client" />

declare const __APP_VERSION__: string;
declare const __APP_NAME__: string;

interface ImportMetaEnv {
  readonly VITE_BACKEND_HOST?: string;
  readonly VITE_BACKEND_PORT?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}