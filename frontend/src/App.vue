<!--
  ============================================================================
  App.vue · 项目终装根组件
  ----------------------------------------------------------------------------
  启动时序:
    1. useSnapshot()     - 启动 REST 全量水合
    2. useWebSocket()    - 启动 WS 连接 + 增量同步
    3. 渲染 HudShell + 三栏内容

  Phase 切换:
    - loading / retrying  → 加载罩
    - degraded            → 错误屏 + 重试按钮
    - ready               → 完整 HUD

  右栏 #side 控制台:
    - 议题注入表单
    - PAUSE / RESUME / POP_TOPIC / FORCE_INTERRUPT 按钮
    - Arbitrator / Watchdog / Semaphore 状态摘要

  左栏 #monitor:
    - 8 个 AgentCard,按 NT/NF/SJ/SP 阵营分组
  ============================================================================
-->

<template>
  <div class="app-root h-screen w-screen bg-hud-bg text-hud-text">
    <!-- 加载罩 / 错误屏 / 主 HUD 三选一 -->
    <template v-if="snapshotPhase === 'loading' || snapshotPhase === 'idle'">
      <BootSplash status="loading" :message="loadingMessage" />
    </template>

    <template v-else-if="snapshotPhase === 'retrying'">
      <BootSplash status="retrying" :message="retryingMessage" />
    </template>

    <template v-else-if="snapshotPhase === 'degraded'">
      <BootSplash status="degraded" :message="degradedMessage">
        <template #actions>
          <button class="hud-btn hud-btn--primary" @click="onRetry" type="button">
            重试水合
          </button>
        </template>
      </BootSplash>
    </template>

    <template v-else>
      <!-- 主 HUD -->
      <HudShell>
        <!-- 左:监控舱 (按阵营分组的 AgentCard) -->
        <template #monitor>
          <div class="flex flex-col gap-4">
            <template v-for="group in groupedAgents" :key="group.faction">
              <div class="flex flex-col gap-2">
                <div class="flex items-center justify-between">
                  <span
                    class="font-mono text-2xs uppercase tracking-hud-wide"
                    :class="factionTextClass(group.faction)"
                  >
                    {{ group.label }}
                  </span>
                  <span class="font-mono text-2xs text-hud-muted/70">
                    {{ group.agents.length }}
                  </span>
                </div>
                <AgentCard
                  v-for="a in group.agents"
                  :key="a.descriptor.agent_id"
                  :agent="a"
                />
              </div>
            </template>

            <div
              v-if="agentsList.length === 0"
              class="rounded-lg border border-white/5 bg-white/[0.02] p-4 text-center font-mono text-2xs text-hud-muted"
            >
              暂无 Agent 数据
            </div>
          </div>
        </template>

        <!-- 中:辩论流主舞台 -->
        <template #center>
          <DebateStream />
        </template>

        <!-- 右:控制台 -->
        <template #side>
          <div class="flex flex-col gap-4">
            <!-- 议题注入表单 -->
            <section class="hud-panel--bare flex flex-col gap-2 p-3">
              <h3
                class="font-mono text-2xs uppercase tracking-hud-wider text-hud-muted"
              >
                议题注入
              </h3>
              <input
                v-model="topicTitleInput"
                type="text"
                placeholder="议题标题"
                maxlength="80"
                class="rounded border border-white/10 bg-zinc-900/40 px-2 py-1.5 font-mono text-sm text-hud-text outline-none focus:border-faction-nt/60"
              />
              <textarea
                v-model="topicDescInput"
                placeholder="议题描述 (选填)"
                rows="3"
                maxlength="400"
                class="rounded border border-white/10 bg-zinc-900/40 px-2 py-1.5 font-mono text-2xs text-hud-text outline-none focus:border-faction-nt/60 resize-none"
              ></textarea>
              <div class="flex gap-2">
                <button
                  class="hud-btn hud-btn--primary flex-1"
                  type="button"
                  :disabled="!canInject"
                  @click="onInjectTopic"
                >
                  注入议题
                </button>
                <button
                  class="hud-btn"
                  type="button"
                  :disabled="topicDepth === 0"
                  @click="onPopTopic"
                >
                  POP
                </button>
              </div>
              <div
                v-if="topicDepth > 0"
                class="font-mono text-2xs text-hud-muted"
              >
                当前栈深: {{ topicDepth }}
              </div>
            </section>

            <!-- 控制按钮 -->
            <section class="hud-panel--bare flex flex-col gap-2 p-3">
              <h3
                class="font-mono text-2xs uppercase tracking-hud-wider text-hud-muted"
              >
                会话控制
              </h3>
              <div class="grid grid-cols-2 gap-2">
                <button
                  class="hud-btn"
                  type="button"
                  :disabled="!canPause"
                  @click="onPause"
                >
                  暂停
                </button>
                <button
                  class="hud-btn hud-btn--primary"
                  type="button"
                  :disabled="!canResume"
                  @click="onResume"
                >
                  继续
                </button>
              </div>
              <button
                class="hud-btn hud-btn--danger"
                type="button"
                :disabled="!canForceInterrupt"
                @click="onForceInterrupt"
              >
                强制打断当前发言
              </button>
            </section>

            <!-- Arbitrator / Watchdog / Semaphore 摘要 -->
            <section class="hud-panel--bare flex flex-col gap-2 p-3">
              <h3
                class="font-mono text-2xs uppercase tracking-hud-wider text-hud-muted"
              >
                运行时指标
              </h3>
              <div class="grid grid-cols-2 gap-2 font-mono text-2xs">
                <MetricCell
                  label="持麦"
                  :value="arb.current_holder_id ?? '—'"
                  highlight
                />
                <MetricCell label="令牌龄" :value="`${arb.current_token_age_sec.toFixed(1)}s`" />
                <MetricCell label="授予总数" :value="arb.granted_total" />
                <MetricCell label="抢话总数" :value="arb.interrupt_granted_total" />
                <MetricCell label="拒绝总数" :value="arb.rejected_total" />
                <MetricCell
                  label="冷却剩余"
                  :value="`${arb.cooldown_remaining_sec.toFixed(1)}s`"
                />
                <MetricCell
                  label="信号量"
                  :value="`${sem.held_count}/${sem.capacity}`"
                />
                <MetricCell label="等待中" :value="sem.waiting_count" />
                <MetricCell
                  label="软驱逐"
                  :value="wd.soft_evict_total"
                />
                <MetricCell
                  label="硬驱逐"
                  :value="wd.hard_evict_total"
                />
                <MetricCell
                  label="进行中"
                  :value="wd.in_flight_count"
                />
                <MetricCell
                  label="信号量等待 ms"
                  :value="sem.last_acquire_wait_ms.toFixed(0)"
                />
              </div>

              <div
                v-if="wd.last_eviction"
                class="mt-1 rounded border border-cyber-red/30 bg-cyber-red/10 px-2 py-1.5 font-mono text-2xs text-cyber-red"
              >
                最近驱逐: {{ wd.last_eviction.kind }} ·
                {{ wd.last_eviction.agent_id }} ·
                {{ wd.last_eviction.reason }}
              </div>
            </section>

            <!-- 操作反馈 toast -->
            <transition
              enter-active-class="transition duration-150 ease-out"
              enter-from-class="opacity-0 -translate-y-1"
              leave-active-class="transition duration-200 ease-in"
              leave-to-class="opacity-0"
            >
              <div
                v-if="lastActionMsg"
                class="rounded border border-white/10 bg-white/[0.04] px-3 py-2 font-mono text-2xs"
                :class="
                  lastActionOk
                    ? 'text-emerald-400'
                    : 'text-cyber-red'
                "
              >
                {{ lastActionMsg }}
              </div>
            </transition>
          </div>
        </template>
      </HudShell>
    </template>
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  defineComponent,
  h,
  onBeforeUnmount,
  ref,
  type PropType,
} from 'vue';

