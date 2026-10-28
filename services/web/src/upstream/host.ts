/**
 * Browser host for the upstream VS Code extension's webviews.
 *
 * In VS Code each webview talks to the extension through ``vscode.postMessage`` and
 * receives replies as window ``message`` events. Here the "extension side" is a
 * handler that turns each command into calls to this platform's API (the sandbox's
 * files API and the upstream agentsociety2 backend behind ``sbx/as2``). The
 * handlers below port replayWebviewProvider.ts, initConfigEditorProvider.ts,
 * simSettingsEditorProvider.ts and prefillParamsViewProvider.ts.
 *
 * Mount one upstream webview at a time: they all listen on ``window``.
 */

import { api, ApiError, as2, sbx, type Query } from "@/api/client";
import type { FileEntry } from "@/api/types";

export interface VSCodeAPI {
  postMessage: (message: any) => void;
  getState: () => any;
  setState: (state: any) => void;
}

export type Reply = (message: unknown) => void;
export type Handler = (message: any, reply: Reply) => void | Promise<void>;

function reply(message: unknown): void {
  // Asynchronous, like messages from a VS Code extension host.
  setTimeout(() => window.dispatchEvent(new MessageEvent("message", { data: message, origin: window.location.origin })), 0);
}

export function createHost(handler: Handler, onError?: (error: unknown) => void): VSCodeAPI {
  let state: unknown;
  return {
    postMessage: (message) => {
      Promise.resolve()
        .then(() => handler(message, reply))
        .catch((error) => onError?.(error));
    },
    getState: () => state,
    setState: (next) => {
      state = next;
    },
  };
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : String(error);
}

// --------------------------------------------------------------------- replay

/** Port of ReplayWebviewProvider.handleMessage. */
export function replayHandler(projectId: string, hypothesisId: string, experimentId: string, notify: (text: string) => void): Handler {
  const base = as2(projectId, `api/v1/replay/${hypothesisId}/${experimentId}`);
  const controllers = new Map<string, AbortController>();
  const versions = new Map<string, number>();

  const fetchJson = async <T>(resource: string, path: string, query: Query, send: Reply, latestGroup?: string): Promise<T | undefined> => {
    let version = 0;
    let signal: AbortSignal | undefined;
    if (latestGroup) {
      controllers.get(latestGroup)?.abort();
      const controller = new AbortController();
      controllers.set(latestGroup, controller);
      version = (versions.get(latestGroup) ?? 0) + 1;
      versions.set(latestGroup, version);
      signal = controller.signal;
    }
    try {
      const data = await api.get<T>(`${base}${path}`, query, signal);
      if (latestGroup && versions.get(latestGroup) !== version) return undefined;
      return data;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return undefined;
      send({ type: "error", message: `Failed to fetch ${resource}: ${errorMessage(error)}` });
      return undefined;
    }
  };

  return async (message, send) => {
    switch (message.command) {
      case "ready":
        send({ type: "init", data: { workspacePath: "/data/workspace", hypothesisId, experimentId, backendUrl: "" } });
        break;
      case "fetchExperimentInfo": {
        const data = await fetchJson("experiment info", "/info", {}, send);
        if (data) send({ type: "experimentInfo", data });
        break;
      }
      case "fetchTimeline": {
        const data = await fetchJson("timeline", "/timeline", {}, send);
        if (data) send({ type: "timeline", data });
        break;
      }
      case "fetchAgentProfiles": {
        const data = await fetchJson("agent profiles", "/agents/profiles", {}, send);
        if (data) send({ type: "agentProfiles", data });
        break;
      }
      case "fetchPanelSchema": {
        const data = await fetchJson("panel schema", "/panel-schema", {}, send);
        if (data) send({ type: "panelSchema", data });
        break;
      }
      case "fetchStepBundle": {
        const data = await fetchJson("step bundle", `/steps/${message.step}/bundle`, {}, send, "stepBundle");
        if (data) send({ type: "stepBundle", data });
        break;
      }
      case "fetchReplayDatasetRows": {
        const query: Query = {
          page: message.page ?? 1,
          page_size: message.pageSize ?? 50,
          step: message.step,
          entity_id: message.entityId,
          start_step: message.startStep,
          end_step: message.endStep,
          max_step: message.maxStep,
          columns: Array.isArray(message.columns) ? message.columns.join(",") : undefined,
          desc_order: message.descOrder,
          latest_per_entity: message.latestPerEntity,
        };
        const group = message.requestKey ? `replayDatasetRows:${message.requestKey}` : "replayDatasetRows:default";
        const data = await fetchJson("replay dataset rows", `/datasets/${encodeURIComponent(message.datasetId)}/rows`, query, send, group);
        if (data) send({ type: "replayDatasetRows", data, requestKey: message.requestKey });
        break;
      }
      case "error":
        notify(`Replay error: ${message.message}`);
        break;
      default:
        break;
    }
  };
}

