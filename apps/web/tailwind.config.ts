import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: "#0a0a0f",
        panel: "#0e0e15",
        card: "#15151d",
        card2: "#1b1b25",
        line: "#262632",
        line2: "#34343f",
        txt: "#e9e9f0",
        muted: "#9a9aa6",
        faint: "#62626e",
        indigo: "#6366f1",
        indigo2: "#4f46e5",
        violet: "#8b5cf6",
        blue: "#3b82f6",
        orange: "#f97316",
        green: "#22c55e",
        red: "#ef4444",
        yellow: "#eab308",
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
