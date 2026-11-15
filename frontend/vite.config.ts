// 画面のビルドの設定。ビルド結果は Python パッケージに同梱する（circuit_sim/app/static。D-041）。
// 外部の CDN を使わない。書き出すファイル名は中身から決まる（同じソースなら同じ結果になる）。
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../circuit_sim/app/static",
    emptyOutDir: true,
    sourcemap: false,
    reportCompressedSize: false,
    target: "es2022",
    assetsInlineLimit: 0,
  },
  server: {
    // 開発用: npm run dev の画面から、別に起動したアプリ本体（circuit-sim --no-browser --port 8765）へ
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
