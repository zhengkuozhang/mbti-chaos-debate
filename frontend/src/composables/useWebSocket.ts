/**
 * ============================================================================
 * frontend/src/composables/useWebSocket.ts
 * ----------------------------------------------------------------------------
 * WebSocket 双向通信引擎
 *
 * 设计原则:
 *   1. 进程级单例 - 一个页面只一个 WS 连接 (防止 mount 抖动重复连接)
 *   2. 指数退避重连 - 1s/2s/4s/8s/16s/30s 封顶,直到显式 stop 或致命 BYE
 *   3. 客户端心跳 - 25s 发 PING,60s 未收任何包视为僵尸连接
 *   4. 入站路由分发 - switch on kind,直接调 stores mutator
 *   5. 出站发送队列 - 离线时入队 (上限 50),重连后冲洗
 *   6. 严格类型 - 出入站全部走 protocol.d.ts Discriminated Union
 *   7. 任何异常路径必须走 finally 清理 timer / RAF / 引用
 * ============================================================================
 */

import { onBeforeUnmount, onMounted, readonly, ref } from 'vue';
import type { Ref } from 'vue';

import { useAgentsStore } from '@/stores/agents';
import { useDebateStore } from '@/stores/debate';
import {
  parseOutboundFrame,
  serializeInboundPacket,
} from '@/utils/packetParser';
import type {
  ByeReason,
  InboundPacket,
  OutboundPacket,
  WsConnState,
} from '@/types/protocol';
import { useJitterBuffer } from '@/composables/useJitterBuffer';

// ── 常量 ──────────────────────────────────────────────────────────────────

const HEARTBEAT_INTERVAL_MS = 25_000;
const ZOMBIE_TIMEOUT_MS = 60_000;
const RECONNECT_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000];
const OUTBOUND_QUEUE_MAX = 50;

/** 致命的 BYE 原因 - 收到后停止所有重连 */
const FATAL_BYE_REASONS: ReadonlySet<ByeReason> = new Set([
  'PROTOCOL_VIOLATION',
]);

// ── 模块级单例状态 ────────────────────────────────────────────────────────

interface SingletonState {
  socket: WebSocket | null;
  state: WsConnState;
  clientSeq: number;
  reconnectAttempt: number;
  reconnectTimer: number | null;
  heartbeatTimer: number | null;
  zombieCheckTimer: number | null;
  lastInboundAt: number;
  outboundQueue: InboundPacket[];
  manuallyStopped: boolean;
  refCount: number;
  /** 暴露给 composable 调用方的响应式状态 */
  reactiveState: Ref<WsConnState>;
  reactiveLastInboundAt: Ref<number>;
}

let _singleton: SingletonState | null = null;

function getOrInitSingleton(): SingletonState {
  if (_singleton) return _singleton;

  _singleton = {
    socket: null,
    state: 'idle',
    clientSeq: 0,
    reconnectAttempt: 0,
    reconnectTimer: null,
    heartbeatTimer: null,
    zombieCheckTimer: null,
    lastInboundAt: 0,
    outboundQueue: [],
    manuallyStopped: false,
    refCount: 0,
    reactiveState: ref<WsConnState>('idle'),
    reactiveLastInboundAt: ref<number>(0),
  };
  return _singleton;
}

// ── URL 构造 ──────────────────────────────────────────────────────────────

function buildWsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  // Vite dev proxy 与 nginx prod proxy 都把 /ws/* 转发到后端
  return `${proto}//${window.location.host}/ws/debate`;
}

// ── 状态变更 (同步更新单例 + reactive ref + Pinia store) ──────────────────

function setState(s: WsConnState): void {
  const sg = getOrInitSingleton();
  sg.state = s;
  sg.reactiveState.value = s;
  // 同步到 debate store (其他组件可订阅 debateStore.wsState)
  try {
    const debateStore = useDebateStore();
    debateStore.setWsState(s);
  } catch {
    // Pinia 未就绪时静默 (理论上 onMounted 后必定就绪)
  }
}

// ── 计算下次重连等待 ──────────────────────────────────────────────────────

function nextBackoffMs(attempt: number): number {
  const idx = Math.min(attempt, RECONNECT_BACKOFF_MS.length - 1);
  return RECONNECT_BACKOFF_MS[idx] ?? 30_000;
}
// ── 启动连接 ──────────────────────────────────────────────────────────────

