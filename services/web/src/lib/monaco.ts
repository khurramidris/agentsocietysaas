// Bundle Monaco instead of loading it from a CDN (self-hosted deployments, strict CSPs).
import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import CssWorker from "monaco-editor/language/css/css.worker?worker";
import HtmlWorker from "monaco-editor/language/html/html.worker?worker";
import JsonWorker from "monaco-editor/language/json/json.worker?worker";
import TsWorker from "monaco-editor/language/typescript/ts.worker?worker";

(self as unknown as { MonacoEnvironment: unknown }).MonacoEnvironment = {
  getWorker(_id: string, label: string) {
    if (label === "json") return new JsonWorker();
    if (label === "css" || label === "scss" || label === "less") return new CssWorker();
    if (label === "html" || label === "handlebars" || label === "razor") return new HtmlWorker();
    if (label === "typescript" || label === "javascript") return new TsWorker();
    return new EditorWorker();
  },
};

loader.config({ monaco });

const LANGUAGES: Record<string, string> = {
  py: "python",
  json: "json",
  ipynb: "json",
  jsonl: "json",
  md: "markdown",
  yaml: "yaml",
  yml: "yaml",
  toml: "ini",
  ini: "ini",
  cfg: "ini",
  sh: "shell",
  bash: "shell",
  js: "javascript",
  mjs: "javascript",
  ts: "typescript",
  tsx: "typescript",
  html: "html",
  htm: "html",
  css: "css",
  r: "r",
  sql: "sql",
  xml: "xml",
  tex: "latex",
  bib: "plaintext",
  csv: "plaintext",
  txt: "plaintext",
  log: "plaintext",
};

export function languageFor(path: string): string {
  const name = path.split("/").pop() ?? "";
  if (name === "Dockerfile") return "dockerfile";
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return LANGUAGES[ext] ?? "plaintext";
}
