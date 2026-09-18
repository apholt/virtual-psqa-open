/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Light / warm "paper" theme — repurposed clinical tokens so existing
        // pages re-theme automatically.
        clinical: {
          bg: "#F1EFE8",
          surface: "#ffffff",
          border: "rgba(0,0,0,0.12)",
          text: "#1a1a18",
          muted: "#5F5E5A",
          faint: "#888780",
          accent: "#3C3489",
          track: "#E8E6DF",
        },
        // Verdict / status palette (final hex values from design system).
        status: {
          measureBg: "#FCEBEB",
          measureText: "#791F1F",
          measureBorder: "#F09595",
          flagBg: "#FAEEDA",
          flagText: "#633806",
          passBg: "#EAF3DE",
          passText: "#27500A",
          pendBg: "#E6F1FB",
          pendText: "#0C447C",
          runBg: "#EEEDFE",
          runText: "#3C3489",
        },
        bar: {
          pass: "#639922",
          fail: "#E24B4A",
          warn: "#EF9F27",
        },
        accentPurple: "#7F77DD",
      },
    },
  },
  plugins: [],
};
