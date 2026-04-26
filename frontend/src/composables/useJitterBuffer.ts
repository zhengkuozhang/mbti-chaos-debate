/**
 * ============================================================================
 * frontend/src/composables/useJitterBuffer.ts
 * ----------------------------------------------------------------------------
 * 抖动缓冲 + 打字机平滑吐字引擎
 *
 * 目标:
 *   把后端 ~100ms 节流推送的 TEXT chunk 在客户端平滑成"逐字符"打字机效果,
 *   消除网络抖动可见性 (用户看不见明显的"chunk 一蹦一蹦的接收点")。
 *
 * 核心机制:
 *   1. 每 agent 一个独立 BufferEntry · 维护剩余字符队列
 *   2. 单一全局 RAF loop · 每帧根据 deltaTime 计算每个 agent 应该吐多少字符
 *   3. 自适应吐字速率:
 *        baseRate = 32 chars/sec
 *        adaptive = baseRate × (1 + min(2.0, queueLen/40))   // 长队列加速
 *   4. flushAgent / flushAll: 立即吐完剩余缓冲 (TURN_SEALED / INTERRUPT 触发)
 *   5. 标签隐藏时降级为"立即模式" (page visibility API)
 *
 * 与 stores/debate 的关系:
 *   buffer 持有"未消费字符",debateStore.ongoing[agent_id].text_buffer 是
 *   "已消费的可见字符"。本 composable 是从 buffer → store 的桥梁。
 *
 * ⚠️ 工程铁律:
 *   - 严禁在 RAF 内做 O(N) 遍历 (N = 字符总数) → 每帧只处理活跃 agent
 *   - 严禁让 RAF 在标签隐藏后空转 → visibilitychange 监听
 *   - 严禁让 buffer 内字符数无限堆积 → flush 兜底 + 单 buffer 上限 4000
 * ============================================================================
 */

import type { OutboundTextPacket } from '@/types/protocol';
import { useDebateStore } from '@/stores/debate';

// ── 常量 ──────────────────────────────────────────────────────────────────

/** 基础吐字速率 (字符 / 秒) */
const BASE_TYPING_RATE_CPS = 32;

/** 单 agent buffer 上限,超出强制 flush 防止 OOM */
const PER_AGENT_BUFFER_HARD_LIMIT = 4_000;

/** 自适应放大上限 (baseRate × 3.0 = 96 cps) */
const ADAPTIVE_RATE_MAX_MULTIPLIER = 3.0;

/** 当队列长度达到此值开始自适应加速 */
const ADAPTIVE_RAMP_START = 12;

/** 长队列饱和点 (达到此值后已是 max multiplier) */
const ADAPTIVE_RAMP_FULL = 80;

// ── 内部数据结构 ──────────────────────────────────────────────────────────

interface BufferEntry {
  agentId: string;
  /** 待消费字符串 (尾部 push,头部 shift) */
  pending: string;
  /** 浮点累计,RAF deltaTime 累加,达 1.0 才吐一个字符 */
  charAccumulator: number;
  /** 当前帧使用的吐字速率 (字符 / 秒) */
  currentRate: number;
  /** 该 agent 最近一次有更新的时间 (用于检测"卡死") */
  lastUpdatedAt: number;
}

interface SingletonState {
  buffers: Map<string, BufferEntry>;
  rafId: number | null;
  lastTickAt: number;
  /** 标签是否可见 · 不可见时降级 */
  pageVisible: boolean;
  visibilityHandler: (() => void) | null;
  refCount: number;
}

let _singleton: SingletonState | null = null;

function getSingleton(): SingletonState {
  if (_singleton) return _singleton;
  _singleton = {
    buffers: new Map<string, BufferEntry>(),
    rafId: null,
    lastTickAt: 0,
    pageVisible:
      typeof document === 'undefined' ? true : !document.hidden,
    visibilityHandler: null,
    refCount: 0,
  };
  return _singleton;
}

// ── 自适应吐字速率 ────────────────────────────────────────────────────────

function computeAdaptiveRate(pendingLen: number): number {
  if (pendingLen <= ADAPTIVE_RAMP_START) {
    return BASE_TYPING_RATE_CPS;
  }
  if (pendingLen >= ADAPTIVE_RAMP_FULL) {
    return BASE_TYPING_RATE_CPS * ADAPTIVE_RATE_MAX_MULTIPLIER;
  }
  // 线性插值
  const t =
    (pendingLen - ADAPTIVE_RAMP_START) /
    (ADAPTIVE_RAMP_FULL - ADAPTIVE_RAMP_START);
  const multiplier =
    1 + (ADAPTIVE_RATE_MAX_MULTIPLIER - 1) * t;
  return BASE_TYPING_RATE_CPS * multiplier;
}

