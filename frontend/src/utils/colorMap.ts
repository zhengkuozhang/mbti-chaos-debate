/**
 * ============================================================================
 * frontend/src/utils/colorMap.ts
 * ----------------------------------------------------------------------------
 * 阵营 (Faction) → 视觉语义色映射
 *
 * 提供三类 API:
 *   - factionColorClasses(faction)  返回 Tailwind class 集合 (供模板拼接)
 *   - factionHex(faction)           返回十六进制色 (供 SVG / Canvas 直接使用)
 *   - factionGlowColor(faction)     返回带 alpha 的 rgba 字符串 (供 box-shadow)
 *
 * 颜色定义必须与 tailwind.config.js theme.extend.colors.faction 完全对齐。
 * ============================================================================
 */

import type { Faction } from '@/types/protocol';

// ── 基础色板 (与 tailwind.config.js 同源) ──────────────────────────────────

export const FACTION_HEX: Readonly<Record<Faction, string>> = Object.freeze({
  NT: '#06B6D4', // cyan-500
  NF: '#D946EF', // fuchsia-500
  SJ: '#F59E0B', // amber-500
  SP: '#10B981', // emerald-500
});

export const FACTION_GLOW_RGBA: Readonly<Record<Faction, string>> =
  Object.freeze({
    NT: 'rgba(6, 182, 212, 0.45)',
    NF: 'rgba(217, 70, 239, 0.45)',
    SJ: 'rgba(245, 158, 11, 0.45)',
    SP: 'rgba(16, 185, 129, 0.45)',
  });

export const FACTION_DISPLAY_NAME: Readonly<Record<Faction, string>> =
  Object.freeze({
    NT: '理性 · NT',
    NF: '共情 · NF',
    SJ: '秩序 · SJ',
    SP: '应变 · SP',
  });

// ── Cyber Red (打断专用) ──────────────────────────────────────────────────

export const CYBER_RED_HEX = '#E11D48';
export const CYBER_RED_GLOW = 'rgba(225, 29, 72, 0.55)';
export const CYBER_RED_FLASH = 'rgba(225, 29, 72, 0.18)';

// ── Tailwind class 集合接口 ───────────────────────────────────────────────

export interface FactionClasses {
  /** 文本主色 */
  text: string;
  /** 边框主色 */
  border: string;
  /** 背景填充 (浅淡) */
  bgSoft: string;
  /** 强调底色 (用于进度条 / Aggro Bar) */
  bgStrong: string;
  /** 阴影 / 光晕 (使用 tailwind.config.js 中 boxShadow) */
  glowShadow: string;
  /** 渐变上半 (用于 AggroBar 的 from) */
  gradientFrom: string;
  /** 渐变下半 (用于 AggroBar 的 to) */
  gradientTo: string;
}

const NT_CLASSES: FactionClasses = Object.freeze({
  text: 'text-faction-nt',
  border: 'border-faction-nt',
  bgSoft: 'bg-faction-nt/10',
  bgStrong: 'bg-faction-nt',
  glowShadow: 'shadow-glow-cyan',
  gradientFrom: 'from-cyan-400',
  gradientTo: 'to-cyan-600',
});

const NF_CLASSES: FactionClasses = Object.freeze({
  text: 'text-faction-nf',
  border: 'border-faction-nf',
  bgSoft: 'bg-faction-nf/10',
  bgStrong: 'bg-faction-nf',
  glowShadow: 'shadow-glow-fuchsia',
  gradientFrom: 'from-fuchsia-400',
  gradientTo: 'to-fuchsia-600',
});

const SJ_CLASSES: FactionClasses = Object.freeze({
  text: 'text-faction-sj',
  border: 'border-faction-sj',
  bgSoft: 'bg-faction-sj/10',
  bgStrong: 'bg-faction-sj',
  glowShadow: 'shadow-glow-amber',
  gradientFrom: 'from-amber-400',
  gradientTo: 'to-amber-600',
});

const SP_CLASSES: FactionClasses = Object.freeze({
  text: 'text-faction-sp',
  border: 'border-faction-sp',
  bgSoft: 'bg-faction-sp/10',
  bgStrong: 'bg-faction-sp',
  glowShadow: 'shadow-glow-emerald',
  gradientFrom: 'from-emerald-400',
  gradientTo: 'to-emerald-600',
});

const FACTION_CLASS_MAP: Readonly<Record<Faction, FactionClasses>> =
  Object.freeze({
    NT: NT_CLASSES,
    NF: NF_CLASSES,
    SJ: SJ_CLASSES,
    SP: SP_CLASSES,
  });

// ── 公开 API ──────────────────────────────────────────────────────────────

/**
 * 返回该阵营的全套 Tailwind class 集合。
 * 使用示例: <div :class="factionColorClasses(faction).border">
 */
export function factionColorClasses(faction: Faction): FactionClasses {
  return FACTION_CLASS_MAP[faction];
}

/** 返回阵营主色十六进制 (供 SVG / 内联 style) */
export function factionHex(faction: Faction): string {
  return FACTION_HEX[faction];
}

/** 返回阵营光晕色 rgba (供动态 box-shadow) */
export function factionGlowColor(faction: Faction): string {
  return FACTION_GLOW_RGBA[faction];
}

/** 返回阵营中文显示名 */
export function factionDisplayName(faction: Faction): string {
  return FACTION_DISPLAY_NAME[faction];
}

/**
 * Aggro 数值 → 强度等级 (用于触发 pulse 动画判定)
 *   - calm   : 0-39
 *   - alert  : 40-69
 *   - burning: 70-100  (触发 aggro-pulse 呼吸动画)
 */
export type AggroIntensity = 'calm' | 'alert' | 'burning';

export function aggroIntensity(value: number): AggroIntensity {
  if (value >= 70) return 'burning';
  if (value >= 40) return 'alert';
  return 'calm';
}