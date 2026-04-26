/**
 * ============================================================================
 * frontend/src/stores/debate.ts
 * ----------------------------------------------------------------------------
 * 辩论会话状态 Pinia Store
 *
 * 持有:
 *   - SessionStatus / 议题栈 / 短记忆窗口
 *   - 进行中发言 (按 agent_id 索引,每个 agent 同时只能有 1 条 ongoing)
 *   - 已封存 Turn 列表 (上限 200,超出 FIFO 丢)
 *   - 最近 Interrupt 时间戳 (供 useInterruptShake 触发动画)
 *   - WS 连接状态
 *
 * 所有 mutator 走方法,绝不让组件直接改 state.
 * ============================================================================
 */

import { defineStore } from 'pinia';
import type {
  CompleteSandboxSnapshot,
  OngoingSpeech,
  OutboundInterruptPacket,
  OutboundTextPacket,
  OutboundTopicPacket,
  OutboundTurnSealedPacket,
  SealedTurn,
  SessionStatus,
  TopicView,
  WsConnState,
} from '@/types/protocol';

const SEALED_TURN_HISTORY_MAX = 200;

interface DebateState {
  sessionStatus: SessionStatus;
  sessionId: string | null;
  backendVersion: string | null;

  topicCurrent: TopicView | null;
  topicStack: TopicView[];
  topicDepth: number;

  /** key = agent_id · 同时进行的所有 ongoing 发言 */
  ongoing: Record<string, OngoingSpeech>;

  /** 时间倒序 (最近的在末尾) · 上限 SEALED_TURN_HISTORY_MAX */
  sealedTurns: SealedTurn[];

  /** 最近一次 Interrupt 事件触发的时间戳 (client unix ms),供动画订阅 */
  lastInterruptAt: number;
  /** 最近一次 Interrupt 事件的双方 ID */
  lastInterruptDetail: {
    interrupter: string;
    interrupted: string;
  } | null;

  wsState: WsConnState;
  wsLastConnectedAt: number;
  wsReconnectAttempts: number;
}

