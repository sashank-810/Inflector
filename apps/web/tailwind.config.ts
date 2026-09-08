import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#0b0f14",
        panel: "#111820",
        raised: "#17212b",
        line: "#26323e",
        ink: "#e6edf3",
        muted: "#91a0af",
        accent: "#82b1d9",
        positive: "#6fbf92",
        negative: "#e58c8c"
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Consolas", "monospace"]
      }
    }
  },
  plugins: []
};

export default config;
