/**
 * ============================================================================
 * frontend/src/types/protocol.d.ts
 * ----------------------------------------------------------------------------
 * 与 backend/schemas/stream_packet.py · backend/schemas/agent_state.py ·
 * backend/schemas/debate_context.py 严格 1:1 对齐的 TypeScript 类型契约。
 *
 * 任何字段变更必须 *双侧同步*,否则 WebSocket Discriminated Union 会失配。
 * ============================================================================
 */

// ============================================================================
// 枚举 · 与 backend 完全对齐
// ============================================================================

export type MBTIType =
  | 'INTJ'
  | 'INTP'
  | 'ENTJ'
  | 'ENTP'
  | 'INFJ'
  | 'INFP'
  | 'ENFJ'
  | 'ENFP'
  | 'ISTJ'
  | 'ISFJ'
  | 'ESTJ'
  | 'ESFJ'
  | 'ISTP'
  | 'ISFP'
  | 'ESTP'
  | 'ESFP';

export type Faction = 'NT' | 'NF' | 'SJ' | 'SP';

export type FSMStateName =
  | 'IDLE'
  | 'LISTENING'
  | 'RETRIEVING'
  | 'COMPUTING'
  | 'SPEAKING'
  | 'UPDATING';

export type SessionStatus =
  | 'INITIALIZING'
  | 'READY'
  | 'RUNNING'
  | 'PAUSED'
  | 'SHUTTING_DOWN';

export type RequestKind =
  | 'AGGRO_THRESHOLD'
  | 'INTERRUPT'
  | 'VACUUM_FILL'
  | 'SCHEDULED';

export type TruncationReason =
  | 'NORMAL'
  | 'INTERRUPTED'
  | 'WATCHDOG_TIMEOUT'
  | 'SYSTEM_ABORT';

export type TopicEventKind = 'PUSH' | 'POP' | 'CLEAR';

// ============================================================================
// Outbound 包 (服务端 → 客户端) · 8 类
// ============================================================================

export type OutboundKind =
  | 'STATE'
  | 'TEXT'
  | 'INTERRUPT'
  | 'TURN_SEALED'
  | 'TOPIC'
  | 'BYE'
  | 'HEARTBEAT'
  | 'ERROR';

export interface OutboundPacketBase {
  kind: OutboundKind;
  seq: number;
  emitted_at_wall_unix: number;
}

// ── STATE ──

export interface MicrophoneTokenView {
  holder_agent_id: string;
  request_kind: RequestKind;
  acquired_at_wall_unix: number;
  epoch: number;
}

export interface FSMStateView {
  name: FSMStateName;
  entered_at_wall_unix: number;
  age_seconds: number;
}

export interface AggroBucketView {
  current: number;
  min: number;
  max: number;
  decay_lambda: number;
  normalized: number;
  above_interrupt_threshold: boolean;
  last_updated_at_wall_unix: number;
}

export interface AgentDescriptorView {
  agent_id: string;
  mbti: MBTIType;
  faction: Faction;
  display_name: string;
  archetype: string;
  behavior_anchor: string;
  decay_lambda: number;
  has_chroma_access: boolean;
  is_summarizer: boolean;
}

export interface AgentStateView {
  descriptor: AgentDescriptorView;
  fsm: FSMStateView;
  aggro: AggroBucketView;
  is_microphone_holder: boolean;
}

export interface ArbitratorMetricsView {
  current_holder_id: string | null;
  current_epoch: number;
  current_token_age_sec: number;
  granted_total: number;
  interrupt_granted_total: number;
  rejected_total: number;
  debounce_drop_total: number;
  last_speaker_id: string | null;
  cooldown_remaining_sec: number;
  seconds_since_any_speech: number;
}

export interface WatchdogEvictionView {
  agent_id: string;
  kind: 'soft' | 'hard';
  occurred_at_wall_unix: number;
  reason: string;
}

export interface WatchdogMetricsView {
  is_running: boolean;
  in_flight_count: number;
  soft_evict_total: number;
  hard_evict_total: number;
  cancelled_total: number;
  last_eviction: WatchdogEvictionView | null;
}

