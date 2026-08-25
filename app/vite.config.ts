// `defineConfig` comes from vitest rather than vite because the `test` key
// below is not part of vite's own config type, and tsconfig.json includes
// this file — so vite's version would fail `tsc --noEmit`.
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// The port is fixed and `strictPort` is set because tauri.conf.json's devUrl
// names it explicitly. Letting Vite fall back to the next free port would leave
// the webview pointed at nothing, which shows up as a blank window rather than
// an error.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    watch: {
      // Rust sources are rebuilt by cargo, not Vite; watching them just burns
      // file handles on a target/ directory that churns constantly.
      ignored: ["**/src-tauri/**"],
    },
  },
  build: {
    target: "es2021",
    sourcemap: true,
  },
  test: {
    // The component tests render into a DOM. The two older suites read source
    // text off disk with node:fs, which jsdom does not take away from them.
    environment: "jsdom",
    restoreMocks: true,
  },
});
