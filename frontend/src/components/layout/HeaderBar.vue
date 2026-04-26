<!--
  ============================================================================
  HeaderBar.vue · HUD 顶栏

  内容三段:
    左:  logo 圆点 + 应用名 + 版本号
    中:  Session 状态 + 议题深度 + 全场 Aggro 均值
    右:  WS 连接状态指示灯 + 时钟

  特效:
    - 顶部 1px 横向扫描线 (anim-scan-line · 6.5s 周期)
    - WS 灯呼吸 (anim-ws-breath)
  ============================================================================
-->

<template>
  <div
    class="header-bar relative flex h-14 w-full items-center justify-between border-b border-white/5 bg-zinc-950/80 px-5 backdrop-blur-xl"
  >
    <!-- 顶部扫描线 -->
    <div class="anim-scan-line"></div>

    <!-- 左:Logo + 应用名 -->
    <div class="flex items-center gap-3">
      <div
        class="logo-pulse h-2.5 w-2.5 rounded-full bg-faction-nt shadow-glow-cyan"
        aria-hidden="true"
      ></div>
      <div class="flex flex-col leading-none">
        <span
          class="font-mono text-2xs uppercase tracking-hud-wider text-hud-muted"
        >
          MBTI · CHAOS
        </span>
        <span class="mt-0.5 font-mono text-sm font-medium text-hud-text">
          DEBATE SANDBOX
        </span>
      </div>
      <span
        class="ml-2 rounded border border-white/5 bg-white/[0.02] px-1.5 py-0.5 font-mono text-2xs text-hud-muted"
      >
        {{ versionLabel }}
      </span>
    </div>

    <!-- 中:Session 状态 + 议题深度 + 全场 Aggro 均值 -->
    <div class="flex items-center gap-6">
      <div class="flex items-center gap-2">
        <span class="hud-dot" :class="sessionDotClass"></span>
        <span class="font-mono text-2xs uppercase tracking-hud-wide text-hud-muted">
          {{ sessionLabel }}
        </span>
      </div>

      <span class="hud-divider-v" aria-hidden="true"></span>

      <div class="flex items-center gap-2">
        <span class="font-mono text-2xs uppercase tracking-hud-wide text-hud-muted">
          议题层
        </span>
        <span class="hud-mono-num text-sm text-hud-text">
          {{ topicDepth }}
        </span>
      </div>

      <span class="hud-divider-v" aria-hidden="true"></span>

      <div class="flex items-center gap-2">
        <span class="font-mono text-2xs uppercase tracking-hud-wide text-hud-muted">
          全场 Aggro
        </span>
        <span
          class="hud-mono-num text-sm tabular-nums"
          :class="aggroAvgColorClass"
        >
          {{ aggroAvgFormatted }}
        </span>
      </div>

      <span class="hud-divider-v" aria-hidden="true"></span>

      <div class="flex items-center gap-2">
        <span class="font-mono text-2xs uppercase tracking-hud-wide text-hud-muted">
          持麦
        </span>
        <span class="hud-mono-num text-sm text-hud-text">
          {{ holderLabel }}
        </span>
      </div>
    </div>

    <!-- 右:WS 状态 + 时钟 -->
    <div class="flex items-center gap-4">
      <div class="flex items-center gap-2">
        <span
          class="hud-dot anim-ws-breath"
          :class="wsDotClass"
          :title="wsStateLabel"
        ></span>
        <span class="font-mono text-2xs uppercase tracking-hud-wide text-hud-muted">
          {{ wsStateLabel }}
        </span>
      </div>

      <span class="hud-divider-v" aria-hidden="true"></span>

      <div
        class="hud-mono-num font-mono text-sm tabular-nums text-hud-text"
        aria-label="clock"
      >
        {{ clockText }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import { useAgentsStore } from '@/stores/agents';
import { useDebateStore } from '@/stores/debate';

const debateStore = useDebateStore();
const agentsStore = useAgentsStore();

// ── 版本标签 ──────────────────────────────────────────────────────────────

const versionLabel = computed<string>(() => {
  const v = debateStore.backendVersion;
  return v ? `v${v}` : 'v?';
});

// ── Session 状态 ──────────────────────────────────────────────────────────

const sessionLabel = computed<string>(() => {
  const s = debateStore.sessionStatus;
  switch (s) {
    case 'INITIALIZING':
      return '初始化';
    case 'READY':
      return '待命';
    case 'RUNNING':
      return '运行';
    case 'PAUSED':
      return '暂停';
    case 'SHUTTING_DOWN':
      return '关闭中';
    default:
      return s;
  }
});

const sessionDotClass = computed<string>(() => {
  const s = debateStore.sessionStatus;
  switch (s) {
    case 'RUNNING':
      return 'bg-emerald-500 shadow-glow-emerald';
    case 'PAUSED':
      return 'bg-amber-500 shadow-glow-amber';
    case 'INITIALIZING':
    case 'READY':
      return 'bg-cyan-500 shadow-glow-cyan';
    case 'SHUTTING_DOWN':
      return 'bg-cyber-red shadow-glow-red';
    default:
      return 'bg-zinc-500';
  }
});

// ── 议题深度 ──────────────────────────────────────────────────────────────

const topicDepth = computed<number>(() => debateStore.topicDepth);

// ── 全场 Aggro 均值 ───────────────────────────────────────────────────────

const aggroAvgFormatted = computed<string>(() => {
  const list = agentsStore.asList;
  if (list.length === 0) return '—';
  const sum = list.reduce((acc, a) => acc + a.aggro.current, 0);
  return (sum / list.length).toFixed(1);
});

const aggroAvgColorClass = computed<string>(() => {
  const list = agentsStore.asList;
  if (list.length === 0) return 'text-hud-muted';
  const avg = list.reduce((acc, a) => acc + a.aggro.current, 0) / list.length;
  if (avg >= 70) return 'text-cyber-red text-shadow-glow-red';
  if (avg >= 50) return 'text-faction-sj';
  return 'text-hud-text';
});

// ── 当前持麦 ──────────────────────────────────────────────────────────────

const holderLabel = computed<string>(() => {
  const id = agentsStore.currentHolderId;
  if (!id) return '—';
  return id.length > 16 ? id.slice(0, 16) + '…' : id;
});

// ── WS 状态 ───────────────────────────────────────────────────────────────

const wsStateLabel = computed<string>(() => {
  switch (debateStore.wsState) {
    case 'idle':
      return 'IDLE';
    case 'connecting':
      return '连接中';
    case 'open':
      return '已连接';
    case 'reconnecting':
      return '重连中';
    case 'closed':
      return '已断开';
    case 'failed':
      return '已失败';
    default:
      return debateStore.wsState;
  }
});

const wsDotClass = computed<string>(() => {
  switch (debateStore.wsState) {
    case 'open':
      return 'bg-emerald-500 shadow-glow-emerald';
    case 'connecting':
    case 'reconnecting':
      return 'bg-amber-500 shadow-glow-amber';
    case 'failed':
      return 'bg-cyber-red shadow-glow-red';
    case 'closed':
      return 'bg-zinc-500';
    case 'idle':
    default:
      return 'bg-zinc-600';
  }
});

// ── 时钟 ──────────────────────────────────────────────────────────────────

const clockText = ref<string>('--:--:--');
let clockTimer: number | null = null;

function tickClock(): void {
  const d = new Date();
  const hh = d.getHours().toString().padStart(2, '0');
  const mm = d.getMinutes().toString().padStart(2, '0');
  const ss = d.getSeconds().toString().padStart(2, '0');
  clockText.value = `${hh}:${mm}:${ss}`;
}

onMounted(() => {
  tickClock();
  clockTimer = window.setInterval(tickClock, 1000);
});

onBeforeUnmount(() => {
  if (clockTimer !== null) {
    window.clearInterval(clockTimer);
    clockTimer = null;
  }
});
</script>

<style scoped>
.logo-pulse {
  animation: logo-breath 2.4s ease-in-out infinite;
}
@keyframes logo-breath {
  0%,
  100% {
    opacity: 0.8;
    transform: scale(0.96);
  }
  50% {
    opacity: 1;
    transform: scale(1);
  }
}
</style>