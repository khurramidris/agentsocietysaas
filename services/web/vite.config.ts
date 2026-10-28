import path from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { upstreamReplayCss } from "./upstream-css";
import { upstreamPatches } from "./upstream-patches";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
// The upstream VS Code extension's React webviews (replay viewer, config editors, module
// catalog) are embedded unmodified; see src/upstream/host.ts for the host shim.
const upstreamWebview = path.join(repo, "upstream/agentsociety/extension/src/webview");
const upstreamMedia = path.join(repo, "upstream/agentsociety/extension/media");
const api = process.env.SOCIETAS_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [
    upstreamPatches(path.join(repo, "upstream/agentsociety"), path.join(repo, "upstream/patches/web")),
    react(),
    upstreamReplayCss(path.join(repo, "upstream/agentsociety/extension/src/replayWebviewProvider.ts")),
  ],
  resolve: {
    alias: [
      { find: /^mapbox-gl\/dist\/mapbox-gl-csp-worker$/, replacement: path.join(here, "src/upstream/mapboxWorker.ts") },
      { find: "@upstream-media", replacement: upstreamMedia },
      { find: "@upstream", replacement: upstreamWebview },
      { find: "@", replacement: path.join(here, "src") },
    ],
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
  },
});
