<!--
  ============================================================================
  HudShell.vue · HUD 全屏三栏壳

  布局:
    ┌──────────────────────────────────────────────────────────────────┐
    │ HeaderBar                                              (56px)    │
    ├───────────────┬─────────────────────────────┬────────────────────┤
    │               │                             │                    │
    │   #monitor    │          #center            │       #side        │
    │     (25%)     │           (50%)             │        (25%)       │
    │               │                             │                    │
    │  8 Agent 卡片 │      辩论流主舞台            │   议题栈+控制台    │
    │               │                             │                    │
    ├───────────────┴─────────────────────────────┴────────────────────┤
    │ StatusFooter                                            (28px)   │
    └──────────────────────────────────────────────────────────────────┘

  - 全屏锁定 h-screen overflow-hidden
  - 主体三列固定百分比 grid-cols-[25%_50%_25%]
  - 单一职责: 仅做布局骨架,所有内容由 slot 注入
  ============================================================================
-->

<template>
  <div class="hud-shell flex h-screen w-screen flex-col overflow-hidden bg-hud-bg text-hud-text">
    <!-- 顶栏 -->
    <header class="hud-shell__header relative shrink-0">
      <slot name="header">
        <HeaderBar />
      </slot>
    </header>

    <!-- 主体三栏 -->
    <main
      class="hud-shell__main grid min-h-0 flex-1 grid-cols-[25%_50%_25%] gap-0 border-y border-white/5"
    >
      <!-- 左:监控舱 -->
      <section
        class="hud-shell__monitor relative flex min-h-0 flex-col overflow-hidden border-r border-white/5"
        aria-label="agent-monitor"
      >
        <div class="hud-shell__column-label">
          <span class="hud-column-label">监控舱 · MONITOR</span>
        </div>
        <div class="flex-1 min-h-0 overflow-y-auto px-3 py-2">
          <slot name="monitor" />
        </div>
      </section>

      <!-- 中:辩论流 -->
      <section
        class="hud-shell__center relative flex min-h-0 flex-col overflow-hidden"
        aria-label="debate-stream"
      >
        <div class="hud-shell__column-label">
          <span class="hud-column-label">辩论流 · DEBATE STREAM</span>
        </div>
        <div class="relative flex-1 min-h-0 overflow-hidden">
          <slot name="center" />
        </div>
      </section>

      <!-- 右:议题与控制台 -->
      <section
        class="hud-shell__side relative flex min-h-0 flex-col overflow-hidden border-l border-white/5"
        aria-label="topic-console"
      >
        <div class="hud-shell__column-label">
          <span class="hud-column-label">仲裁台 · ARBITER</span>
        </div>
        <div class="flex-1 min-h-0 overflow-y-auto px-3 py-2">
          <slot name="side" />
        </div>
      </section>
    </main>

    <!-- 底部状态条 -->
    <footer
      class="hud-shell__footer flex h-7 shrink-0 items-center justify-between border-t border-white/5 bg-zinc-950/60 px-4 font-mono text-2xs uppercase tracking-hud-wide text-hud-muted"
    >
      <slot name="footer">
        <div>
          MBTI · CHAOS · DEBATE ·
          <span class="text-hud-text/60">{{ appVersion }}</span>
        </div>
        <div class="flex items-center gap-3">
          <span>session: {{ sessionIdShort }}</span>
          <span class="text-hud-muted/50">|</span>
          <span>{{ statusLabel }}</span>
        </div>
      </slot>
    </footer>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import HeaderBar from '@/components/layout/HeaderBar.vue';
import { useDebateStore } from '@/stores/debate';

const debateStore = useDebateStore();

const appVersion = computed<string>(() => debateStore.backendVersion ?? 'v?.?.?');

const sessionIdShort = computed<string>(() => {
  const id = debateStore.sessionId;
  if (!id) return '—';
  return id.length > 14 ? id.slice(0, 14) + '…' : id;
});

const statusLabel = computed<string>(() => {
  const s = debateStore.sessionStatus;
  switch (s) {
    case 'INITIALIZING':
      return '初始化中';
    case 'READY':
      return '待命';
    case 'RUNNING':
      return '辩论中';
    case 'PAUSED':
      return '已暂停';
    case 'SHUTTING_DOWN':
      return '关闭中';
    default:
      return s;
  }
});
</script>

<style scoped>
.hud-shell__column-label {
  @apply flex h-8 shrink-0 items-center border-b border-white/5 bg-zinc-950/40 px-3;
}
</style>