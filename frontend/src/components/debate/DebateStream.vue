<!--
  ============================================================================
  DebateStream.vue · 中央辩论流主舞台
  ----------------------------------------------------------------------------
  - JetBrains Mono 等宽 · 自动滚动到底 · 顶/底 mask-fade-y 渐变
  - 三类内容混排:
      sealedTurns (已封存)  → 完整气泡
      ongoing[agent_id]      → 进行中,末尾打字机光标
      影子后缀 shadow_suffix → 红色 anim-shadow-suffix-fade 追加
  - 同一 agent 连续多轮共享头部 (减视觉噪声)
  - 打断动画通过 useInterruptShake 应用到容器根
  - 用户手动上滑暂停 auto-scroll;距底部 < 80px 恢复
  ============================================================================
-->

<template>
  <div
    ref="streamRoot"
    class="debate-stream relative flex h-full w-full flex-col overflow-hidden"
  >
    <!-- 顶部议题横幅 -->
    <TopicBanner class="shrink-0" />

    <!-- 滚动主体 -->
    <div
      ref="scrollContainer"
      class="debate-stream__scroll mask-fade-y flex-1 min-h-0 overflow-y-auto px-6 py-5"
      @scroll.passive="onUserScroll"
    >
      <div class="debate-stream__inner mx-auto flex max-w-3xl flex-col gap-3">
        <!-- 混排显示项 -->
        <template v-for="item in displayItems" :key="item.key">
          <!-- 分组头部 (新发言者首条) -->
          <div
            v-if="item.showHeader"
            class="debate-stream__head mt-1 flex items-baseline gap-3"
          >
            <span
              class="font-mono text-2xs uppercase tracking-hud-wide"
              :class="headerFactionClass(item.agentId)"
            >
              {{ item.headerLabel }}
            </span>
            <span class="font-mono text-2xs text-hud-muted/70">
              {{ item.timeLabel }}
            </span>
            <span
              v-if="item.holderHint"
              class="rounded border border-white/10 bg-white/[0.03] px-1.5 py-0.5 font-mono text-2xs text-hud-muted"
            >
              {{ item.holderHint }}
            </span>
          </div>

          <!-- 文本主体 -->
          <div
            class="debate-stream__bubble whitespace-pre-wrap break-words font-mono text-hud-mono-md leading-relaxed"
            :class="bubbleColorClass(item.agentId, item.kind)"
          >
            <span>{{ item.text }}</span>

            <!-- ongoing: 末尾光标 -->
            <span
              v-if="item.kind === 'ongoing'"
              class="anim-typewriter-caret"
              :class="bubbleColorClass(item.agentId, item.kind)"
              aria-hidden="true"
            ></span>

            <!-- 影子后缀 -->
            <span
              v-if="item.shadowSuffix"
              class="anim-shadow-suffix-fade ml-1 font-mono text-hud-mono-sm"
            >
              {{ item.shadowSuffix }}
            </span>
          </div>
        </template>

        <!-- 空状态 -->
        <div
          v-if="displayItems.length === 0"
          class="flex flex-1 flex-col items-center justify-center py-24 text-center"
        >
          <div
            class="font-mono text-2xs uppercase tracking-hud-wider text-hud-muted/60"
          >
            STAGE · STANDBY
          </div>
          <div class="mt-3 font-mono text-sm text-hud-muted">
            等待议题注入后辩论将自动开始
          </div>
        </div>
      </div>
    </div>

    <!-- 锁定到底按钮 (用户手动上滑后浮现) -->
    <button
      v-if="!autoScrollEnabled"
      class="hud-btn hud-btn--primary absolute bottom-4 left-1/2 -translate-x-1/2 shadow-glow-cyan"
      @click="resumeAutoScroll"
      type="button"
    >
      回到最新 ↓
    </button>

    <!-- 打断瞬间字幕 -->
    <InterruptOverlay class="pointer-events-none" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import InterruptOverlay from '@/components/debate/InterruptOverlay.vue';
import TopicBanner from '@/components/debate/TopicBanner.vue';
import { useInterruptShake } from '@/composables/useInterruptShake';
import { useAgentsStore } from '@/stores/agents';
import { useDebateStore } from '@/stores/debate';
import { factionColorClasses } from '@/utils/colorMap';
import type { Faction, SealedTurn, TruncationReason } from '@/types/protocol';

// ── refs ──────────────────────────────────────────────────────────────────

const streamRoot = ref<HTMLElement | null>(null);
const scrollContainer = ref<HTMLElement | null>(null);

const debateStore = useDebateStore();
const agentsStore = useAgentsStore();

// 打断动画绑定到根容器
useInterruptShake(streamRoot);

// ── 自动滚动控制 ─────────────────────────────────────────────────────────

const autoScrollEnabled = ref<boolean>(true);
const RESUME_THRESHOLD_PX = 80;

function onUserScroll(): void {
  const el = scrollContainer.value;
  if (!el) return;
  const distFromBottom = el.scrollHeight - (el.scrollTop + el.clientHeight);
  if (distFromBottom > RESUME_THRESHOLD_PX) {
    autoScrollEnabled.value = false;
  } else {
    autoScrollEnabled.value = true;
  }
}