export const useDebateStore = defineStore('debate', {
  state: (): DebateState => ({
    sessionStatus: 'INITIALIZING',
    sessionId: null,
    backendVersion: null,

    topicCurrent: null,
    topicStack: [],
    topicDepth: 0,

    ongoing: {},
    sealedTurns: [],

    lastInterruptAt: 0,
    lastInterruptDetail: null,

    wsState: 'idle',
    wsLastConnectedAt: 0,
    wsReconnectAttempts: 0,
  }),

  getters: {
    isRunning(state): boolean {
      return state.sessionStatus === 'RUNNING';
    },
    isPaused(state): boolean {
      return state.sessionStatus === 'PAUSED';
    },
    isShuttingDown(state): boolean {
      return state.sessionStatus === 'SHUTTING_DOWN';
    },
    /** 当前正在发言的 Agent ID 列表 (有 ongoing 缓冲即视为发言中) */
    speakingAgentIds(state): string[] {
      return Object.keys(state.ongoing);
    },
    /** 最后一条已封存的 Turn (供 TopicBanner 副标题展示) */
    latestSealed(state): SealedTurn | null {
      const len = state.sealedTurns.length;
      return len > 0 ? (state.sealedTurns[len - 1] ?? null) : null;
    },
  },

  actions: {
    // ── 全量水合 (REST snapshot) ──────────────────────────────────────────

    hydrateFromSnapshot(snap: CompleteSandboxSnapshot): void {
      this.sessionStatus = snap.session.status;
      this.sessionId = snap.session.session_id;
      this.backendVersion = snap.session.backend_version;

      this.topicCurrent = snap.debate.topic_stack.current;
      this.topicStack = snap.debate.topic_stack.stack;
      this.topicDepth = snap.debate.topic_stack.depth;

      // 短记忆 → SealedTurn 列表 (附 client unix)
      const nowClient = Date.now();
      this.sealedTurns = snap.debate.short_memory.turns.map((t) => ({
        turn_id: t.turn_id,
        agent_id: t.agent_id,
        topic_id: t.topic_id,
        text: t.text,
        truncation: t.truncation,
        interrupter_agent_id: t.interrupter_agent_id,
        shadow_suffix: null,
        duration_seconds: t.duration_seconds,
        sealed_at_wall_unix: t.sealed_at_wall_unix,
        received_at_client_unix: nowClient,
      }));

      // 水合时清空所有 ongoing (服务端 source-of-truth)
      this.ongoing = {};
    },

    // ── WS 状态变更 ───────────────────────────────────────────────────────

    setWsState(s: WsConnState): void {
      this.wsState = s;
      if (s === 'open') {
        this.wsLastConnectedAt = Date.now();
        this.wsReconnectAttempts = 0;
      } else if (s === 'reconnecting') {
        this.wsReconnectAttempts += 1;
      }
    },

    // ── STATE 包: 仅同步 sessionStatus (其他指标在 agents store) ─────────

    applyStatePacket(sessionStatus: SessionStatus): void {
      this.sessionStatus = sessionStatus;
    },

    // ── TEXT 包: 累加进 ongoing[agent_id] ────────────────────────────────

    applyTextPacket(p: OutboundTextPacket): void {
      const aid = p.agent_id;
      if (!aid) return;

      const existing = this.ongoing[aid];
      const nowClient = Date.now();

      if (existing) {
        existing.text_buffer += p.text;
        existing.last_chunk_at_client_unix = nowClient;
        if (p.shadow_suffix) {
          existing.shadow_suffix = p.shadow_suffix;
        }
      } else {
        this.ongoing[aid] = {
          agent_id: aid,
          text_buffer: p.text,
          shadow_suffix: p.shadow_suffix,
          started_at_client_unix: nowClient,
          last_chunk_at_client_unix: nowClient,
        };
      }
    },

    // ── INTERRUPT 包: 仅触发动画用时间戳 (Turn 实际状态由 TURN_SEALED 给出) ──

    applyInterruptPacket(p: OutboundInterruptPacket): void {
      this.lastInterruptAt = Date.now();
      this.lastInterruptDetail = {
        interrupter: p.interrupter_agent_id,
        interrupted: p.interrupted_agent_id,
      };
    },

    // ── TURN_SEALED 包: 把 ongoing 提到 sealedTurns,并清掉对应 agent 的缓冲 ──

    applyTurnSealedPacket(p: OutboundTurnSealedPacket): void {
      // 1) 移除 ongoing
      delete this.ongoing[p.agent_id];

      // 2) 推入 sealedTurns
      const sealed: SealedTurn = {
        turn_id: p.turn_id,
        agent_id: p.agent_id,
        topic_id: p.topic_id,
        text: p.text,
        truncation: p.truncation,
        interrupter_agent_id: p.interrupter_agent_id,
        shadow_suffix: p.shadow_suffix,
        duration_seconds: p.duration_seconds,
        sealed_at_wall_unix: p.sealed_at_wall_unix,
        received_at_client_unix: Date.now(),
      };
      this.sealedTurns.push(sealed);

      // 3) 容量上限
      if (this.sealedTurns.length > SEALED_TURN_HISTORY_MAX) {
        this.sealedTurns.splice(
          0,
          this.sealedTurns.length - SEALED_TURN_HISTORY_MAX,
        );
      }
    },

    // ── TOPIC 包 ──────────────────────────────────────────────────────────

    applyTopicPacket(p: OutboundTopicPacket): void {
      this.topicCurrent = p.current_topic;
      this.topicDepth = p.stack_depth;
      // 注: stack 列表后端没在 TOPIC 包里下发,仅 snapshot 提供;
      // 这里保留旧 stack,等下次 snapshot 刷新
    },

    // ── 重置 (BYE 或断连后) ───────────────────────────────────────────────

    softReset(): void {
      this.ongoing = {};
      this.lastInterruptAt = 0;
      this.lastInterruptDetail = null;
    },
  },
});