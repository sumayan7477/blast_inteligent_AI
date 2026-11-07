/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#0a0d12",
          900: "#0f141b",
          850: "#131a23",
          800: "#18202b",
          700: "#222c39",
          600: "#2e3a4a",
          500: "#4a586b",
          400: "#7a8899",
          300: "#a7b2bf",
          200: "#cdd5de",
          100: "#e8edf2",
        },
        ore: { 500: "#e0a526", 400: "#efbd4f", 300: "#f6d488", 900: "#3a2a07" },
        signal: { ok: "#3fb68b", warn: "#e0a526", bad: "#e5604f", info: "#5aa2d8" },
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "-apple-system", "Roboto", "Helvetica Neue", "Arial", "sans-serif"],
        mono: ["JetBrains Mono", "Cascadia Mono", "Consolas", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
