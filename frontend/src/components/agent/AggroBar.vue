<!--
  ============================================================================
  AggroBar.vue · Aggro 进度条原子组件

  视觉:
    [████████████░░░░░░░░] 67.4 / 100
                         ↑ interrupt threshold (红色刻度线)

  态:
    - 渐变色: 由 faction 决定 (NT 青 / NF 紫 / SJ 琥 / SP 绿)
    - burning (Aggro >= 70): 叠加 anim-aggro-stream 流光
    - 触达 interrupt_threshold: 闪过红色刻度线
  ============================================================================
-->

<template>
  <div class="aggro-bar flex items-center gap-2">
    <div
      class="relative h-1.5 flex-1 overflow-hidden rounded-full bg-zinc-800/60"
      role="progressbar"
      :aria-valuenow="value"
      :aria-valuemin="min"
      :aria-valuemax="max"
    >
      <!-- 阈值刻度线 -->
      <div
        v-if="thresholdPct > 0 && thresholdPct < 100"
        class="absolute top-0 h-full w-px bg-cyber-red/70"
        :style="{ left: `${thresholdPct}%` }"
        aria-hidden="true"
      ></div>

      <!-- 进度填充 -->
      <div
        class="aggro-bar__fill absolute left-0 top-0 h-full rounded-full bg-gradient-to-r transition-[width] duration-280 ease-out"
        :class="[fillGradientClass, burning ? 'shadow-[0_0_8px_0_currentColor]' : '']"
        :style="{ width: `${fillPct}%` }"
      >
        <!-- burning 流光叠加 -->
        <div
          v-if="burning"
          class="anim-aggro-stream absolute inset-0 rounded-full"
          aria-hidden="true"
        ></div>
      </div>

      <!-- burning 时整条呼吸 -->
      <div
        v-if="burning"
        class="absolute inset-0 rounded-full ring-1 ring-current animate-aggro-pulse"
        :class="ringColorClass"
        aria-hidden="true"
      ></div>
    </div>

    <!-- 数值标签 -->
    <div class="hud-mono-num min-w-[44px] text-right font-mono text-2xs tabular-nums">
      <span :class="numberColorClass">{{ valueLabel }}</span>
      <span class="text-hud-muted/60">/{{ Math.round(max) }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

import { factionColorClasses } from '@/utils/colorMap';
import type { Faction } from '@/types/protocol';

interface Props {
  value: number;
  min: number;
  max: number;
  faction: Faction;
  /** 后端的抢话阈值 · 通常 85 */
  interruptThreshold: number;
  /** value >= 70 时为 true · 由父组件计算并传入 */
  burning: boolean;
}

const props = defineProps<Props>();

// ── 计算填充百分比 (clamp 0..100) ────────────────────────────────────────

const fillPct = computed<number>(() => {
  const range = props.max - props.min;
  if (range <= 0) return 0;
  const raw = ((props.value - props.min) / range) * 100;
  return Math.max(0, Math.min(100, raw));
});

const thresholdPct = computed<number>(() => {
  const range = props.max - props.min;
  if (range <= 0) return 0;
  const raw = ((props.interruptThreshold - props.min) / range) * 100;
  return Math.max(0, Math.min(100, raw));
});

const valueLabel = computed<string>(() => props.value.toFixed(1));

// ── 颜色类 ────────────────────────────────────────────────────────────────

const factionClasses = computed(() => factionColorClasses(props.faction));

const fillGradientClass = computed<string>(() => {
  const c = factionClasses.value;
  return `${c.gradientFrom} ${c.gradientTo}`;
});

const ringColorClass = computed<string>(() => factionClasses.value.text);

const numberColorClass = computed<string>(() => {
  if (props.value >= props.interruptThreshold) {
    return 'text-cyber-red text-shadow-glow-red';
  }
  if (props.burning) {
    return factionClasses.value.text;
  }
  return 'text-hud-text';
});
</script>