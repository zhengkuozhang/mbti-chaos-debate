/**
 * ============================================================================
 * frontend/src/composables/useInterruptShake.ts
 * ----------------------------------------------------------------------------
 * 打断动画触发器
 *
 * 职责:
 *   监听 debateStore.lastInterruptAt 变化,
 *   触发指定 element 的 shake + cyber-flash 组合动画。
 *
 * 设计原则:
 *   1. CSS class toggle · 不直接操控 transform/opacity
 *   2. animationend 后自动移除 class · 避免动画堆叠
 *   3. 200ms 内连续 INTERRUPT 合并为一次 · 防视觉抽搐
 *   4. 暴露最近一次 interrupt 的 interrupter / interrupted 元数据
 *
 * 使用方式:
 *   const target = ref<HTMLElement>();
 *   const { lastDetail, isShaking } = useInterruptShake(target);
 *
 * ⚠️ 工程铁律:
 *   - 严禁在 watch 中直接操控 DOM · 必须通过 ref 委托
 *   - 严禁忘记移除 class · 用 finally + 兜底 setTimeout
 *   - 严禁让动画影响布局 · 仅 transform / opacity / shadow
 * ============================================================================
 */

import { onBeforeUnmount, ref, watch } from 'vue';
import type { Ref } from 'vue';

import { useDebateStore } from '@/stores/debate';

// ── 常量 ──────────────────────────────────────────────────────────────────

/** 与 tailwind.config.js 中 keyframes 对齐 */
const SHAKE_CLASS = 'animate-shake-x';
const FLASH_CLASS = 'animate-cyber-flash';

/** 合并窗口: 此区间内的连续 INTERRUPT 不重复触发 */
const COALESCE_WINDOW_MS = 200;

/** 兜底超时: 即使 animationend 没触发,也强制移除 class */
const FALLBACK_REMOVE_MS = 900;

// ── 公开 API ──────────────────────────────────────────────────────────────

export interface InterruptDetail {
  interrupter: string;
  interrupted: string;
  triggeredAt: number;
}

export interface UseInterruptShakeApi {
  /** 当前是否正在 shake · 供组件附加额外样式 */
  isShaking: Ref<boolean>;
  /** 最近一次 interrupt 的元数据 (含触发时刻) */
  lastDetail: Ref<InterruptDetail | null>;
  /** 手动触发动画 (供调试) */
  triggerManually: () => void;
}

/**
 * 监听 debate store 的 lastInterruptAt,触发 target 元素的动画。
 *
 * @param targetRef 目标元素 ref · 通常是中央辩论流的根 <div>
 */
export function useInterruptShake(
  targetRef: Ref<HTMLElement | null | undefined>,
): UseInterruptShakeApi {
  const debateStore = useDebateStore();
  const isShaking = ref(false);
  const lastDetail = ref<InterruptDetail | null>(null);

  let lastFiredAt = 0;
  let cleanupTimer: number | null = null;
  let onAnimationEnd: ((ev: AnimationEvent) => void) | null = null;
  let currentTarget: HTMLElement | null = null;

  function bindListenerOnce(target: HTMLElement): void {
    if (currentTarget === target) return;
    if (currentTarget && onAnimationEnd) {
      currentTarget.removeEventListener('animationend', onAnimationEnd);
    }
    onAnimationEnd = (ev: AnimationEvent): void => {
      // 仅响应我们的两个动画
      if (ev.animationName !== 'shake-x' && ev.animationName !== 'cyber-flash') {
        return;
      }
      removeShake(target);
    };
    target.addEventListener('animationend', onAnimationEnd);
    currentTarget = target;
  }

  function removeShake(target: HTMLElement): void {
    target.classList.remove(SHAKE_CLASS, FLASH_CLASS);
    isShaking.value = false;
    if (cleanupTimer !== null) {
      window.clearTimeout(cleanupTimer);
      cleanupTimer = null;
    }
  }

  function applyShake(target: HTMLElement): void {
    // 先移除再添加,强制重新触发动画 (浏览器对同名 class 不会重启)
    target.classList.remove(SHAKE_CLASS, FLASH_CLASS);
    // 强制 reflow 重置动画
    void target.offsetWidth;
    target.classList.add(SHAKE_CLASS, FLASH_CLASS);
    isShaking.value = true;

    // 兜底超时移除
    if (cleanupTimer !== null) {
      window.clearTimeout(cleanupTimer);
    }
    cleanupTimer = window.setTimeout(() => {
      removeShake(target);
    }, FALLBACK_REMOVE_MS);
  }

  // ── 主监听: lastInterruptAt 变化 ──
  const stopWatch = watch(
    () => debateStore.lastInterruptAt,
    (newVal: number, oldVal: number | undefined) => {
      if (!newVal || newVal === oldVal) return;

      const now = Date.now();
      // 合并窗口内的连续触发 → 静默丢弃
      if (now - lastFiredAt < COALESCE_WINDOW_MS) {
        return;
      }
      lastFiredAt = now;

      // 同步元数据
      const detail = debateStore.lastInterruptDetail;
      if (detail) {
        lastDetail.value = {
          interrupter: detail.interrupter,
          interrupted: detail.interrupted,
          triggeredAt: now,
        };
      }

      // 应用动画
      const target = targetRef.value ?? null;
      if (!target) {
        // eslint-disable-next-line no-console
        console.debug('[shake] target ref 未挂载,跳过动画');
        return;
      }
      bindListenerOnce(target);
      applyShake(target);
    },
  );

  function triggerManually(): void {
    const target = targetRef.value ?? null;
    if (!target) return;
    lastFiredAt = Date.now();
    lastDetail.value = {
      interrupter: '__manual__',
      interrupted: '__manual__',
      triggeredAt: lastFiredAt,
    };
    bindListenerOnce(target);
    applyShake(target);
  }

  onBeforeUnmount(() => {
    stopWatch();
    if (cleanupTimer !== null) {
      window.clearTimeout(cleanupTimer);
      cleanupTimer = null;
    }
    if (currentTarget && onAnimationEnd) {
      currentTarget.removeEventListener('animationend', onAnimationEnd);
      currentTarget = null;
      onAnimationEnd = null;
    }
  });

  return {
    isShaking,
    lastDetail,
    triggerManually,
  };
}