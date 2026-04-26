/**
 * ============================================================================
 * frontend/src/utils/packetParser.ts
 * ----------------------------------------------------------------------------
 * 入站二进制 / 文本帧 → OutboundPacket 解析器
 *
 * 设计原则:
 *   1. 严禁抛错 — 任何畸形包返回 null,WS 主循环继续运行
 *   2. 类型守卫式校验 — Discriminated Union 的 kind 字段作主分支
 *   3. 容错降级 — 缺字段时填默认值而非拒收 (前端展示比断连更重要)
 *   4. 客户端 client_seq 由 useWebSocket.ts 管理,本文件仅负责解析
 * ============================================================================
 */

import type {
  OutboundPacket,
  OutboundKind,
  OutboundStatePacket,
  OutboundTextPacket,
  OutboundInterruptPacket,
  OutboundTurnSealedPacket,
  OutboundTopicPacket,
  OutboundByePacket,
  OutboundHeartbeatPacket,
  OutboundErrorPacket,
} from '@/types/protocol';

// ── 解析入口 ──────────────────────────────────────────────────────────────

/**
 * 解析 WebSocket onmessage 事件 data。
 *
 * @param data Blob | ArrayBuffer | string (取决于 socket.binaryType)
 * @returns 成功则返回 OutboundPacket;失败返回 null (静默,已 console.warn)
 */
export async function parseOutboundFrame(
  data: Blob | ArrayBuffer | string,
): Promise<OutboundPacket | null> {
  let jsonText: string;

  try {
    if (typeof data === 'string') {
      jsonText = data;
    } else if (data instanceof ArrayBuffer) {
      jsonText = new TextDecoder('utf-8', { fatal: false }).decode(data);
    } else if (data instanceof Blob) {
      jsonText = await data.text();
    } else {
      // eslint-disable-next-line no-console
      console.warn('[packetParser] 未识别的 data 类型,丢弃帧');
      return null;
    }
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn('[packetParser] 解码 bytes → text 失败,丢弃:', err);
    return null;
  }

  return parseOutboundJson(jsonText);
}

/** 同步版本 · 当确定上层已是 string 时使用 */
export function parseOutboundJson(jsonText: string): OutboundPacket | null {
  let raw: unknown;
  try {
    raw = JSON.parse(jsonText);
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn('[packetParser] JSON.parse 失败,丢弃:', err, jsonText.slice(0, 80));
    return null;
  }

  if (raw === null || typeof raw !== 'object') {
    // eslint-disable-next-line no-console
    console.warn('[packetParser] 顶层非 object,丢弃');
    return null;
  }

  const obj = raw as Record<string, unknown>;
  const kind = obj['kind'];

  if (typeof kind !== 'string') {
    // eslint-disable-next-line no-console
    console.warn('[packetParser] kind 字段缺失或非 string,丢弃');
    return null;
  }

  switch (kind as OutboundKind) {
    case 'STATE':
      return parseStatePacket(obj);
    case 'TEXT':
      return parseTextPacket(obj);
    case 'INTERRUPT':
      return parseInterruptPacket(obj);
    case 'TURN_SEALED':
      return parseTurnSealedPacket(obj);
    case 'TOPIC':
      return parseTopicPacket(obj);
    case 'BYE':
      return parseByePacket(obj);
    case 'HEARTBEAT':
      return parseHeartbeatPacket(obj);
    case 'ERROR':
      return parseErrorPacket(obj);
    default:
      // eslint-disable-next-line no-console
      console.warn(`[packetParser] 未知 kind=${String(kind)},丢弃`);
      return null;
  }
}

// ── 通用字段提取 ──────────────────────────────────────────────────────────

function pickNumber(obj: Record<string, unknown>, key: string, fallback: number): number {
  const v = obj[key];
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback;
}

function pickString(
  obj: Record<string, unknown>,
  key: string,
  fallback: string,
): string {
  const v = obj[key];
  return typeof v === 'string' ? v : fallback;
}

function pickStringOrNull(
  obj: Record<string, unknown>,
  key: string,
): string | null {
  const v = obj[key];
  if (typeof v === 'string') return v;
  if (v === null) return null;
  return null;
}