export interface LLMSemaphoreHolderView {
  holder_name: string;
  acquired_at_wall_unix: number;
  age_sec: number;
}

export interface LLMSemaphoreMetricsView {
  capacity: number;
  held_count: number;
  waiting_count: number;
  acquired_total: number;
  released_total: number;
  timeout_total: number;
  cancelled_total: number;
  last_acquire_wait_ms: number;
  holders: LLMSemaphoreHolderView[];
}

export interface OutboundStatePacket extends OutboundPacketBase {
  kind: 'STATE';
  agents: AgentStateView[];
  arbitrator: ArbitratorMetricsView;
  watchdog: WatchdogMetricsView;
  semaphore: LLMSemaphoreMetricsView;
  session_status: SessionStatus;
}

// ── TEXT ──

export interface OutboundTextPacket extends OutboundPacketBase {
  kind: 'TEXT';
  agent_id: string;
  text: string;
  /** 影子后缀 (例: "[系统强制截断:被 X 夺取麦克风]") · 仅截断后才有 */
  shadow_suffix: string | null;
}

// ── INTERRUPT ──

export interface OutboundInterruptPacket extends OutboundPacketBase {
  kind: 'INTERRUPT';
  interrupter_agent_id: string;
  interrupted_agent_id: string;
}

// ── TURN_SEALED ──

export interface OutboundTurnSealedPacket extends OutboundPacketBase {
  kind: 'TURN_SEALED';
  turn_id: string;
  agent_id: string;
  topic_id: string;
  text: string;
  truncation: TruncationReason;
  interrupter_agent_id: string | null;
  shadow_suffix: string | null;
  duration_seconds: number;
  sealed_at_wall_unix: number;
}

// ── TOPIC ──

export interface TopicView {
  topic_id: string;
  title: string;
  description: string;
  pushed_at_wall_unix: number;
}

export interface OutboundTopicPacket extends OutboundPacketBase {
  kind: 'TOPIC';
  event_kind: TopicEventKind;
  current_topic: TopicView | null;
  stack_depth: number;
}

// ── BYE ──

export type ByeReason =
  | 'SERVER_SHUTDOWN'
  | 'PROTOCOL_VIOLATION'
  | 'IDLE_TIMEOUT'
  | 'OPERATOR';

export interface OutboundByePacket extends OutboundPacketBase {
  kind: 'BYE';
  reason: ByeReason;
  detail: string;
}

// ── HEARTBEAT ──

export interface OutboundHeartbeatPacket extends OutboundPacketBase {
  kind: 'HEARTBEAT';
  server_uptime_sec: number;
}

// ── ERROR ──

export type ErrorSeverity = 'INFO' | 'WARNING' | 'ERROR' | 'FATAL';

export interface OutboundErrorPacket extends OutboundPacketBase {
  kind: 'ERROR';
  severity: ErrorSeverity;
  code: string;
  message: string;
}

// ── Outbound Discriminated Union ──

export type OutboundPacket =
  | OutboundStatePacket
  | OutboundTextPacket
  | OutboundInterruptPacket
  | OutboundTurnSealedPacket
  | OutboundTopicPacket
  | OutboundByePacket
  | OutboundHeartbeatPacket
  | OutboundErrorPacket;

// ============================================================================
// Inbound 包 (客户端 → 服务端) · 6 类
// ============================================================================

export type InboundKind =
  | 'PING'
  | 'PAUSE_SESSION'
  | 'RESUME_SESSION'
  | 'INJECT_TOPIC'
  | 'POP_TOPIC'
  | 'FORCE_INTERRUPT';

export interface InboundPacketBase {
  kind: InboundKind;
  client_seq: number;
  sent_at_wall_unix: number;
}

export interface InboundPingPacket extends InboundPacketBase {
  kind: 'PING';
  client_token: string;
}

export interface InboundPausePacket extends InboundPacketBase {
  kind: 'PAUSE_SESSION';
  reason: string | null;
}

export interface InboundResumePacket extends InboundPacketBase {
  kind: 'RESUME_SESSION';
  reason: string | null;
}

