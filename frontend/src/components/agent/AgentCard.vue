<!--
  ============================================================================
  AgentCard.vue · 单个 Agent 监控卡片

  显示:
    - 顶部:MBTI 徽章 + display_name + archetype
    - 中部:Aggro 进度条 (AggroBar)
    - 底部:FSM 指示器 + 是否持麦标识 + 是否在发言

  视觉态:
    - is_microphone_holder = true → ring + handoff-glow + 强阴影
    - aggro >= 70 (burning)        → AggroBar 流光 + 顶部脉动徽章
    - has ongoing speech           → 底部出现 "speaking…" 微动画
  ============================================================================
-->

<template>
  <div
    class="agent-card hud-panel relative flex flex-col gap-2 px-3 py-2.5 transition-all duration-180"
    :class="cardStateClass"
    :style="cardCustomStyle"
    role="article"
    :aria-label="`agent-${agent.descriptor.agent_id}`"
  >
    <!-- 顶部:MBTI 徽章 + 名字 -->
    <div class="flex items-center gap-2.5">
      <!-- MBTI 徽章 -->
      <div
        class="agent-card__badge flex h-7 min-w-[42px] items-center justify-center rounded border px-1.5 font-mono text-2xs font-medium tracking-hud-wide"
        :class="[badgeClass, isBurning ? 'animate-aggro-pulse' : '']"
      >
        {{ agent.descriptor.mbti }}
      </div>

      <!-- 名字 + archetype -->
      <div class="flex min-w-0 flex-1 flex-col leading-tight">
        <span class="truncate font-medium text-sm text-hud-text">
          {{ agent.descriptor.display_name }}
        </span>
        <span class="truncate font-mono text-2xs text-hud-muted">
          {{ agent.descriptor.archetype }}
        </span>
      </div>

      <!-- 持麦徽标 -->
      <div
        v-if="isHolder"
        class="agent-card__holder flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-2xs uppercase tracking-hud-wide"
        :class="holderBadgeClass"
        :title="`持麦 · ${tokenAge}s`"
      >
        <span class="hud-dot bg-current"></span>
        <span>MIC</span>
      </div>
    </div>

    <!-- 中部:Aggro 进度条 -->
    <AggroBar
      :value="agent.aggro.current"
      :min="agent.aggro.min"
      :max="agent.aggro.max"
      :faction="agent.descriptor.faction"
      :interrupt-threshold="interruptThresholdValue"
      :burning="isBurning"
    />

    <!-- 底部:FSM + speaking 指示 + λ -->
    <div class="flex items-center justify-between">
      <FsmIndicator
        :state="agent.fsm.name"
        :age-seconds="agent.fsm.age_seconds"
        :faction="agent.descriptor.faction"
      />

      <div class="flex items-center gap-3 font-mono text-2xs text-hud-muted">
        <!-- λ -->
        <span :title="`衰减系数 λ`">
          λ
          <span class="hud-mono-num text-hud-text/80">
            {{ agent.descriptor.decay_lambda.toFixed(2) }}
          </span>
        </span>

        <!-- speaking 微指示 -->
        <span
          v-if="hasOngoing"
          class="flex items-center gap-1 text-faction-nt"
          :class="factionTextClass"
        >
          <span class="hud-dot bg-current animate-fsm-glow"></span>
          <span class="text-2xs uppercase tracking-hud-wide">SPEAKING</span>
        </span>

        <!-- 唯一权限标识:Chroma 检索 / Summarizer -->
        <span
          v-if="agent.descriptor.has_chroma_access"
          class="rounded border border-faction-sj/50 bg-faction-sj/10 px-1 py-0.5 text-2xs uppercase tracking-hud-wide text-faction-sj"
          title="拥有 ChromaDB 检索权"
        >
          REC
        </span>
        <span
          v-if="agent.descriptor.is_summarizer"
          class="rounded border border-faction-sp/50 bg-faction-sp/10 px-1 py-0.5 text-2xs uppercase tracking-hud-wide text-faction-sp"
          title="承担控场身份"
        >
          CTRL
        </span>
      </div>
    </div>

    <!-- 持麦时的接力光晕 (一次性) -->
