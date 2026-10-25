/**
 * HTTP client for the control plane.
 *
 * The access token lives only in memory; on load and whenever it expires the client
 * exchanges the HttpOnly refresh cookie (path /api/v1/auth) for a new one. All
 * sandbox calls go through the authenticated proxy at /api/v1/projects/{id}/sbx/.
 */

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public body?: unknown,
  ) {
    super(message);
  }
}

export interface Session {
  access_token: string;
  expires_in: number;
  user: import("./types").User;
  orgs: import("./types").OrgSummary[];
}

type Listener = (session: Session | null) => void;

let accessToken: string | null = null;
let refreshing: Promise<Session | null> | null = null;
const listeners = new Set<Listener>();

export function onSession(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function setSession(session: Session | null): void {
  accessToken = session?.access_token ?? null;
  listeners.forEach((l) => l(session));
}

export function getAccessToken(): string | null {
  return accessToken;
}

/** Exchange the refresh cookie for a fresh session (single flight). */
export function refreshSession(): Promise<Session | null> {
  if (!refreshing) {
    refreshing = fetch("/api/v1/auth/refresh", { method: "POST", credentials: "same-origin" })
      .then(async (r) => {
        const session = r.ok ? ((await r.json()) as Session) : null;
        setSession(session);
        return session;
      })
      .catch(() => {
        setSession(null);
        return null;
      })
      .finally(() => {
        refreshing = null;
      });
  }
  return refreshing;
}

export type Query = Record<string, string | number | boolean | undefined | null>;

export function withQuery(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  Object.entries(query).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  });
  const qs = params.toString();
  return qs ? `${path}${path.includes("?") ? "&" : "?"}${qs}` : path;
}

export interface RequestOptions {
  method?: string;
  json?: unknown;
  body?: BodyInit;
  headers?: Record<string, string>;
  query?: Query;
  signal?: AbortSignal;
  /** Return the raw Response instead of parsed JSON. */
  raw?: boolean;
}

async function errorFrom(r: Response): Promise<ApiError> {
  let body: unknown = undefined;
  let message = `${r.status} ${r.statusText}`;
  try {
    body = await r.json();
    const detail = (body as { detail?: unknown; error?: { message?: string } }).detail ?? (body as { error?: { message?: string } }).error?.message;
    if (typeof detail === "string") message = detail;
    else if (Array.isArray(detail)) message = detail.map((d) => d?.msg ?? JSON.stringify(d)).join("; ");
  } catch {
    /* not JSON */
  }
  return new ApiError(r.status, message, body);
}

export async function request<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = withQuery(path, opts.query);
  const send = () => {
    const headers: Record<string, string> = { ...(opts.headers ?? {}) };
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    let body = opts.body;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.json);
    }
    return fetch(url, { method: opts.method ?? (body ? "POST" : "GET"), headers, body, signal: opts.signal, credentials: "same-origin" });
  };
  let r = await send();
  if (r.status === 401 && !path.startsWith("/api/v1/auth/")) {
    if (await refreshSession()) r = await send();
  }
  if (!r.ok) throw await errorFrom(r);
  if (opts.raw) return r as unknown as T;
  if (r.status === 204) return undefined as T;
  const type = r.headers.get("content-type") ?? "";
  return (type.includes("json") ? r.json() : r.text()) as Promise<T>;
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) => request<T>(path, { query, signal }),
  post: <T>(path: string, json?: unknown, query?: Query) => request<T>(path, { method: "POST", json: json ?? {}, query }),
  put: <T>(path: string, json?: unknown, query?: Query) => request<T>(path, { method: "PUT", json: json ?? {}, query }),
  patch: <T>(path: string, json?: unknown) => request<T>(path, { method: "PATCH", json: json ?? {} }),
  delete: <T>(path: string, query?: Query) => request<T>(path, { method: "DELETE", query }),
};

/** Paths inside a project's sandbox (files, experiments, chat, tools, upstream backend). */
export function sbx(projectId: string, path: string): string {
  return `/api/v1/projects/${projectId}/sbx/${path.replace(/^\//, "")}`;
}

/** Calls to the upstream agentsociety2 backend running in the sandbox. */
export function as2(projectId: string, path: string): string {
  return sbx(projectId, `as2/${path.replace(/^\//, "")}`);
}

export interface SSEEvent {
  event: string;
  data: string;
  id?: string;
}

/**
 * Server-sent events over fetch (EventSource cannot send an Authorization header).
 * Resolves when the stream ends; rejects on HTTP errors.
 */
export async function streamSSE(path: string, onEvent: (e: SSEEvent) => void, signal: AbortSignal, query?: Query): Promise<void> {
  const r = await request<Response>(path, { query, signal, raw: true, headers: { Accept: "text/event-stream" } });
  if (!r.body) return;
  const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let event: SSEEvent = { event: "message", data: "" };
  const flush = () => {
    if (event.data !== "" || event.event !== "message") onEvent({ ...event, data: event.data.replace(/\n$/, "") });
    event = { event: "message", data: "" };
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let index: number;
    while ((index = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, "");
      buffer = buffer.slice(index + 1);
      if (line === "") flush();
      else if (line.startsWith(":")) continue;
      else {
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        const val = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "event") event.event = val;
        else if (field === "data") event.data += `${val}\n`;
        else if (field === "id") event.id = val;
      }
    }
  }
  flush();
}

/** Download an authenticated resource as a file. */
export async function downloadFile(path: string, filename: string, query?: Query): Promise<void> {
  const r = await request<Response>(path, { query, raw: true });
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** Fetch an authenticated resource and return an object URL (for iframes and images). */
export async function blobUrl(path: string, query?: Query): Promise<string> {
  const r = await request<Response>(path, { query, raw: true });
  return URL.createObjectURL(await r.blob());
}