export interface InjectTopicPayload {
  title: string;
  description: string;
}

export interface InboundInjectTopicPacket extends InboundPacketBase {
  kind: 'INJECT_TOPIC';
  payload: InjectTopicPayload;
}

export interface InboundPopTopicPacket extends InboundPacketBase {
  kind: 'POP_TOPIC';
}

export interface ForceInterruptPayload {
  reason: string;
}

export interface InboundForceInterruptPacket extends InboundPacketBase {
  kind: 'FORCE_INTERRUPT';
  payload: ForceInterruptPayload;
}

export type InboundPacket =
  | InboundPingPacket
  | InboundPausePacket
  | InboundResumePacket
  | InboundInjectTopicPacket
  | InboundPopTopicPacket
  | InboundForceInterruptPacket;

// ============================================================================
// REST 响应模型 · 与 backend/schemas/debate_context.py 对齐
// ============================================================================

export interface SessionInfoView {
  session_id: string;
  status: SessionStatus;
  started_at_wall_unix: number;
  uptime_seconds: number;
  total_turns_sealed: number;
  total_interrupts: number;
  backend_version: string;
}

export interface TopicStackView {
  current: TopicView | null;
  stack: TopicView[];
  depth: number;
}

export interface TurnView {
  turn_id: string;
  agent_id: string;
  topic_id: string;
  text: string;
  truncation: TruncationReason;
  interrupter_agent_id: string | null;
  duration_seconds: number;
  sealed_at_wall_unix: number;
}

export interface ShortMemoryView {
  turns: TurnView[];
  window_size: number;
  current_size: number;
  overflow_queue_size: number;
}

export interface DebateSnapshotView {
  topic_stack: TopicStackView;
  short_memory: ShortMemoryView;
  ongoing_agent_ids: string[];
}

export interface CompleteSandboxSnapshot {
  session: SessionInfoView;
  debate: DebateSnapshotView;
  agents: AgentStateView[];
  arbitrator: ArbitratorMetricsView;
  watchdog: WatchdogMetricsView;
  semaphore: LLMSemaphoreMetricsView;
  server_at_wall_unix: number;
}

export interface HealthCheckResponse {
  status: 'ok' | 'degraded' | 'shutting_down';
  session_id: string;
  uptime_seconds: number;
  backend_version: string;
}

// ── REST 控制 API ──

export interface InjectTopicRequest {
  title: string;
  description: string;
  auto_start_debate: boolean;
}

export interface InjectTopicResponse {
  accepted: boolean;
  topic: TopicView | null;
  message: string;
}

export interface PopTopicResponse {
  accepted: boolean;
  popped_topic: TopicView | null;
  remaining_depth: number;
  message: string;
}

export type ControlActionKind =
  | 'PAUSE'
  | 'RESUME'
  | 'FORCE_INTERRUPT'
  | 'EMERGENCY_STOP';

export interface ControlActionRequest {
  action: ControlActionKind;
  reason: string | null;
}

export interface ControlActionResponse {
  accepted: boolean;
  action: ControlActionKind;
  new_status: SessionStatus;
  message: string;
  server_at_wall_unix: number;
}

// ============================================================================
// 客户端内部辅助类型 (非协议层,但与协议字段紧耦合)
// ============================================================================

/** 一段进行中的发言 (Turn 未封存前) · 由 TEXT 包驱动 */
export interface OngoingSpeech {
  agent_id: string;
  text_buffer: string;
  shadow_suffix: string | null;
  started_at_client_unix: number;
  last_chunk_at_client_unix: number;
}

/** 已封存的 Turn (前端展示用,源自 TURN_SEALED 包) */
export interface SealedTurn {
  turn_id: string;
  agent_id: string;
  topic_id: string;
  text: string;
  truncation: TruncationReason;
  interrupter_agent_id: string | null;
  shadow_suffix: string | null;
  duration_seconds: number;
  sealed_at_wall_unix: number;
  /** 客户端附加字段:接收时刻 */
  received_at_client_unix: number;
}

/** WS 连接状态 */
export type WsConnState =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'closed'
  | 'failed';