function connect(): void {
  const sg = getOrInitSingleton();

  if (sg.manuallyStopped) {
    // eslint-disable-next-line no-console
    console.info('[ws] connect() 跳过: manuallyStopped');
    return;
  }

  if (sg.socket && (sg.socket.readyState === WebSocket.OPEN || sg.socket.readyState === WebSocket.CONNECTING)) {
    // eslint-disable-next-line no-console
    console.debug('[ws] connect() 跳过: 已存在活跃 socket');
    return;
  }

  setState(sg.reconnectAttempt === 0 ? 'connecting' : 'reconnecting');

  const url = buildWsUrl();
  // eslint-disable-next-line no-console
  console.info(`[ws] CONNECT → ${url} (attempt=${sg.reconnectAttempt})`);

  let ws: WebSocket;
  try {
    ws = new WebSocket(url);
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error('[ws] new WebSocket 抛异常:', err);
    scheduleReconnect();
    return;
  }
  ws.binaryType = 'arraybuffer';
  sg.socket = ws;

  ws.addEventListener('open', onOpen);
  ws.addEventListener('message', onMessage);
  ws.addEventListener('close', onClose);
  ws.addEventListener('error', onError);
}

// ── 事件处理 ──────────────────────────────────────────────────────────────

function onOpen(): void {
  const sg = getOrInitSingleton();
  // eslint-disable-next-line no-console
  console.info('[ws] OPEN');

  setState('open');
  sg.reconnectAttempt = 0;
  sg.lastInboundAt = Date.now();
  sg.reactiveLastInboundAt.value = sg.lastInboundAt;

  // 启动心跳与僵尸检测
  startHeartbeat();
  startZombieCheck();

  // 冲洗离线队列
  flushOutboundQueue();
}

async function onMessage(ev: MessageEvent): Promise<void> {
  const sg = getOrInitSingleton();
  sg.lastInboundAt = Date.now();
  sg.reactiveLastInboundAt.value = sg.lastInboundAt;

  // 解析
  const packet = await parseOutboundFrame(
    ev.data as Blob | ArrayBuffer | string,
  );
  if (!packet) {
    // 已在 parser 内 console.warn,这里不再重复
    return;
  }

  // 路由分发
  try {
    dispatchInbound(packet);
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error('[ws] dispatchInbound 异常 (吞掉,继续接收):', err, packet);
  }
}

function onClose(ev: CloseEvent): void {
  const sg = getOrInitSingleton();
  // eslint-disable-next-line no-console
  console.info(
    `[ws] CLOSE · code=${ev.code} reason=${ev.reason || '(empty)'} clean=${ev.wasClean}`,
  );

  cleanupTimers();
  sg.socket = null;

  if (sg.manuallyStopped) {
    setState('closed');
    return;
  }

  // 进入重连
  scheduleReconnect();
}

function onError(ev: Event): void {
  // eslint-disable-next-line no-console
  console.warn('[ws] ERROR event (close 即将触发):', ev);
  // 不在 error 中触发重连,等 close 事件统一处理
}

// ── 入站路由 ──────────────────────────────────────────────────────────────

function dispatchInbound(packet: OutboundPacket): void {
  const debateStore = useDebateStore();
  const agentsStore = useAgentsStore();
  const jitter = useJitterBuffer();

  switch (packet.kind) {
    case 'STATE': {
      agentsStore.applyStatePacket(packet);
      debateStore.applyStatePacket(packet.session_status);
      return;
    }
    case 'TEXT': {
      // 把 chunk 推入抖动缓冲,由 RAF 平滑吐字到 store.ongoing
      jitter.feedTextChunk(packet);
      return;
    }
    case 'INTERRUPT': {
      // 立即冲洗 interrupted 的剩余缓冲 (避免被打断后还在吐字)
      jitter.flushAgent(packet.interrupted_agent_id);
      debateStore.applyInterruptPacket(packet);
      return;
    }
    case 'TURN_SEALED': {
      // 立即吐完剩余 + 影子后缀,然后封存
      jitter.flushAgent(packet.agent_id, packet.shadow_suffix);
      debateStore.applyTurnSealedPacket(packet);
      return;
    }
    case 'TOPIC': {
      debateStore.applyTopicPacket(packet);
      // 议题切换时所有 ongoing 缓冲一律失效
      jitter.flushAll();
      return;
    }
    case 'BYE': {
      // eslint-disable-next-line no-console
      console.warn(`[ws] 收到 BYE · reason=${packet.reason} detail=${packet.detail}`);
      handleBye(packet.reason);
      return;
    }
    case 'HEARTBEAT': {
      // 仅心跳,lastInboundAt 已在 onMessage 更新
      return;
    }
    case 'ERROR': {
      // eslint-disable-next-line no-console
      console.warn(
        `[ws] 收到 ERROR · severity=${packet.severity} code=${packet.code} msg=${packet.message}`,
      );
      return;
    }
    default: {
      return;
    }
  }
}