import HudShell from '@/components/layout/HudShell.vue';
import AgentCard from '@/components/agent/AgentCard.vue';
import DebateStream from '@/components/debate/DebateStream.vue';

import { useSnapshot } from '@/composables/useSnapshot';
import { useWebSocket } from '@/composables/useWebSocket';
import { useAgentsStore } from '@/stores/agents';
import { useDebateStore } from '@/stores/debate';
import {
  factionColorClasses,
  factionDisplayName,
} from '@/utils/colorMap';
import type { AgentStateView, Faction } from '@/types/protocol';

// ── 启动两大引擎 ──────────────────────────────────────────────────────────

const snapshot = useSnapshot();
const ws = useWebSocket();

const snapshotPhase = snapshot.phase;
const snapshotError = snapshot.lastError;

// ── stores ────────────────────────────────────────────────────────────────

const agentsStore = useAgentsStore();
const debateStore = useDebateStore();

// ── 加载文案 ──────────────────────────────────────────────────────────────

const loadingMessage = '正在与后端握手 · 拉取沙盒全量快照…';
const retryingMessage = computed<string>(() => {
  const err = snapshotError.value;
  return err
    ? `连接受阻,正在重试 · ${err.slice(0, 80)}`
    : '正在重试连接后端…';
});
const degradedMessage = computed<string>(() => {
  const err = snapshotError.value;
  return err
    ? `5 次水合失败 · ${err.slice(0, 120)}`
    : '后端水合多次失败,可手动重试';
});