// ── RAF 主循环 ────────────────────────────────────────────────────────────

function tick(now: number): void {
  const sg = getSingleton();
  sg.rafId = null;

  if (sg.lastTickAt === 0) {
    sg.lastTickAt = now;
    scheduleNextTickIfNeeded();
    return;
  }
  const dtSec = Math.min(0.1, (now - sg.lastTickAt) / 1000); // 上限 100ms 防大停顿
  sg.lastTickAt = now;

  // 仅在标签可见时做平滑;不可见时由 feedTextChunk 直接 flush
  if (!sg.pageVisible) {
    scheduleNextTickIfNeeded();
    return;
  }

  if (sg.buffers.size === 0) {
    // 空闲时不持续 RAF,等待下次 feed 触发
    sg.lastTickAt = 0;
    return;
  }

  // 取 store 引用一次,本帧内复用
  const debateStore = useDebateStore();

  for (const [agentId, entry] of sg.buffers) {
    if (entry.pending.length === 0) {
      // 无字符可吐 · 可能是已 flush 完成,延迟 1 帧后清理
      // 立即移除,避免无限占用 map slot
      sg.buffers.delete(agentId);
      continue;
    }

    // 自适应速率
    entry.currentRate = computeAdaptiveRate(entry.pending.length);
    entry.charAccumulator += entry.currentRate * dtSec;

    let charsToEmit = Math.floor(entry.charAccumulator);
    if (charsToEmit <= 0) continue;

    if (charsToEmit > entry.pending.length) {
      charsToEmit = entry.pending.length;
    }

    const slice = entry.pending.slice(0, charsToEmit);
    entry.pending = entry.pending.slice(charsToEmit);
    entry.charAccumulator -= charsToEmit;
    entry.lastUpdatedAt = now;

    // 推到 store · 合成一个伪 TEXT 包形式
    debateStore.applyTextPacket({
      kind: 'TEXT',
      seq: 0,
      emitted_at_wall_unix: Date.now() / 1000,
      agent_id: agentId,
      text: slice,
      shadow_suffix: null,
    });

    if (entry.pending.length === 0) {
      sg.buffers.delete(agentId);
    }
  }

  scheduleNextTickIfNeeded();
}

function scheduleNextTickIfNeeded(): void {
  const sg = getSingleton();
  if (sg.rafId !== null) return;
  if (sg.buffers.size === 0) return;
  if (typeof window === 'undefined' || !window.requestAnimationFrame) return;

  sg.rafId = window.requestAnimationFrame(tick);
}

// ── Page Visibility 集成 ──────────────────────────────────────────────────

function ensureVisibilityHandler(): void {
  const sg = getSingleton();
  if (sg.visibilityHandler !== null) return;

  const handler = (): void => {
    sg.pageVisible = !document.hidden;
    if (!sg.pageVisible) {
      // 隐藏 → 立即 flush 全部缓冲到 store
      flushAllInternal();
    } else {
      // 重新可见 → 重启 RAF
      sg.lastTickAt = 0;
      scheduleNextTickIfNeeded();
    }
  };
  document.addEventListener('visibilitychange', handler);
  sg.visibilityHandler = handler;
}

function teardownVisibilityHandler(): void {
  const sg = getSingleton();
  if (sg.visibilityHandler === null) return;
  document.removeEventListener('visibilitychange', sg.visibilityHandler);
  sg.visibilityHandler = null;
}

// ── feedTextChunk: 入站 TEXT 包接入 ───────────────────────────────────────