// ── BYE 处理 ──────────────────────────────────────────────────────────────

function handleBye(reason: ByeReason): void {
  const sg = getOrInitSingleton();

  if (FATAL_BYE_REASONS.has(reason)) {
    // eslint-disable-next-line no-console
    console.error(`[ws] 致命 BYE (${reason}) 停止所有重连`);
    sg.manuallyStopped = true;
    setState('failed');
    if (sg.socket) {
      try {
        sg.socket.close(1000, 'fatal_bye');
      } catch {
        /* noop */
      }
    }
    return;
  }

  // 非致命 BYE: 让 onClose 自然触发重连
}

// ── 心跳 + 僵尸检测 ───────────────────────────────────────────────────────

function startHeartbeat(): void {
  const sg = getOrInitSingleton();
  if (sg.heartbeatTimer !== null) return;

  sg.heartbeatTimer = window.setInterval(() => {
    sendInbound({
      kind: 'PING',
      client_seq: nextClientSeq(),
      sent_at_wall_unix: Date.now() / 1000,
      client_token: `cli_${Date.now().toString(36)}`,
    });
  }, HEARTBEAT_INTERVAL_MS);
}

function startZombieCheck(): void {
  const sg = getOrInitSingleton();
  if (sg.zombieCheckTimer !== null) return;

  sg.zombieCheckTimer = window.setInterval(() => {
    const idle = Date.now() - sg.lastInboundAt;
    if (idle > ZOMBIE_TIMEOUT_MS) {
      // eslint-disable-next-line no-console
      console.warn(
        `[ws] 僵尸连接 (${idle}ms 无入站包),主动关闭以触发重连`,
      );
      forceCloseAndReconnect('zombie');
    }
  }, 5_000);
}

function cleanupTimers(): void {
  const sg = getOrInitSingleton();
  if (sg.heartbeatTimer !== null) {
    window.clearInterval(sg.heartbeatTimer);
    sg.heartbeatTimer = null;
  }
  if (sg.zombieCheckTimer !== null) {
    window.clearInterval(sg.zombieCheckTimer);
    sg.zombieCheckTimer = null;
  }
  if (sg.reconnectTimer !== null) {
    window.clearTimeout(sg.reconnectTimer);
    sg.reconnectTimer = null;
  }
}

function forceCloseAndReconnect(reason: string): void {
  const sg = getOrInitSingleton();
  if (sg.socket) {
    try {
      sg.socket.close(4001, reason.slice(0, 64));
    } catch {
      /* noop · onClose 会触发后续逻辑 */
    }
  }
}

// ── 重连调度 ──────────────────────────────────────────────────────────────

function scheduleReconnect(): void {
  const sg = getOrInitSingleton();
  if (sg.manuallyStopped) {
    setState('closed');
    return;
  }

  if (sg.reconnectTimer !== null) return;

  setState('reconnecting');
  const wait = nextBackoffMs(sg.reconnectAttempt);
  sg.reconnectAttempt += 1;
  // eslint-disable-next-line no-console
  console.info(
    `[ws] 重连调度 #${sg.reconnectAttempt} · ${wait}ms 后重试`,
  );

  sg.reconnectTimer = window.setTimeout(() => {
    sg.reconnectTimer = null;
    connect();
  }, wait);
}

// ── 出站发送 ──────────────────────────────────────────────────────────────

function nextClientSeq(): number {
  const sg = getOrInitSingleton();
  sg.clientSeq += 1;
  return sg.clientSeq;
}

function sendInbound(packet: InboundPacket): void {
  const sg = getOrInitSingleton();
  const ws = sg.socket;
  const isOpen = ws !== null && ws.readyState === WebSocket.OPEN;

  if (!isOpen) {
    // 入队 (上限保护)
    if (sg.outboundQueue.length >= OUTBOUND_QUEUE_MAX) {
      sg.outboundQueue.shift(); // FIFO 丢最旧
    }
    sg.outboundQueue.push(packet);
    return;
  }

  try {
    const text = serializeInboundPacket(packet);
    ws!.send(text);
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn('[ws] send 失败,加入队列待重试:', err);
    if (sg.outboundQueue.length < OUTBOUND_QUEUE_MAX) {
      sg.outboundQueue.push(packet);
    }
  }
}

