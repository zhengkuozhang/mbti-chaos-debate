/**
 * ============================================================================
 * frontend/src/stores/agents.ts
 * ----------------------------------------------------------------------------
 * Agents 状态 Pinia Store
 *
 * 持有:
 *   - 8 个 AgentStateView (按 agent_id 索引)
 *   - Arbitrator / Watchdog / LLMSemaphore 全局指标
 *
 * 数据源:
 *   - REST /api/sandbox/snapshot 全量水合
 *   - WS STATE 包增量覆盖 (整体替换,不做 merge)
 * ============================================================================
 */

import { defineStore } from 'pinia';
import type {
  AgentStateView,
  ArbitratorMetricsView,
  CompleteSandboxSnapshot,
  Faction,
  LLMSemaphoreMetricsView,
  OutboundStatePacket,
  WatchdogMetricsView,
} from '@/types/protocol';

interface AgentsState {
  /** key = agent_id */
  byId: Record<string, AgentStateView>;
  /** 注册顺序 (与后端 NT→NF→SJ→SP 一致) */
  orderedIds: string[];

  arbitrator: ArbitratorMetricsView;
  watchdog: WatchdogMetricsView;
  semaphore: LLMSemaphoreMetricsView;

  lastUpdatedAtClientUnix: number;
}

const DEFAULT_ARBITRATOR: ArbitratorMetricsView = Object.freeze({
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
});

const DEFAULT_WATCHDOG: WatchdogMetricsView = Object.freeze({
  is_running: false,
  in_flight_count: 0,
  soft_evict_total: 0,
  hard_evict_total: 0,
  cancelled_total: 0,
  last_eviction: null,
});

const DEFAULT_SEMAPHORE: LLMSemaphoreMetricsView = Object.freeze({
  capacity: 1,
  held_count: 0,
  waiting_count: 0,
  acquired_total: 0,
  released_total: 0,
  timeout_total: 0,
  cancelled_total: 0,
  last_acquire_wait_ms: 0,
  holders: [],
});

export const useAgentsStore = defineStore('agents', {
  state: (): AgentsState => ({
    byId: {},
    orderedIds: [],
    arbitrator: { ...DEFAULT_ARBITRATOR },
    watchdog: { ...DEFAULT_WATCHDOG },
    semaphore: { ...DEFAULT_SEMAPHORE },
    lastUpdatedAtClientUnix: 0,
  }),

  getters: {
    asList(state): AgentStateView[] {
      return state.orderedIds
        .map((id) => state.byId[id])
        .filter((a): a is AgentStateView => a !== undefined);
    },

    agentsByFaction(state) {
      return (faction: Faction): AgentStateView[] =>
        state.orderedIds
          .map((id) => state.byId[id])
          .filter((a): a is AgentStateView => a !== undefined)
          .filter((a) => a.descriptor.faction === faction);
    },

    /** 当前持麦 Agent (供高亮显示) */
    currentHolderId(state): string | null {
      return state.arbitrator.current_holder_id;
    },

    /** Aggro >= 70 的 Agent ID (用于 pulse 触发) */
    burningAgentIds(state): string[] {
      return state.orderedIds
        .map((id) => state.byId[id])
        .filter((a): a is AgentStateView => a !== undefined)
        .filter((a) => a.aggro.current >= 70)
        .map((a) => a.descriptor.agent_id);
    },
  },

  actions: {
    hydrateFromSnapshot(snap: CompleteSandboxSnapshot): void {
      const byId: Record<string, AgentStateView> = {};
      const orderedIds: string[] = [];

      for (const a of snap.agents) {
        const id = a.descriptor.agent_id;
        byId[id] = a;
        orderedIds.push(id);
      }

      this.byId = byId;
      this.orderedIds = orderedIds;
      this.arbitrator = snap.arbitrator;
      this.watchdog = snap.watchdog;
      this.semaphore = snap.semaphore;
      this.lastUpdatedAtClientUnix = Date.now();
    },

    applyStatePacket(p: OutboundStatePacket): void {
      const byId: Record<string, AgentStateView> = {};
      // 保留原 orderedIds (后端注册顺序不变);若 STATE 给的 id 不在原顺序里,追加到末尾
      const orderedSet = new Set(this.orderedIds);
      const newOrdered: string[] = [...this.orderedIds];

      for (const a of p.agents) {
        const id = a.descriptor.agent_id;
        byId[id] = a;
        if (!orderedSet.has(id)) {
          newOrdered.push(id);
          orderedSet.add(id);
        }
      }

      this.byId = byId;
      this.orderedIds = newOrdered;
      this.arbitrator = p.arbitrator;
      this.watchdog = p.watchdog;
      this.semaphore = p.semaphore;
      this.lastUpdatedAtClientUnix = Date.now();
    },

    /** 仅供测试 / 紧急重置 */
    reset(): void {
      this.byId = {};
      this.orderedIds = [];
      this.arbitrator = { ...DEFAULT_ARBITRATOR };
      this.watchdog = { ...DEFAULT_WATCHDOG };
      this.semaphore = { ...DEFAULT_SEMAPHORE };
      this.lastUpdatedAtClientUnix = 0;
    },
  },
});