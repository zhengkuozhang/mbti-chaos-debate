<!--
  ============================================================================
  InterruptOverlay.vue · 打断瞬间叠加层
  ----------------------------------------------------------------------------
  - 监听 debateStore.lastInterruptDetail · 显示约 1.2 秒
  - 中央字幕: "INTERRUPTER ⚡ INTERRUPTED"
  - interrupter 名字: anim-digital-glitch (数字毛刺)
  - interrupted 名字: 删除线 + 灰化
  - pointer-events: none (绝不阻塞下层交互)
  ============================================================================
-->

<template>
  <transition
    enter-active-class="transition-opacity duration-150 ease-out"
    leave-active-class="transition-opacity duration-300 ease-in"
    enter-from-class="opacity-0"
    leave-to-class="opacity-0"
  >
    <div
      v-if="visible"
      class="interrupt-overlay pointer-events-none absolute inset-0 z-30 flex items-center justify-center"
      role="status"
      aria-live="polite"
    >
      <!-- 背景闪光 -->
      <div
        class="absolute inset-0 bg-cyber-red/[0.06]"
        aria-hidden="true"
      ></div>

      <!-- 字幕 -->
      <div class="relative flex flex-col items-center gap-3">
        <div
          class="font-mono text-2xs uppercase tracking-hud-wider text-cyber-red text-shadow-glow-red"
        >
          ⚡ INTERRUPT ⚡
        </div>

        <div class="flex items-center gap-4 font-mono text-lg">
          <span
            class="anim-digital-glitch text-cyber-red text-shadow-glow-red"
            :class="interrupterFactionClass"
          >
            {{ interrupterDisplay }}
          </span>
          <span class="text-hud-muted/70">→</span>
          <span class="text-hud-muted line-through opacity-70">
            {{ interruptedDisplay }}
          </span>
        </div>

        <div
          class="mt-1 font-mono text-2xs uppercase tracking-hud-wide text-hud-muted/70"
        >
          麦克风强制夺取
        </div>
      </div>
    </div>
  </transition>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';

import { useAgentsStore } from '@/stores/agents';
import { useDebateStore } from '@/stores/debate';
import { factionColorClasses } from '@/utils/colorMap';
import type { Faction } from '@/types/protocol';

const debateStore = useDebateStore();
const agentsStore = useAgentsStore();

const VISIBLE_DURATION_MS = 1_200;

const visible = ref<boolean>(false);
const interrupterId = ref<string>('');
const interruptedId = ref<string>('');

let hideTimer: number | null = null;

function cancelHideTimer(): void {
  if (hideTimer !== null) {
    window.clearTimeout(hideTimer);
    hideTimer = null;
  }
}

watch(
  () => debateStore.lastInterruptAt,
  (newVal: number, oldVal: number | undefined) => {
    if (!newVal || newVal === oldVal) return;
    const detail = debateStore.lastInterruptDetail;
    if (!detail) return;

    interrupterId.value = detail.interrupter;
    interruptedId.value = detail.interrupted;
    visible.value = true;

    cancelHideTimer();
    hideTimer = window.setTimeout(() => {
      visible.value = false;
      hideTimer = null;
    }, VISIBLE_DURATION_MS);
  },
);

function lookup(agentId: string): { display: string; faction: Faction } {
  const a = agentsStore.byId[agentId];
  if (a) {
    return {
      display: a.descriptor.display_name,
      faction: a.descriptor.faction,
    };
  }
  return { display: agentId || '?', faction: 'NT' };
}

const interrupterDisplay = computed<string>(() => lookup(interrupterId.value).display);
const interruptedDisplay = computed<string>(() => lookup(interruptedId.value).display);

const interrupterFactionClass = computed<string>(
  () => factionColorClasses(lookup(interrupterId.value).faction).text,
);

onBeforeUnmount(() => {
  cancelHideTimer();
});
</script>