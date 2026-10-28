/**
 * Vite plugin: the upstream replay webview's layout CSS lives in the <style> block of
 * the extension's replayWebviewProvider.ts (the HTML VS Code loads), not in the React
 * code. ``import "virtual:upstream-replay.css"`` extracts it at build time and scopes
 * every rule to ``.upstream-replay`` so it cannot leak into the rest of the app.
 */

import fs from "node:fs";
import path from "node:path";

import type { Plugin } from "vite";

const VIRTUAL = "virtual:upstream-replay.css";
const RESOLVED = "/__upstream__/replay.css";
const SCOPE = ".upstream-replay";

function scopeSelector(selector: string): string {
  const s = selector.trim();
  if (s === "*") return `${SCOPE} *`;
  if (s === "html" || s === "body" || s === "#root" || s === ":root") return SCOPE;
  if (s.startsWith("body.")) return `${SCOPE}${s.slice(4)}`;
  if (s.startsWith("body ")) return `${SCOPE}${s.slice(4)}`;
  return `${SCOPE} ${s}`;
}

/** Prefix every selector outside @keyframes with the scope class. */
export function scopeCss(css: string): string {
  let out = "";
  let prelude = "";
  const stack: ("keyframes" | "at" | "rule")[] = [];
  for (const ch of css) {
    if (ch === "{") {
      const head = prelude.trim();
      if (head.startsWith("@")) {
        stack.push(head.startsWith("@keyframes") || head.startsWith("@-webkit-keyframes") ? "keyframes" : "at");
        out += `${prelude}{`;
      } else if (stack[stack.length - 1] === "keyframes") {
        stack.push("rule");
        out += `${prelude}{`;
      } else {
        stack.push("rule");
        const leading = prelude.match(/^\s*/)?.[0] ?? "";
        out += `${leading}${head.split(",").map(scopeSelector).join(", ")} {`;
      }
      prelude = "";
    } else if (ch === "}") {
      stack.pop();
      out += `${prelude}}`;
      prelude = "";
    } else if (ch === ";" && stack[stack.length - 1] !== "rule" && stack.length === 0) {
      out += `${prelude};`;
      prelude = "";
    } else {
      prelude += ch;
    }
  }
  return out + prelude;
}

export function upstreamReplayCss(providerPath: string): Plugin {
  return {
    name: "upstream-replay-css",
    resolveId(id) {
      return id === VIRTUAL ? RESOLVED : undefined;
    },
    load(id) {
      if (id !== RESOLVED) return undefined;
      this.addWatchFile(providerPath);
      const source = fs.readFileSync(providerPath, "utf8");
      const match = source.match(/<style[^>]*>([\s\S]*?)<\/style>/);
      if (!match) throw new Error(`No <style> block in ${path.basename(providerPath)}`);
      return scopeCss(match[1]);
    },
  };
}
