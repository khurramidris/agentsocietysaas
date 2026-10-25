import path from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
// The upstream VS Code extension's React webviews (replay viewer, config editors, module
// catalog) are embedded unmodified; see src/upstream/host.ts for the host shim.
const upstreamWebview = path.join(repo, "upstream/agentsociety/extension/src/webview");
const upstreamMedia = path.join(repo, "upstream/agentsociety/extension/media");
const api = process.env.SOCIETAS_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.join(here, "src"),
      "@upstream": upstreamWebview,
      "@upstream-media": upstreamMedia,
    },
    dedupe: ["react", "react-dom", "antd", "@ant-design/icons", "i18next", "react-i18next"],
  },
  server: {
    port: 5173,
    fs: { allow: [repo] },
    proxy: Object.fromEntries(
      ["/api", "/llm", "/literature", "/ide", "/healthz"].map((prefix) => [prefix, { target: api, changeOrigin: false, ws: prefix === "/ide" }]),
    ),
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 4000,
    rolldownOptions: {
      output: {
        advancedChunks: {
          groups: [
            { name: "monaco", test: /monaco-editor/ },
            { name: "map", test: /mapbox-gl|deck\.gl|luma\.gl|react-map-gl/ },
            { name: "antd", test: /node_modules[\\/](antd|@ant-design|rc-|@rc-component)/ },
          ],
        },
      },
    },
  },
});