function pickBool(obj: Record<string, unknown>, key: string, fallback: boolean): boolean {
  const v = obj[key];
  return typeof v === 'boolean' ? v : fallback;
}

function pickObject(obj: Record<string, unknown>, key: string): Record<string, unknown> | null {
  const v = obj[key];
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    return v as Record<string, unknown>;
  }
  return null;
}

function pickArray(obj: Record<string, unknown>, key: string): unknown[] {
  const v = obj[key];
  return Array.isArray(v) ? v : [];
}

function commonHeader(obj: Record<string, unknown>): {
  seq: number;
  emitted_at_wall_unix: number;
} {
  return {
    seq: pickNumber(obj, 'seq', 0),
    emitted_at_wall_unix: pickNumber(obj, 'emitted_at_wall_unix', 0),
  };
}

// ── STATE ─────────────────────────────────────────────────────────────────

function parseStatePacket(obj: Record<string, unknown>): OutboundStatePacket {
  return {
    kind: 'STATE',
    ...commonHeader(obj),
    agents: pickArray(obj, 'agents') as OutboundStatePacket['agents'],
    arbitrator: (pickObject(obj, 'arbitrator') ??
      defaultArbitrator()) as OutboundStatePacket['arbitrator'],
    watchdog: (pickObject(obj, 'watchdog') ??
      defaultWatchdog()) as OutboundStatePacket['watchdog'],
    semaphore: (pickObject(obj, 'semaphore') ??
      defaultSemaphore()) as OutboundStatePacket['semaphore'],
    session_status: pickString(
      obj,
      'session_status',
      'INITIALIZING',
    ) as OutboundStatePacket['session_status'],
  };
}

function defaultArbitrator(): OutboundStatePacket['arbitrator'] {
  return {
    current_holder_id: null,
    current_epoch: 0,
    current_token_age_sec: 0,
    granted_total: 0,
    interrupt_granted_total: 0,
    rejected_total: 0,
    debounce_drop_total: 0,
    last_speaker_id: null,
    cooldown_remaining_sec: 0,
    seconds_since_any_speech: 0,
  };
}

function defaultWatchdog(): OutboundStatePacket['watchdog'] {
  return {
    is_running: false,
    in_flight_count: 0,
    soft_evict_total: 0,
    hard_evict_total: 0,
    cancelled_total: 0,
    last_eviction: null,
  };
}

function defaultSemaphore(): OutboundStatePacket['semaphore'] {
  return {
    capacity: 1,
    held_count: 0,
    waiting_count: 0,
    acquired_total: 0,
    released_total: 0,
    timeout_total: 0,
    cancelled_total: 0,
    last_acquire_wait_ms: 0,
    holders: [],
  };
}

// ── TEXT ──────────────────────────────────────────────────────────────────

function parseTextPacket(obj: Record<string, unknown>): OutboundTextPacket {
  return {
    kind: 'TEXT',
    ...commonHeader(obj),
    agent_id: pickString(obj, 'agent_id', ''),
    text: pickString(obj, 'text', ''),
    shadow_suffix: pickStringOrNull(obj, 'shadow_suffix'),
  };
}

// ── INTERRUPT ─────────────────────────────────────────────────────────────

function parseInterruptPacket(
  obj: Record<string, unknown>,
): OutboundInterruptPacket {
  return {
    kind: 'INTERRUPT',
    ...commonHeader(obj),
    interrupter_agent_id: pickString(obj, 'interrupter_agent_id', ''),
    interrupted_agent_id: pickString(obj, 'interrupted_agent_id', ''),
  };
}

// ── TURN_SEALED ──────────────────────────────────────────────────────────