// ----------------------------------------------------------- file-backed editors

async function readText(projectId: string, path: string): Promise<string | null> {
  try {
    const file = await api.get<{ content: string }>(sbx(projectId, "files/content"), { path });
    return file.content;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

function writeText(projectId: string, path: string, content: string) {
  return api.put(sbx(projectId, "files/content"), { path, content });
}

function parseJson(text: string | null): any {
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

/** Port of InitConfigEditorProvider: edits ``<experiment>/init/init_config.json``. */
export function initConfigHandler(projectId: string, path: string, onSaved: (text: string) => void): Handler {
  return async (message, send) => {
    if (message.command === "requestData") {
      send({ command: "initialData", config: parseJson(await readText(projectId, path)) });
    } else if (message.command === "save") {
      await writeText(projectId, path, String(message.content ?? ""));
      onSaved(String(message.content ?? ""));
      send({ command: "update", config: parseJson(String(message.content ?? "")) });
    }
  };
}

/** Port of SimSettingsEditorProvider: edits ``hypothesis_<id>/SIM_SETTINGS.json``. */
export function simSettingsHandler(projectId: string, path: string, onSaved: (text: string) => void): Handler {
  return async (message, send) => {
    if (message.command === "requestData") {
      const settings = parseJson(await readText(projectId, path));
      let agentClasses: Record<string, unknown> = {};
      let envModules: Record<string, unknown> = {};
      try {
        const r = await api.get<{ success: boolean; agents: Record<string, unknown> }>(as2(projectId, "api/v1/modules/agent_classes"));
        if (r.success) agentClasses = r.agents;
      } catch {
        /* the webview shows an empty list, as upstream does */
      }
      try {
        const r = await api.get<{ success: boolean; modules: Record<string, unknown> }>(as2(projectId, "api/v1/modules/env_module_classes"));
        if (r.success) envModules = r.modules;
      } catch {
        /* as above */
      }
      send({ command: "initialData", settings, agentClasses, envModules });
    } else if (message.command === "save") {
      const content = String(message.content ?? "");
      await writeText(projectId, path, content);
      onSaved(content);
      send({ command: "update", text: content });
    }
  };
}

// --------------------------------------------------------------- module catalog

export const PREFILL_PATH = ".agentsociety/prefill_params.json";

async function findClassSource(projectId: string, kind: string, className: string): Promise<string | null> {
  const root = `custom/${kind === "agent" ? "agents" : "envs"}`;
  let tree: { entries: FileEntry[] };
  try {
    tree = await api.get<{ entries: FileEntry[] }>(sbx(projectId, "files/tree"), { path: root, depth: 6 });
  } catch {
    return null;
  }
  const files: string[] = [];
  const walk = (entries: FileEntry[]) =>
    entries.forEach((e) => {
      if (e.type === "directory" && e.name !== "examples" && e.name !== "__pycache__") walk(e.children ?? []);
      else if (e.type === "file" && e.name.endsWith(".py") && !e.name.startsWith("__")) files.push(e.path);
    });
  walk(tree.entries);
  const pattern = new RegExp(`^class\\s+${className.replace(/[^A-Za-z0-9_]/g, "")}\\b`, "m");
  for (const file of files) {
    const text = await readText(projectId, file).catch(() => null);
    if (text && pattern.test(text)) return file;
  }
  return null;
}

/** Port of PrefillParamsViewProvider (the module catalog with pre-filled parameters). */
export function prefillParamsHandler(
  projectId: string,
  hooks: { openFile: (path: string) => void; openSettings: () => void; notify: (text: string, kind?: "success" | "warning" | "error") => void },
): Handler {
  const requestData = async (send: Reply) => {
    try {
      const classes = await api.get(as2(projectId, "api/v1/custom/classes"));
      const prefill = await api.get<{ data: unknown }>(as2(projectId, "api/v1/prefill-params"));
      send({ command: "initialData", classes, prefillParams: prefill.data });
    } catch (error) {
      const text = errorMessage(error);
      send({ command: "error", error: text, backendOffline: error instanceof ApiError && (error.status === 502 || error.status === 503 || error.status === 409) });
    }
  };

  return async (message, send) => {
    switch (message.command) {
      case "requestData":
      case "refresh":
        await requestData(send);
        break;
      case "testCustomModule": {
        try {
          const r = await api.post<{ success: boolean; results?: { success: boolean; output?: string; error?: string }[]; test_output?: string; error?: string }>(
            as2(projectId, "api/v1/custom/test"),
            { module_kind: message.moduleType, module_class_name: message.moduleClassName },
          );
          const first = r.results?.[0];
          send({
            command: "testResult",
            moduleKey: message.moduleKey,
            success: first ? first.success : !!r.success,
            output: first ? (first.output ?? "") : (r.test_output ?? ""),
            error: first ? first.error : (r.error ?? `No test result for "${message.moduleClassName}"`),
          });
        } catch (error) {
          send({ command: "testResult", moduleKey: message.moduleKey, success: false, error: errorMessage(error), output: "" });
        }
        break;
      }
      case "saveClassPrefill": {
        const kind = message.kind === "agent" ? "agent" : message.kind === "env_module" ? "env_module" : "";
        const typeName = typeof message.type === "string" ? message.type.trim() : "";
        if (!kind || !typeName) {
          send({ command: "saveResult", success: false, error: "Invalid target" });
          break;
        }
        const params = message.params && typeof message.params === "object" && !Array.isArray(message.params) ? message.params : {};
        try {
          const raw = parseJson(await readText(projectId, PREFILL_PATH));
          const data = {
            version: typeof raw.version === "string" ? raw.version : "1.0",
            env_modules: raw.env_modules && typeof raw.env_modules === "object" && !Array.isArray(raw.env_modules) ? raw.env_modules : {},
            agents: raw.agents && typeof raw.agents === "object" && !Array.isArray(raw.agents) ? raw.agents : {},
          };
          const bucket = kind === "env_module" ? data.env_modules : data.agents;
          if (Object.keys(params).length === 0) delete bucket[typeName];
          else bucket[typeName] = params;
          await writeText(projectId, PREFILL_PATH, `${JSON.stringify(data, null, 2)}\n`);
          send({ command: "saveResult", success: true, kind, type: typeName, params });
        } catch (error) {
          send({ command: "saveResult", success: false, kind, type: typeName, error: errorMessage(error) });
        }
        break;
      }
      case "openPrefillParamsJson": {
        if ((await readText(projectId, PREFILL_PATH)) === null) {
          await writeText(projectId, PREFILL_PATH, `${JSON.stringify({ version: "1.0", env_modules: {}, agents: {} }, null, 2)}\n`);
        }
        hooks.openFile(PREFILL_PATH);
        break;
      }
      case "openCustomModuleSource": {
        const className = typeof message.className === "string" ? message.className.trim() : "";
        if (!className) break;
        const file = await findClassSource(projectId, message.kind, className);
        if (file) hooks.openFile(file);
        else hooks.notify(`Could not find the source of ${className} under custom/`, "warning");
        break;
      }
      case "startBackend": {
        try {
          await api.post(sbx(projectId, "backend/restart"));
          send({ command: "startBackendResult", success: true });
        } catch (error) {
          send({ command: "startBackendResult", success: false, error: errorMessage(error) });
        }
        break;
      }
      case "openConfigPage":
        hooks.openSettings();
        break;
      case "copyText":
        if (typeof message.text === "string" && message.text) await navigator.clipboard?.writeText(message.text).catch(() => undefined);
        break;
      default:
        break;
    }
  };
}