function flushOutboundQueue(): void {
  const sg = getOrInitSingleton();
  if (sg.outboundQueue.length === 0) return;

  // eslint-disable-next-line no-console
  console.info(`[ws] 冲洗离线队列 (size=${sg.outboundQueue.length})`);

  const queue = sg.outboundQueue;
  sg.outboundQueue = [];
  for (const p of queue) {
    sendInbound(p);
  }
}

// ── 公开 API (语义化封装) ─────────────────────────────────────────────────

export interface UseWebSocketApi {
  /** 当前 WS 连接状态 (响应式) */
  state: Readonly<Ref<WsConnState>>;
  /** 最近一次收到入站包的时刻 (client unix ms) */
  lastInboundAt: Readonly<Ref<number>>;
  /** 主动启动 (idempotent) */
  start: () => void;
  /** 主动停止 + 关闭 socket + 取消所有 timer */
  stop: () => void;
  /** 发送入站包 (离线时入队) */
  send: (packet: InboundPacket) => void;
  /** 便捷:发送 PING */
  sendPing: () => void;
  /** 便捷:发送 INJECT_TOPIC */
  injectTopic: (title: string, description: string) => void;
  /** 便捷:发送 POP_TOPIC */
  popTopic: () => void;
  /** 便捷:发送 PAUSE / RESUME */
  pauseSession: (reason?: string) => void;
  resumeSession: (reason?: string) => void;
  /** 便捷:发送 FORCE_INTERRUPT */
  forceInterrupt: (reason: string) => void;
}

/**
 * Composable 入口 · 引用计数式生命周期。
 * 多次调用返回同一单例;refCount 归零时自动 stop。
 */
export function useWebSocket(): UseWebSocketApi {
  const sg = getOrInitSingleton();
  sg.refCount += 1;

  onMounted(() => {
    if (sg.manuallyStopped) {
      sg.manuallyStopped = false;
    }
    if (!sg.socket || sg.socket.readyState >= WebSocket.CLOSING) {
      connect();
    }
  });

  onBeforeUnmount(() => {
    sg.refCount = Math.max(0, sg.refCount - 1);
    if (sg.refCount === 0) {
      stop();
    }
  });

  function start(): void {
    sg.manuallyStopped = false;
    if (!sg.socket || sg.socket.readyState >= WebSocket.CLOSING) {
      connect();
    }
  }

  function stop(): void {
    sg.manuallyStopped = true;
    cleanupTimers();
    if (sg.socket) {
      try {
        sg.socket.close(1000, 'client_stop');
      } catch {
        /* noop */
      }
      sg.socket = null;
    }
    setState('closed');
    sg.outboundQueue = [];
    sg.reconnectAttempt = 0;
  }

  function send(packet: InboundPacket): void {
    sendInbound(packet);
  }

  function sendPing(): void {
    sendInbound({
      kind: 'PING',
      client_seq: nextClientSeq(),
      sent_at_wall_unix: Date.now() / 1000,
      client_token: `cli_${Date.now().toString(36)}`,
    });
  }

  function injectTopic(title: string, description: string): void {
    sendInbound({
      kind: 'INJECT_TOPIC',
      client_seq: nextClientSeq(),
      sent_at_wall_unix: Date.now() / 1000,
      payload: { title, description },
    });
  }

  function popTopic(): void {
    sendInbound({
      kind: 'POP_TOPIC',
      client_seq: nextClientSeq(),
      sent_at_wall_unix: Date.now() / 1000,
    });
  }

  function pauseSession(reason?: string): void {
    sendInbound({
      kind: 'PAUSE_SESSION',
      client_seq: nextClientSeq(),
      sent_at_wall_unix: Date.now() / 1000,
      reason: reason ?? null,
    });
  }

  function resumeSession(reason?: string): void {
    sendInbound({
      kind: 'RESUME_SESSION',
      client_seq: nextClientSeq(),
      sent_at_wall_unix: Date.now() / 1000,
      reason: reason ?? null,
    });
  }

  function forceInterrupt(reason: string): void {
    sendInbound({
      kind: 'FORCE_INTERRUPT',
      client_seq: nextClientSeq(),
      sent_at_wall_unix: Date.now() / 1000,
      payload: { reason },
    });
  }

  return {
    state: readonly(sg.reactiveState),
    lastInboundAt: readonly(sg.reactiveLastInboundAt),
    start,
    stop,
    send,
    sendPing,
    injectTopic,
    popTopic,
    pauseSession,
    resumeSession,
    forceInterrupt,
  };
}