function scrollToBottom(smooth: boolean = true): void {
  const el = scrollContainer.value;
  if (!el) return;
  el.scrollTo({
    top: el.scrollHeight,
    behavior: smooth ? 'smooth' : 'auto',
  });
}

function resumeAutoScroll(): void {
  autoScrollEnabled.value = true;
  scrollToBottom(true);
}

// ── 混排数据 ─────────────────────────────────────────────────────────────

interface DisplayItem {
  key: string;
  agentId: string;
  kind: 'sealed' | 'ongoing';
  text: string;
  shadowSuffix: string | null;
  showHeader: boolean;
  headerLabel: string;
  timeLabel: string;
  holderHint: string | null;
  truncation: TruncationReason | null;
}

function formatTime(ts: number): string {
  if (!ts || ts <= 0) return '';
  const d = new Date(ts * 1000);
  const hh = d.getHours().toString().padStart(2, '0');
  const mm = d.getMinutes().toString().padStart(2, '0');
  const ss = d.getSeconds().toString().padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}

function lookupAgentDisplay(agentId: string): {
  display: string;
  faction: Faction;
} {
  const agent = agentsStore.byId[agentId];
  if (agent) {
    return {
      display: agent.descriptor.display_name,
      faction: agent.descriptor.faction,
    };
  }
  // 回退: 显示原始 agent_id
  return { display: agentId || '?', faction: 'NT' };
}

const displayItems = computed<DisplayItem[]>(() => {
  const items: DisplayItem[] = [];
  let lastAgentIdInGroup: string | null = null;

  // ── 1. 已封存 Turn ──
  for (const turn of debateStore.sealedTurns) {
    const showHeader = turn.agent_id !== lastAgentIdInGroup;
    const lookup = lookupAgentDisplay(turn.agent_id);

    let truncationHint: string | null = null;
    let shadowSuffix: string | null = null;

    if (turn.truncation === 'INTERRUPTED') {
      const interrupter = turn.interrupter_agent_id ?? '?';
      const interrupterDisplay =
        lookupAgentDisplay(interrupter).display;
      shadowSuffix = `[系统强制截断:被 ${interrupterDisplay} 夺取麦克风]`;
      truncationHint = 'INTERRUPTED';
    } else if (turn.truncation === 'WATCHDOG_TIMEOUT') {
      shadowSuffix = '[系统强制截断:看门狗超时]';
      truncationHint = 'WATCHDOG';
    } else if (turn.truncation === 'SYSTEM_ABORT') {
      shadowSuffix = '[系统强制截断:系统中止]';
      truncationHint = 'ABORT';
    }

    items.push({
      key: `sealed:${turn.turn_id}`,
      agentId: turn.agent_id,
      kind: 'sealed',
      text: turn.text,
      shadowSuffix,
      showHeader,
      headerLabel: lookup.display,
      timeLabel: formatTime(turn.sealed_at_wall_unix),
      holderHint: truncationHint,
      truncation: turn.truncation,
    });

    lastAgentIdInGroup = turn.agent_id;
  }

  // ── 2. 进行中 ongoing ──
  for (const aid of Object.keys(debateStore.ongoing)) {
    const speech = debateStore.ongoing[aid];
    if (!speech) continue;
    const showHeader = aid !== lastAgentIdInGroup;
    const lookup = lookupAgentDisplay(aid);

    items.push({
      key: `ongoing:${aid}`,
      agentId: aid,
      kind: 'ongoing',
      text: speech.text_buffer,
      shadowSuffix: speech.shadow_suffix,
      showHeader,
      headerLabel: lookup.display,
      timeLabel: formatTime(speech.started_at_client_unix / 1000),
      holderHint: 'SPEAKING',
      truncation: null,
    });

    lastAgentIdInGroup = aid;
  }

  return items;
});

// ── 颜色工具 ──────────────────────────────────────────────────────────────

function headerFactionClass(agentId: string): string {
  const lookup = lookupAgentDisplay(agentId);
  return factionColorClasses(lookup.faction).text;
}

function bubbleColorClass(agentId: string, kind: 'sealed' | 'ongoing'): string {
  const lookup = lookupAgentDisplay(agentId);
  if (kind === 'ongoing') {
    return `${factionColorClasses(lookup.faction).text} text-shadow-glow-cyan`;
  }
  return 'text-hud-text';
}

// ── 自动滚动驱动 ──────────────────────────────────────────────────────────

watch(
  () => displayItems.value.length,
  () => {
    if (!autoScrollEnabled.value) return;
    nextTick(() => scrollToBottom(true));
  },
);

// 监控所有 ongoing 的文本长度变化 (打字机吐字时也要保持锁底)
watch(
  () =>
    Object.values(debateStore.ongoing)
      .map((o) => o.text_buffer.length)
      .reduce((a, b) => a + b, 0),
  () => {
    if (!autoScrollEnabled.value) return;
    nextTick(() => scrollToBottom(false));
  },
);

// ── 生命周期 ──────────────────────────────────────────────────────────────

onMounted(() => {
  nextTick(() => scrollToBottom(false));
});

onBeforeUnmount(() => {
  // useInterruptShake 内部 onBeforeUnmount 已自清理
});
</script>

<style scoped>
.debate-stream__scroll {
  scrollbar-gutter: stable;
}
</style>