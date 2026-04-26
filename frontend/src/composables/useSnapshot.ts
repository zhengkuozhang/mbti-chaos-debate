/**
 * ============================================================================
 * frontend/src/composables/useSnapshot.ts
 * ----------------------------------------------------------------------------
 * REST 全量水合管理器
 *
 * 职责:
 *   1. 启动时调 GET /api/sandbox/snapshot 完成 stores 全量水合
 *   2. 失败时进入指数退避重试 (1s/2s/4s/8s/16s,5 次)
 *   3. 成功后每 30s 静默轮询一次,矫正 STATE 包累积漂移
 *   4. AbortController 在卸载时取消所有未完成 fetch
 *   5. 暴露 phase 响应式状态供 UI 显示加载提示
 *
 * 时序:
 *   onMounted → fetchOnce → hydrate → start polling
 *
 * 设计原则:
 *   - 失败永远不抛 - 转化为 phase = 'degraded' 让 UI 提示用户
 *   - 与 useWebSocket 解耦 - 后者的连接独立于水合是否成功
 *   - 30s 轮询是"软矫正" · 主路径仍是 WS 增量
 * ============================================================================
 */

import { onBeforeUnmount, onMounted, readonly, ref } from 'vue';
import type { Ref } from 'vue';

import { useAgentsStore } from '@/stores/agents';
import { useDebateStore } from '@/stores/debate';
import type { CompleteSandboxSnapshot } from '@/types/protocol';

// ── 常量 ──────────────────────────────────────────────────────────────────

const SNAPSHOT_URL = '/api/sandbox/snapshot';
const HEALTHZ_URL = '/api/sandbox/healthz';
const MAX_INITIAL_RETRY = 5;
const RETRY_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 16_000];
const POLLING_INTERVAL_MS = 30_000;
const FETCH_TIMEOUT_MS = 8_000;

// ── 状态枚举 ──────────────────────────────────────────────────────────────

export type SnapshotPhase =
  | 'idle'
  | 'loading'
  | 'ready'
  | 'retrying'
  | 'degraded';

// ── 单例状态 (避免组件重复挂载触发多次水合) ───────────────────────────────

interface SingletonState {
  phase: Ref<SnapshotPhase>;
  lastFetchedAt: Ref<number>;
  lastError: Ref<string | null>;
  retryAttempt: number;
  retryTimer: number | null;
  pollingTimer: number | null;
  abortController: AbortController | null;
  refCount: number;
  initialized: boolean;
}

let _singleton: SingletonState | null = null;

function getSingleton(): SingletonState {
  if (_singleton) return _singleton;
  _singleton = {
    phase: ref<SnapshotPhase>('idle'),
    lastFetchedAt: ref<number>(0),
    lastError: ref<string | null>(null),
    retryAttempt: 0,
    retryTimer: null,
    pollingTimer: null,
    abortController: null,
    refCount: 0,
    initialized: false,
  };
  return _singleton;
}

// ── fetch 工具 (带超时) ───────────────────────────────────────────────────