async function onRetry(): Promise<void> {
  await snapshot.refetch();
}

// ── Agent 分组 ────────────────────────────────────────────────────────────

const agentsList = computed<AgentStateView[]>(() => agentsStore.asList);

interface AgentGroup {
  faction: Faction;
  label: string;
  agents: AgentStateView[];
}

const groupedAgents = computed<AgentGroup[]>(() => {
  const order: Faction[] = ['NT', 'NF', 'SJ', 'SP'];
  const groups: AgentGroup[] = [];
  for (const f of order) {
    const list = agentsList.value.filter((a) => a.descriptor.faction === f);
    if (list.length > 0) {
      groups.push({
        faction: f,
        label: factionDisplayName(f),
        agents: list,
      });
    }
  }
  return groups;
});

function factionTextClass(faction: Faction): string {
  return factionColorClasses(faction).text;
}

// ── 控制台 · 议题注入 ────────────────────────────────────────────────────

const topicTitleInput = ref<string>('');
const topicDescInput = ref<string>('');

const topicDepth = computed<number>(() => debateStore.topicDepth);

const canInject = computed<boolean>(
  () =>
    topicTitleInput.value.trim().length > 0 &&
    debateStore.sessionStatus !== 'SHUTTING_DOWN',
);

async function onInjectTopic(): Promise<void> {
  const title = topicTitleInput.value.trim();
  if (!title) return;
  const desc = topicDescInput.value.trim();
  try {
    const resp = await fetch('/api/control/topic/inject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title,
        description: desc,
        auto_start_debate: true,
      }),
    });
    const data = (await resp.json()) as {
      accepted: boolean;
      message: string;
    };
    flashAction(data.accepted, data.message);
    if (data.accepted) {
      topicTitleInput.value = '';
      topicDescInput.value = '';
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    flashAction(false, `网络异常: ${msg}`);
  }
}

async function onPopTopic(): Promise<void> {
  try {
    const resp = await fetch('/api/control/topic/pop', { method: 'POST' });
    const data = (await resp.json()) as {
      accepted: boolean;
      message: string;
    };
    flashAction(data.accepted, data.message);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    flashAction(false, `网络异常: ${msg}`);
  }
}

// ── 会话控制 ──────────────────────────────────────────────────────────────

const canPause = computed<boolean>(
  () =>
    debateStore.sessionStatus === 'RUNNING' ||
    debateStore.sessionStatus === 'READY',
);
const canResume = computed<boolean>(
  () =>
    debateStore.sessionStatus === 'PAUSED' ||
    debateStore.sessionStatus === 'READY',
);
const canForceInterrupt = computed<boolean>(
  () => agentsStore.currentHolderId !== null,
);

async function callControlAction(
  action: 'PAUSE' | 'RESUME' | 'FORCE_INTERRUPT' | 'EMERGENCY_STOP',
  reason: string,
): Promise<void> {
  try {
    const resp = await fetch('/api/control/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, reason }),
    });
    if (resp.status === 409) {
      const text = await resp.text();
      flashAction(false, `状态冲突 · ${text.slice(0, 120)}`);
      return;
    }
    const data = (await resp.json()) as {
      accepted: boolean;
      message: string;
    };
    flashAction(data.accepted, data.message);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    flashAction(false, `网络异常: ${msg}`);
  }
}

function onPause(): void {
  void callControlAction('PAUSE', 'operator_click');
}
function onResume(): void {
  void callControlAction('RESUME', 'operator_click');
}
function onForceInterrupt(): void {
  void callControlAction('FORCE_INTERRUPT', 'operator_click');
}