function parseTurnSealedPacket(
  obj: Record<string, unknown>,
): OutboundTurnSealedPacket {
  return {
    kind: 'TURN_SEALED',
    ...commonHeader(obj),
    turn_id: pickString(obj, 'turn_id', ''),
    agent_id: pickString(obj, 'agent_id', ''),
    topic_id: pickString(obj, 'topic_id', ''),
    text: pickString(obj, 'text', ''),
    truncation: pickString(
      obj,
      'truncation',
      'NORMAL',
    ) as OutboundTurnSealedPacket['truncation'],
    interrupter_agent_id: pickStringOrNull(obj, 'interrupter_agent_id'),
    shadow_suffix: pickStringOrNull(obj, 'shadow_suffix'),
    duration_seconds: pickNumber(obj, 'duration_seconds', 0),
    sealed_at_wall_unix: pickNumber(obj, 'sealed_at_wall_unix', 0),
  };
}

// ── TOPIC ─────────────────────────────────────────────────────────────────

function parseTopicPacket(obj: Record<string, unknown>): OutboundTopicPacket {
  const currentRaw = pickObject(obj, 'current_topic');
  return {
    kind: 'TOPIC',
    ...commonHeader(obj),
    event_kind: pickString(
      obj,
      'event_kind',
      'PUSH',
    ) as OutboundTopicPacket['event_kind'],
    current_topic: currentRaw
      ? {
          topic_id: pickString(currentRaw, 'topic_id', ''),
          title: pickString(currentRaw, 'title', ''),
          description: pickString(currentRaw, 'description', ''),
          pushed_at_wall_unix: pickNumber(currentRaw, 'pushed_at_wall_unix', 0),
        }
      : null,
    stack_depth: pickNumber(obj, 'stack_depth', 0),
  };
}

// ── BYE ───────────────────────────────────────────────────────────────────

function parseByePacket(obj: Record<string, unknown>): OutboundByePacket {
  return {
    kind: 'BYE',
    ...commonHeader(obj),
    reason: pickString(obj, 'reason', 'SERVER_SHUTDOWN') as OutboundByePacket['reason'],
    detail: pickString(obj, 'detail', ''),
  };
}

// ── HEARTBEAT ─────────────────────────────────────────────────────────────

function parseHeartbeatPacket(
  obj: Record<string, unknown>,
): OutboundHeartbeatPacket {
  return {
    kind: 'HEARTBEAT',
    ...commonHeader(obj),
    server_uptime_sec: pickNumber(obj, 'server_uptime_sec', 0),
  };
}

// ── ERROR ─────────────────────────────────────────────────────────────────

function parseErrorPacket(obj: Record<string, unknown>): OutboundErrorPacket {
  return {
    kind: 'ERROR',
    ...commonHeader(obj),
    severity: pickString(
      obj,
      'severity',
      'ERROR',
    ) as OutboundErrorPacket['severity'],
    code: pickString(obj, 'code', 'UNKNOWN'),
    message: pickString(obj, 'message', ''),
  };
}

// ── 客户端出站序列化 (轻量,直接 JSON.stringify 即可) ──────────────────────

import type { InboundPacket } from '@/types/protocol';

export function serializeInboundPacket(packet: InboundPacket): string {
  // 出站走文本帧,后端 parse_inbound_packet 兼容 string + bytes
  return JSON.stringify(packet);
}

// ── 类型守卫导出 (供 stores 在 switch 中使用) ──────────────────────────────

export function isStatePacket(p: OutboundPacket): p is OutboundStatePacket {
  return p.kind === 'STATE';
}
export function isTextPacket(p: OutboundPacket): p is OutboundTextPacket {
  return p.kind === 'TEXT';
}
export function isInterruptPacket(p: OutboundPacket): p is OutboundInterruptPacket {
  return p.kind === 'INTERRUPT';
}
export function isTurnSealedPacket(p: OutboundPacket): p is OutboundTurnSealedPacket {
  return p.kind === 'TURN_SEALED';
}
export function isTopicPacket(p: OutboundPacket): p is OutboundTopicPacket {
  return p.kind === 'TOPIC';
}
export function isByePacket(p: OutboundPacket): p is OutboundByePacket {
  return p.kind === 'BYE';
}
export function isHeartbeatPacket(p: OutboundPacket): p is OutboundHeartbeatPacket {
  return p.kind === 'HEARTBEAT';
}
export function isErrorPacket(p: OutboundPacket): p is OutboundErrorPacket {
  return p.kind === 'ERROR';
}