<div
  v-if="showHandoffGlow"
  class="anim-handoff-glow absolute inset-0 rounded-xl pointer-events-none"
  :style="handoffGlowStyle"
></div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';

import AggroBar from '@/components/agent/AggroBar.vue';
import FsmIndicator from '@/components/agent/FsmIndicator.vue';
import { useAgentsStore } from '@/stores/agents';
import { useDebateStore } from '@/stores/debate';
import {
  factionColorClasses,
  factionGlowColor,
} from '@/utils/colorMap';
import type { AgentStateView } from '@/types/protocol';

interface Props {
  agent: AgentStateView;
}
const props = defineProps<Props>();

const agentsStore = useAgentsStore();
const debateStore = useDebateStore();

// ── 派生状态 ──────────────────────────────────────────────────────────────

const isHolder = computed<boolean>(() => props.agent.is_microphone_holder);

const isBurning = computed<boolean>(() => props.agent.aggro.current >= 70);

const tokenAge = computed<string>(() =>
  agentsStore.arbitrator.current_token_age_sec.toFixed(1),
);

const hasOngoing = computed<boolean>(
  () => props.agent.descriptor.agent_id in debateStore.ongoing,
);

const factionClasses = computed(() =>
  factionColorClasses(props.agent.descriptor.faction),
);

const glowColor = computed<string>(() =>
  factionGlowColor(props.agent.descriptor.faction),
);

const handoffGlowStyle = computed<Record<string, string>>(() => ({
  '--handoff-color': glowColor.value,
}));

// ── 卡片整体视觉态 ────────────────────────────────────────────────────────

const cardStateClass = computed<string[]>(() => {
  const classes: string[] = [];
  if (isHolder.value) {
    classes.push('ring-1', factionClasses.value.border, factionClasses.value.glowShadow);
  } else {
    classes.push('hover:border-white/10');
  }
  return classes;
});

const cardCustomStyle = computed<Record<string, string>>(() => {
  if (!isHolder.value) return {};
  return {
    boxShadow: `0 0 0 1px ${glowColor.value}, 0 12px 30px -12px ${glowColor.value}`,
  };
});

// ── 徽章 / 持麦 / 文本色 ─────────────────────────────────────────────────

const badgeClass = computed<string>(() => {
  const c = factionClasses.value;
  return `${c.border} ${c.text} ${c.bgSoft}`;
});

const holderBadgeClass = computed<string>(() => {
  const c = factionClasses.value;
  return `${c.border} ${c.text} ${c.bgSoft}`;
});

const factionTextClass = computed<string>(() => factionClasses.value.text);

// ── interrupt threshold (从 aggro 桶派生) ─────────────────────────────────

const interruptThresholdValue = computed<number>(() => {
  // above_interrupt_threshold 为派生字段;用 normalized × max 反推阈值不准。
  // 这里取后端默认 85 (与 settings.AGGRO_INTERRUPT_THRESHOLD 对齐)。
  // 若未来后端把阈值放进 AggroBucketView,改成 props.agent.aggro.threshold 即可。
  return 85;
});

// ── 接力光晕触发 (持麦切换时一次性闪烁) ──────────────────────────────────

const showHandoffGlow = ref<boolean>(false);
let glowTimer: number | null = null;

watch(
  () => isHolder.value,
  (now, prev) => {
    if (now && !prev) {
      showHandoffGlow.value = true;
      if (glowTimer !== null) {
        window.clearTimeout(glowTimer);
      }
      glowTimer = window.setTimeout(() => {
        showHandoffGlow.value = false;
        glowTimer = null;
      }, 900);
    }
  },
);
</script>