// ── Toast 反馈 ────────────────────────────────────────────────────────────

const lastActionMsg = ref<string>('');
const lastActionOk = ref<boolean>(true);
let toastTimer: number | null = null;

function flashAction(ok: boolean, msg: string): void {
  lastActionOk.value = ok;
  lastActionMsg.value = msg;
  if (toastTimer !== null) {
    window.clearTimeout(toastTimer);
  }
  toastTimer = window.setTimeout(() => {
    lastActionMsg.value = '';
    toastTimer = null;
  }, 4_000);
}

// ── 派生指标 ──────────────────────────────────────────────────────────────

const arb = computed(() => agentsStore.arbitrator);
const wd = computed(() => agentsStore.watchdog);
const sem = computed(() => agentsStore.semaphore);

// ── BootSplash 子组件 (defineComponent 写法) ─────────────────────────────

const BootSplash = defineComponent({
  props: {
    status: {
      type: String as PropType<'loading' | 'retrying' | 'degraded'>,
      required: true,
    },
    message: {
      type: String,
      required: true,
    },
  },
  setup(props, { slots }) {
    return () =>
      h(
        'div',
        {
          class:
            'flex h-full w-full flex-col items-center justify-center gap-5 bg-hud-bg text-center',
        },
        [
          h('div', { class: 'flex flex-col items-center gap-3' }, [
            h('div', {
              class:
                props.status === 'degraded'
                  ? 'h-3 w-3 rounded-full bg-cyber-red shadow-glow-red'
                  : props.status === 'retrying'
                    ? 'h-3 w-3 rounded-full bg-amber-500 shadow-glow-amber animate-aggro-pulse'
                    : 'h-3 w-3 rounded-full bg-faction-nt shadow-glow-cyan animate-aggro-pulse',
            }),
            h(
              'div',
              {
                class:
                  'font-mono text-2xs uppercase tracking-hud-wider text-hud-muted',
              },
              props.status === 'degraded'
                ? 'BACKEND UNREACHABLE'
                : props.status === 'retrying'
                  ? 'RETRYING'
                  : 'INITIALIZING',
            ),
            h(
              'div',
              { class: 'font-mono text-sm text-hud-text/80 max-w-md px-6' },
              props.message,
            ),
          ]),
          slots.actions ? slots.actions() : null,
        ],
      );
  },
});

// ── MetricCell 子组件 ────────────────────────────────────────────────────

const MetricCell = defineComponent({
  props: {
    label: { type: String, required: true },
    value: { type: [String, Number], required: true },
    highlight: { type: Boolean, default: false },
  },
  setup(props) {
    return () =>
      h(
        'div',
        {
          class:
            'flex items-center justify-between rounded border border-white/5 bg-white/[0.02] px-2 py-1',
        },
        [
          h(
            'span',
            { class: 'text-hud-muted uppercase tracking-hud-wide' },
            props.label,
          ),
          h(
            'span',
            {
              class: [
                'tabular-nums',
                props.highlight
                  ? 'text-faction-nt text-shadow-glow-cyan'
                  : 'text-hud-text',
              ],
            },
            String(props.value ?? '—'),
          ),
        ],
      );
  },
});

// ── MetricCell 子组件 (内嵌定义) ─────────────────────────────────────────

const MetricCell = (props: {
  label: string;
  value: string | number;
  highlight?: boolean;
}) =>
  h(
    'div',
    {
      class: [
        'flex items-center justify-between rounded border border-white/5 bg-white/[0.02] px-2 py-1',
      ],
    },
    [
      h(
        'span',
        { class: 'text-hud-muted uppercase tracking-hud-wide' },
        props.label,
      ),
      h(
        'span',
        {
          class: [
            'tabular-nums',
            props.highlight ? 'text-faction-nt text-shadow-glow-cyan' : 'text-hud-text',
          ],
        },
        String(props.value ?? '—'),
      ),
    ],
  );

// ── 卸载清理 ──────────────────────────────────────────────────────────────

onBeforeUnmount(() => {
  if (toastTimer !== null) {
    window.clearTimeout(toastTimer);
    toastTimer = null;
  }
  // useWebSocket / useSnapshot 的 onBeforeUnmount 已经通过 refCount 自清理
});

// 防止 ws 引用被 tree-shake (它的 onMounted 必须执行)
void ws;
</script>