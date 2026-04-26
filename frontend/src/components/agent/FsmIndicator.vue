<!--
  ============================================================================
  FsmIndicator.vue · FSM 状态指示器原子组件

  6 个状态对应 6 种视觉:
    IDLE        灰点 · 静止
    LISTENING   小点 · 微弱呼吸
    RETRIEVING  双圈 · 正反向旋转 (ISFJ 检索专属)
    COMPUTING   单圈 · 缓慢顺转
    SPEAKING    实心 · 流光填充
    UPDATING    闪烁
  ============================================================================
-->

<template>
  <div
    class="fsm-indicator flex items-center gap-1.5"
    :title="title"
  >
    <!-- 视觉部分 (按状态切换) -->
    <div class="relative h-3.5 w-3.5">
      <!-- IDLE -->
      <span
        v-if="state === 'IDLE'"
        class="absolute inset-1 rounded-full bg-zinc-600"
      ></span>

      <!-- LISTENING -->
      <span
        v-else-if="state === 'LISTENING'"
        class="absolute inset-1 rounded-full bg-current animate-fsm-glow"
        :class="factionTextClass"
      ></span>

      <!-- RETRIEVING (双圈) -->
      <template v-else-if="state === 'RETRIEVING'">
        <span
          class="absolute inset-0 rounded-full border border-current opacity-60 anim-rot-cw"
          :class="factionTextClass"
        ></span>
        <span
          class="absolute inset-1 rounded-full border border-current opacity-90 anim-rot-ccw"
          :class="factionTextClass"
        ></span>
      </template>

      <!-- COMPUTING (单圈呼吸) -->
      <template v-else-if="state === 'COMPUTING'">
        <span
          class="absolute inset-0 rounded-full border border-current opacity-50 animate-fsm-glow"
          :class="factionTextClass"
        ></span>
        <span
          class="absolute inset-1 rounded-full bg-current opacity-90"
          :class="factionTextClass"
        ></span>
      </template>

      <!-- SPEAKING (实心 + 流光) -->
      <template v-else-if="state === 'SPEAKING'">
        <span
          class="absolute inset-0 rounded-full bg-current"
          :class="factionTextClass"
        ></span>
        <span
          class="absolute inset-0 rounded-full anim-aggro-stream"
          aria-hidden="true"
        ></span>
      </template>

      <!-- UPDATING (闪烁红) -->
      <span
        v-else-if="state === 'UPDATING'"
        class="absolute inset-1 rounded-full bg-cyber-red animate-aggro-pulse"
      ></span>

      <!-- 兜底 -->
      <span
        v-else
        class="absolute inset-1 rounded-full bg-zinc-500"
      ></span>
    </div>

    <!-- 状态文字 -->
    <span class="font-mono text-2xs uppercase tracking-hud-wide" :class="labelColorClass">
      {{ stateLabel }}
    </span>

    <!-- age (秒) -->
    <span
      v-if="showAge"
      class="font-mono text-2xs tabular-nums text-hud-muted"
    >
      {{ ageSeconds.toFixed(1) }}s
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

import { factionColorClasses } from '@/utils/colorMap';
import type { Faction, FSMStateName } from '@/types/protocol';

interface Props {
  state: FSMStateName;
  ageSeconds: number;
  faction: Faction;
  /** 是否显示 age (默认 true,但极小卡片可关闭) */
  showAge?: boolean;
}

const props = withDefaults(defineProps<Props>(), { showAge: true });

const factionTextClass = computed<string>(
  () => factionColorClasses(props.faction).text,
);

const labelColorClass = computed<string>(() => {
  switch (props.state) {
    case 'IDLE':
      return 'text-hud-muted/70';
    case 'LISTENING':
    case 'COMPUTING':
    case 'RETRIEVING':
      return factionTextClass.value;
    case 'SPEAKING':
      return `${factionTextClass.value} text-shadow-glow-cyan`;
    case 'UPDATING':
      return 'text-cyber-red';
    default:
      return 'text-hud-muted';
  }
});

const stateLabel = computed<string>(() => {
  switch (props.state) {
    case 'IDLE':
      return 'IDLE';
    case 'LISTENING':
      return 'LISTEN';
    case 'RETRIEVING':
      return 'RETR';
    case 'COMPUTING':
      return 'COMP';
    case 'SPEAKING':
      return 'SPEAK';
    case 'UPDATING':
      return 'UPD';
    default:
      return '???';
  }
});

const title = computed<string>(
  () => `FSM=${props.state} · age=${props.ageSeconds.toFixed(2)}s`,
);
</script>