function feedTextChunkInternal(packet: OutboundTextPacket): void {
  const sg = getSingleton();
  const aid = packet.agent_id;
  if (!aid || !packet.text) {
    // 仅 shadow_suffix 的特殊情况: 直接转 store (走 Pinia mutator 已经处理 shadow_suffix)
    if (packet.shadow_suffix && aid) {
      const debateStore = useDebateStore();
      debateStore.applyTextPacket(packet);
    }
    return;
  }

  // 标签隐藏 → 立即 flush 不走平滑
  if (!sg.pageVisible) {
    const debateStore = useDebateStore();
    debateStore.applyTextPacket(packet);
    return;
  }

  let entry = sg.buffers.get(aid);
  if (!entry) {
    entry = {
      agentId: aid,
      pending: '',
      charAccumulator: 0,
      currentRate: BASE_TYPING_RATE_CPS,
      lastUpdatedAt: performance.now(),
    };
    sg.buffers.set(aid, entry);
  }

  entry.pending += packet.text;

  // shadow_suffix 不进 buffer · 它是封存时刻才有 (TEXT 包通常不带 shadow_suffix)
  // 若极端情况 TEXT 包附带 shadow_suffix,直接合并入 store
  if (packet.shadow_suffix) {
    const debateStore = useDebateStore();
    debateStore.applyTextPacket({
      ...packet,
      text: '', // 文本部分留给 RAF 平滑吐
      shadow_suffix: packet.shadow_suffix,
    });
  }

  // 上限保护
  if (entry.pending.length > PER_AGENT_BUFFER_HARD_LIMIT) {
    // eslint-disable-next-line no-console
    console.warn(
      `[jitter] agent=${aid} buffer 超过 ${PER_AGENT_BUFFER_HARD_LIMIT},强制 flush`,
    );
    flushAgentInternal(aid, null);
  }

  scheduleNextTickIfNeeded();
}

// ── flushAgent / flushAll ─────────────────────────────────────────────────

function flushAgentInternal(
  agentId: string,
  shadowSuffix: string | null,
): void {
  const sg = getSingleton();
  const entry = sg.buffers.get(agentId);

  // 立即把 buffer 中剩余字符 + shadow_suffix 推到 store
  if (entry && entry.pending.length > 0) {
    const debateStore = useDebateStore();
    debateStore.applyTextPacket({
      kind: 'TEXT',
      seq: 0,
      emitted_at_wall_unix: Date.now() / 1000,
      agent_id: agentId,
      text: entry.pending,
      shadow_suffix: shadowSuffix,
    });
  } else if (shadowSuffix) {
    // 没有 pending 但有 shadow_suffix · 单独推
    const debateStore = useDebateStore();
    debateStore.applyTextPacket({
      kind: 'TEXT',
      seq: 0,
      emitted_at_wall_unix: Date.now() / 1000,
      agent_id: agentId,
      text: '',
      shadow_suffix: shadowSuffix,
    });
  }

  sg.buffers.delete(agentId);

  if (sg.buffers.size === 0 && sg.rafId !== null) {
    window.cancelAnimationFrame(sg.rafId);
    sg.rafId = null;
    sg.lastTickAt = 0;
  }
}

function flushAllInternal(): void {
  const sg = getSingleton();
  const ids = [...sg.buffers.keys()];
  for (const id of ids) {
    flushAgentInternal(id, null);
  }
}

// ── 公开 API ──────────────────────────────────────────────────────────────

export interface UseJitterBufferApi {
  feedTextChunk: (packet: OutboundTextPacket) => void;
  flushAgent: (agentId: string, shadowSuffix?: string | null) => void;
  flushAll: () => void;
  /** 调试用 · 当前所有缓冲的统计 */
  inspect: () => Array<{ agentId: string; pendingLen: number; rate: number }>;
}

export function useJitterBuffer(): UseJitterBufferApi {
  const sg = getSingleton();
  sg.refCount += 1;

  ensureVisibilityHandler();

  // 不在 onBeforeUnmount 中 teardown · 因为这是模块级单例,被多个组件共用
  // 仅在 refCount == 0 时清理 (但一般整个页面生命周期都活)

  return {
    feedTextChunk: feedTextChunkInternal,
    flushAgent: (agentId: string, shadowSuffix: string | null = null): void =>
      flushAgentInternal(agentId, shadowSuffix),
    flushAll: flushAllInternal,
    inspect(): Array<{ agentId: string; pendingLen: number; rate: number }> {
      return [...sg.buffers.values()].map((e) => ({
        agentId: e.agentId,
        pendingLen: e.pending.length,
        rate: e.currentRate,
      }));
    },
  };
}

// ── 全局清理 (供测试 / HMR) ───────────────────────────────────────────────

export function __resetJitterBufferForTesting(): void {
  const sg = getSingleton();
  if (sg.rafId !== null) {
    window.cancelAnimationFrame(sg.rafId);
    sg.rafId = null;
  }
  teardownVisibilityHandler();
  sg.buffers.clear();
  sg.lastTickAt = 0;
  _singleton = null;
}