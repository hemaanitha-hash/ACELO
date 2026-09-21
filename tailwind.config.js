/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: {
          DEFAULT: "#FFFFFF",
          raised: "#FBF5F6",
        },
        panel: {
          DEFAULT: "#FFFFFF",
          hover: "#FDF1F2",
          border: "#EDD9DB",
          borderStrong: "#DBB7BA",
        },
        ink: {
          DEFAULT: "#241014",
          muted: "#7A5459",
          faint: "#B08D90",
        },
        brand: {
          50: "#FCE9EB",
          100: "#F8C9CE",
          300: "#E8828D",
          400: "#E24A52",
          500: "#D71920",
          600: "#B01118",
          700: "#7D1126",
          900: "#3D0812",
        },
        signal: {
          high: "#E2412D",
          medium: "#C98A1E",
          low: "#2F7D45",
          info: "#3D6FB4",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "sans-serif",
        ],
        mono: ["IBM Plex Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      borderRadius: {
        sm: "6px",
        DEFAULT: "10px",
        lg: "14px",
      },
      boxShadow: {
        panel: "0 1px 0 0 rgba(255,255,255,0.02) inset",
        elevated: "0 8px 24px -12px rgba(0,0,0,0.55)",
      },
      fontSize: {
        display: ["2rem", { lineHeight: "1.15", letterSpacing: "-0.01em" }],
      },
    },
  },
  plugins: [],
};
