/**
 * Vite plugin: apply upstream/patches/web/*.patch to the vendored upstream sources as
 * they are loaded, in memory. The vendored tree stays byte-identical to the pinned
 * upstream commit (so sync_upstream.py can refresh it); the build fails loudly when a
 * patch no longer applies after an upstream update.
 */

import fs from "node:fs";
import path from "node:path";

import { applyPatch, parsePatch, type StructuredPatch } from "diff";
import type { Plugin } from "vite";

interface FilePatch {
  name: string;
  patch: StructuredPatch;
}

export function upstreamPatches(upstreamRoot: string, patchDir: string): Plugin {
  const byFile = new Map<string, FilePatch[]>();
  const names = fs.existsSync(patchDir) ? fs.readdirSync(patchDir).filter((f) => f.endsWith(".patch")).sort() : [];
  for (const name of names) {
    for (const patch of parsePatch(fs.readFileSync(path.join(patchDir, name), "utf8"))) {
      const rel = (patch.newFileName ?? patch.oldFileName ?? "").replace(/^b\//, "");
      if (!rel) continue;
      const file = path.join(upstreamRoot, rel);
      byFile.set(file, [...(byFile.get(file) ?? []), { name, patch }]);
    }
  }

  return {
    name: "upstream-patches",
    enforce: "pre",
    load(id) {
      const file = id.split("?")[0];
      const patches = byFile.get(file);
      if (!patches) return undefined;
      let text = fs.readFileSync(file, "utf8");
      for (const { name, patch } of patches) {
        const out = applyPatch(text, patch);
        if (out === false) {
          throw new Error(`upstream/patches/web/${name} no longer applies to ${path.relative(upstreamRoot, file)}; update the patch for the new upstream version`);
        }
        text = out;
        this.addWatchFile(path.join(patchDir, name));
      }
      return text;
    },
  };
}
