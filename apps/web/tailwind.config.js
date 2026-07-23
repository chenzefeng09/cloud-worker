/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ['class', '[data-theme="dark"]'],
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Geist', 'Inter', '-apple-system', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
        display: ['"Instrument Serif"', '"Cormorant Garamond"', 'Georgia', 'serif'],
      },
      colors: {
        // Cloud-worker design tokens (light wash by default; dark via [data-theme="dark"])
        paper: {
          DEFAULT: '#f7f5ee',
          2: '#efece2',
        },
        surface: {
          DEFAULT: '#fffdf7',
          // Keep a few neutral steps so existing components (login etc.) keep compiling.
          50: '#f8fafc',
          100: '#f1f5f9',
          200: '#e2e8f0',
          800: '#1e293b',
          900: '#0f172a',
        },
        ink: {
          DEFAULT: '#15171a',
          2: '#3c3d40',
        },
        dim: '#75756f',
        line: {
          DEFAULT: '#d8d4c6',
          2: '#e6e3d6',
        },
        accent: {
          DEFAULT: '#d23a1f',
          soft: 'rgba(210, 58, 31, 0.08)',
          line: 'rgba(210, 58, 31, 0.5)',
        },
        ok: '#2c8f5d',
        warn: '#c68a17',
        // Legacy primary kept so LoginPage / earlier components still render.
        primary: { 50: '#eff6ff', 500: '#3b82f6', 600: '#2563eb', 700: '#1d4ed8' },
      },
      borderRadius: {
        sm: '4px',
        md: '6px',
        lg: '10px',
      },
      keyframes: {
        blip: {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.45', transform: 'scale(0.85)' },
        },
        popIn: {
          '0%': { opacity: '0', transform: 'scale(0.96)' },
          '100%': { opacity: '1', transform: 'scale(1)' },
        },
      },
      animation: {
        blip: 'blip 1.4s ease-in-out infinite',
        'pop-in': 'popIn 120ms ease-out',
      },
    },
  },
  plugins: [],
};
