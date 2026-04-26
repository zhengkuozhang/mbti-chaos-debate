<!--
  ============================================================================
  TopicBanner.vue · 议题横幅
  ----------------------------------------------------------------------------
  - 顶部带 anim-topic-slide-in/out 切换动画
  - 议题切换时把当前 topic_id 存到 currentBanner,旧的淡出
  - 空议题时占位"等待议题注入"
  - 右侧栈深度小标 LV n / m
  ============================================================================
-->

<template>
  <div
    class="topic-banner relative flex shrink-0 items-stretch border-b border-white/5 bg-zinc-950/40 backdrop-blur-md"
  >
    <transition
      mode="out-in"
      enter-active-class="anim-topic-slide-in"
      leave-active-class="anim-topic-slide-out"
    >
      <div
        v-if="currentTopic"
        :key="currentTopic.topic_id"
        class="topic-banner__content flex w-full items-center justify-between gap-6 px-6 py-3"
      >
        <!-- 左:LV 标 + 标题 + 描述 -->
        <div class="flex min-w-0 flex-1 flex-col gap-1">
          <div class="flex items-center gap-3">
            <span
              class="rounded border border-faction-nt/40 bg-faction-nt/10 px-2 py-0.5 font-mono text-2xs uppercase tracking-hud-wide text-faction-nt"
            >
              LV {{ stackDepth }}
            </span>
            <span
              class="font-mono text-2xs uppercase tracking-hud-wide text-hud-muted"
            >
              CURRENT TOPIC
            </span>
            <span class="font-mono text-2xs text-hud-muted/70">
              · {{ pushedAtLabel }}
            </span>
          </div>
          <div
            class="truncate font-medium text-base text-hud-text text-shadow-glow-cyan"
          >
            {{ currentTopic.title }}
          </div>
          <div
            v-if="currentTopic.description"
            class="line-clamp-2 font-mono text-2xs text-hud-muted"
          >
            {{ currentTopic.description }}
          </div>
        </div>

        <!-- 右:topic id 短哈希 -->
        <div
          class="hidden shrink-0 items-center gap-2 font-mono text-2xs text-hud-muted/60 lg:flex"
        >
          <span class="uppercase tracking-hud-wide">REF</span>
          <span class="rounded border border-white/5 bg-white/[0.02] px-1.5 py-0.5 text-hud-muted">
            {{ topicShortId }}
          </span>
        </div>
      </div>

      <!-- 空议题占位 -->
      <div
        v-else
        key="empty"
        class="flex w-full items-center justify-center gap-3 px-6 py-3"
      >
        <span
          class="font-mono text-2xs uppercase tracking-hud-wider text-hud-muted/70"
        >
          NO TOPIC
        </span>
        <span class="font-mono text-sm text-hud-muted">
          等待议题注入 (右侧控制台)
        </span>
      </div>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useDebateStore } from '@/stores/debate';

const debateStore = useDebateStore();

const currentTopic = computed(() => debateStore.topicCurrent);
const stackDepth = computed<number>(() => debateStore.topicDepth);

const topicShortId = computed<string>(() => {
  const t = currentTopic.value;
  if (!t || !t.topic_id) return '—';
  const id = t.topic_id;
  return id.length > 10 ? id.slice(0, 10) : id;
});

const pushedAtLabel = computed<string>(() => {
  const t = currentTopic.value;
  if (!t) return '';
  const ts = t.pushed_at_wall_unix;
  if (!ts || ts <= 0) return '';
  const d = new Date(ts * 1000);
  const hh = d.getHours().toString().padStart(2, '0');
  const mm = d.getMinutes().toString().padStart(2, '0');
  return `${hh}:${mm}`;
});
</script>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>