async function fetchWithTimeout(
  url: string,
  signal: AbortSignal,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const onAbort = (): void => controller.abort();
  signal.addEventListener('abort', onAbort, { once: true });

  const timer = window.setTimeout(() => {
    controller.abort();
  }, timeoutMs);

  try {
    return await fetch(url, {
      method: 'GET',
      signal: controller.signal,
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
  } finally {
    window.clearTimeout(timer);
    signal.removeEventListener('abort', onAbort);
  }
}

// ── snapshot fetch + hydrate ──────────────────────────────────────────────

async function fetchAndHydrateOnce(signal: AbortSignal): Promise<void> {
  const sg = getSingleton();

  const resp = await fetchWithTimeout(SNAPSHOT_URL, signal, FETCH_TIMEOUT_MS);
  if (!resp.ok) {
    throw new Error(`snapshot HTTP ${resp.status}`);
  }

  const data = (await resp.json()) as CompleteSandboxSnapshot;

  // 基本校验 · 防止后端发空对象
  if (!data || typeof data !== 'object' || !data.session || !data.agents) {
    throw new Error('snapshot 格式异常: 缺少必需字段');
  }

  // 水合两个 store
  const debateStore = useDebateStore();
  const agentsStore = useAgentsStore();
  debateStore.hydrateFromSnapshot(data);
  agentsStore.hydrateFromSnapshot(data);

  sg.lastFetchedAt.value = Date.now();
  sg.lastError.value = null;
}

// ── 启动水合 (含重试调度) ─────────────────────────────────────────────────

async function bootstrapHydrate(): Promise<void> {
  const sg = getSingleton();
  if (sg.initialized) return;

  sg.initialized = true;
  sg.phase.value = 'loading';
  sg.retryAttempt = 0;

  await tryHydrate();
}

async function tryHydrate(): Promise<void> {
  const sg = getSingleton();

  if (sg.abortController) {
    sg.abortController.abort();
  }
  sg.abortController = new AbortController();

  try {
    await fetchAndHydrateOnce(sg.abortController.signal);
    sg.phase.value = 'ready';
    sg.retryAttempt = 0;
    startPolling();
    // eslint-disable-next-line no-console
    console.info('[snapshot] 水合完成,启动 30s 轮询');
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      // eslint-disable-next-line no-console
      console.debug('[snapshot] fetch 被取消');
      return;
    }
    const msg = err instanceof Error ? err.message : String(err);
    sg.lastError.value = msg;
    // eslint-disable-next-line no-console
    console.warn(`[snapshot] 水合失败 (attempt=${sg.retryAttempt}): ${msg}`);

    if (sg.retryAttempt < MAX_INITIAL_RETRY) {
      sg.phase.value = 'retrying';
      const wait =
        RETRY_BACKOFF_MS[
          Math.min(sg.retryAttempt, RETRY_BACKOFF_MS.length - 1)
        ] ?? 16_000;
      sg.retryAttempt += 1;
      sg.retryTimer = window.setTimeout(() => {
        sg.retryTimer = null;
        void tryHydrate();
      }, wait);
    } else {
      sg.phase.value = 'degraded';
      // eslint-disable-next-line no-console
      console.error(
        '[snapshot] 已重试 5 次仍失败 · 进入 degraded 模式 · WS 仍会尝试连接',
      );
    }
  }
}

// ── 30s 轮询 (软矫正) ─────────────────────────────────────────────────────

function startPolling(): void {
  const sg = getSingleton();
  if (sg.pollingTimer !== null) return;

  sg.pollingTimer = window.setInterval(() => {
    void pollingTick();
  }, POLLING_INTERVAL_MS);
}

async function pollingTick(): Promise<void> {
  const sg = getSingleton();
  if (sg.phase.value === 'degraded') return; // 已放弃,不再轮询

  if (sg.abortController) {
    sg.abortController.abort();
  }
  sg.abortController = new AbortController();

  try {
    await fetchAndHydrateOnce(sg.abortController.signal);
    // eslint-disable-next-line no-console
    console.debug('[snapshot] 轮询矫正成功');
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') return;
    const msg = err instanceof Error ? err.message : String(err);
    // eslint-disable-next-line no-console
    console.warn('[snapshot] 轮询矫正失败 (软):', msg);
    sg.lastError.value = msg;
    // 不切到 degraded · 仅记录,等下个 tick 再试
  }
}

function stopPolling(): void {
  const sg = getSingleton();
  if (sg.pollingTimer !== null) {
    window.clearInterval(sg.pollingTimer);
    sg.pollingTimer = null;
  }
}

// ── 卸载清理 ──────────────────────────────────────────────────────────────

function teardown(): void {
  const sg = getSingleton();
  stopPolling();
  if (sg.retryTimer !== null) {
    window.clearTimeout(sg.retryTimer);
    sg.retryTimer = null;
  }
  if (sg.abortController) {
    sg.abortController.abort();
    sg.abortController = null;
  }
}

// ── 公开 API ──────────────────────────────────────────────────────────────

export interface UseSnapshotApi {
  phase: Readonly<Ref<SnapshotPhase>>;
  lastFetchedAt: Readonly<Ref<number>>;
  lastError: Readonly<Ref<string | null>>;
  /** 手动触发重新水合 (UI 上的"重连"按钮) */
  refetch: () => Promise<void>;
  /** 检查后端 healthz · 返回 status 字符串或 null (网络错误) */
  pingHealth: () => Promise<string | null>;
}

export function useSnapshot(): UseSnapshotApi {
  const sg = getSingleton();
  sg.refCount += 1;

  onMounted(() => {
    if (!sg.initialized) {
      void bootstrapHydrate();
    }
  });

  onBeforeUnmount(() => {
    sg.refCount = Math.max(0, sg.refCount - 1);
    if (sg.refCount === 0) {
      teardown();
      sg.initialized = false;
    }
  });

  async function refetch(): Promise<void> {
    sg.retryAttempt = 0;
    sg.phase.value = 'loading';
    if (sg.retryTimer !== null) {
      window.clearTimeout(sg.retryTimer);
      sg.retryTimer = null;
    }
    await tryHydrate();
  }

  async function pingHealth(): Promise<string | null> {
    try {
      const ctrl = new AbortController();
      const resp = await fetchWithTimeout(HEALTHZ_URL, ctrl.signal, 4_000);
      if (!resp.ok) return null;
      const data = (await resp.json()) as { status?: string };
      return typeof data.status === 'string' ? data.status : null;
    } catch {
      return null;
    }
  }

  return {
    phase: readonly(sg.phase),
    lastFetchedAt: readonly(sg.lastFetchedAt),
    lastError: readonly(sg.lastError),
    refetch,
    pingHealth,
  };
}