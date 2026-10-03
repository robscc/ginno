import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // ⚠️ `base` shadows Tailwind's `base` FONT-SIZE keyword: the utility
        // `text-base` compiles to BOTH font-size:1rem AND color:var(--base),
        // painting text the background color (invisible). Never use `text-base`
        // for font-size here — use `text-[1rem]` (or text-sm/lg/…) instead.
        base: "rgb(var(--base) / <alpha-value>)",
        panel: "rgb(var(--panel) / <alpha-value>)",
        card: "rgb(var(--card) / <alpha-value>)",
        card2: "rgb(var(--card2) / <alpha-value>)",
        line: "rgb(var(--line) / <alpha-value>)",
        line2: "rgb(var(--line2) / <alpha-value>)",
        txt: "rgb(var(--txt) / <alpha-value>)",
        muted: "rgb(var(--muted) / <alpha-value>)",
        faint: "rgb(var(--faint) / <alpha-value>)",
        // `bg-hover` / `hover:bg-hover` 的底色(此前未定义,编译为空)
        hover: "rgb(var(--card2) / <alpha-value>)",
        // 语义色:DEFAULT 保持既有 token 用法(text-green 等);数字档位补齐
        // Tailwind 默认色阶,否则 `bg-violet-600` 这类写法编译为空 → light
        // 模式下白字按钮落在白卡上完全隐形(2026-10-02 连接器安装指引)。
        indigo: { DEFAULT: "#6366f1", 400: "#818cf8", 500: "#6366f1", 600: "#4f46e5" },
        indigo2: "#4f46e5",
        violet: { DEFAULT: "#8b5cf6", 400: "#a78bfa", 500: "#8b5cf6", 600: "#7c3aed" },
        blue: { DEFAULT: "#3b82f6", 400: "#60a5fa", 500: "#3b82f6", 600: "#2563eb" },
        orange: { DEFAULT: "#f97316", 400: "#fb923c", 500: "#f97316", 600: "#ea580c" },
        green: { DEFAULT: "#22c55e", 100: "#dcfce7", 400: "#4ade80", 500: "#22c55e", 600: "#16a34a", 900: "#14532d" },
        red: { DEFAULT: "#ef4444", 400: "#f87171", 500: "#ef4444", 600: "#dc2626" },
        yellow: { DEFAULT: "#eab308", 200: "#fef08a", 400: "#facc15", 500: "#eab308", 600: "#ca8a04", 900: "#713f12" },
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      borderRadius: {
        xl: "0.9rem",
        "2xl": "1.1rem",
      },
    },
  },
  plugins: [],
};

export default config;
