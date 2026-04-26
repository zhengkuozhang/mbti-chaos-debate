/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // 阵营语义色 (NT/NF/SJ/SP)
        faction: {
          nt: '#06B6D4', // cyan-500
          nt_glow: 'rgba(6, 182, 212, 0.45)',
          nf: '#D946EF', // fuchsia-500
          nf_glow: 'rgba(217, 70, 239, 0.45)',
          sj: '#F59E0B', // amber-500
          sj_glow: 'rgba(245, 158, 11, 0.45)',
          sp: '#10B981', // emerald-500
          sp_glow: 'rgba(16, 185, 129, 0.45)',
        },
        // Cyber Red (打断动效专用)
        cyber: {
          red: '#E11D48', // rose-600 偏热的红
          red_glow: 'rgba(225, 29, 72, 0.55)',
        },
        // HUD 灰阶
        hud: {
          bg: '#09090B', // zinc-950
          surface: '#18181B', // zinc-900
          border: '#27272A', // zinc-800
          text: '#E4E4E7', // zinc-200
          muted: '#71717A', // zinc-500
        },
      },

      fontFamily: {
        sans: [
          'Inter',
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'Roboto',
          'PingFang SC',
          'Hiragino Sans GB',
          'Microsoft YaHei',
          'sans-serif',
        ],
        mono: [
          'JetBrains Mono',
          'ui-monospace',
          'SF Mono',
          'Cascadia Code',
          'Menlo',
          'monospace',
        ],
      },

      fontSize: {
        // HUD 专用尺寸,精确对齐三栏布局
        '2xs': ['10px', { lineHeight: '14px' }],
        'hud-mono-sm': ['13px', { lineHeight: '20px', letterSpacing: '0.01em' }],
        'hud-mono-md': ['15px', { lineHeight: '24px', letterSpacing: '0.005em' }],
        'hud-mono-lg': ['18px', { lineHeight: '28px' }],
      },

      letterSpacing: {
        'hud-wide': '0.18em',
        'hud-wider': '0.3em',
      },

      backdropBlur: {
        xs: '2px',
        '4xl': '64px',
      },

      boxShadow: {
        'glow-cyan': '0 0 18px 0 rgba(6, 182, 212, 0.35)',
        'glow-fuchsia': '0 0 18px 0 rgba(217, 70, 239, 0.35)',
        'glow-amber': '0 0 18px 0 rgba(245, 158, 11, 0.35)',
        'glow-emerald': '0 0 18px 0 rgba(16, 185, 129, 0.35)',
        'glow-red': '0 0 24px 0 rgba(225, 29, 72, 0.55)',
      },

      keyframes: {
        // Aggro >70% 呼吸动画
        'aggro-pulse': {
          '0%, 100%': {
            opacity: '0.85',
            filter: 'brightness(1.0)',
          },
          '50%': {
            opacity: '1',
            filter: 'brightness(1.25)',
          },
        },
        // 打断时中央文本物理摇晃
        'shake-x': {
          '0%, 100%': { transform: 'translate3d(0, 0, 0)' },
          '15%': { transform: 'translate3d(-6px, 0, 0)' },
          '30%': { transform: 'translate3d(7px, 0, 0)' },
          '45%': { transform: 'translate3d(-5px, 1px, 0)' },
          '60%': { transform: 'translate3d(4px, -1px, 0)' },
          '75%': { transform: 'translate3d(-2px, 0, 0)' },
          '90%': { transform: 'translate3d(1px, 0, 0)' },
        },
        // 打断瞬间红色闪烁
        'cyber-flash': {
          '0%': { backgroundColor: 'rgba(225, 29, 72, 0.0)' },
          '20%': { backgroundColor: 'rgba(225, 29, 72, 0.18)' },
          '60%': { backgroundColor: 'rgba(225, 29, 72, 0.08)' },
          '100%': { backgroundColor: 'rgba(225, 29, 72, 0.0)' },
        },
        // 影子截断后缀文本碎裂式淡入
        'shadow-shatter': {
          '0%': {
            opacity: '0',
            transform: 'translateY(-4px) scale(0.92)',
            filter: 'blur(2px)',
          },
          '100%': {
            opacity: '0.85',
            transform: 'translateY(0) scale(1)',
            filter: 'blur(0)',
          },
        },
        // FSM 指示器自发光
        'fsm-glow': {
          '0%, 100%': { opacity: '0.55' },
          '50%': { opacity: '1' },
        },
      },

      animation: {
        'aggro-pulse': 'aggro-pulse 1.4s ease-in-out infinite',
        'shake-x': 'shake-x 0.55s cubic-bezier(.36,.07,.19,.97) both',
        'cyber-flash': 'cyber-flash 0.6s ease-out both',
        'shadow-shatter': 'shadow-shatter 0.5s ease-out both',
        'fsm-glow': 'fsm-glow 1.8s ease-in-out infinite',
      },

      transitionDuration: {
        80: '80ms',
        180: '180ms',
        280: '280ms',
      },
    },
  },
  plugins: [],
};