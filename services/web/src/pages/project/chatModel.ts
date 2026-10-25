/**
 * Turns the sandbox's chat events (Claude Code ``stream-json`` lines wrapped by
 * sandboxd) into a renderable thread: user turns, assistant text, thinking, tool
 * calls paired with their results, and per-turn results.
 */

import type { ChatEvent } from "@/api/types";

export type ThreadItem =
  | { type: "user"; key: string; text: string }
  | { type: "assistant"; key: string; text: string; partial?: boolean }
  | { type: "thinking"; key: string; text: string; partial?: boolean }
  | { type: "tool"; key: string; id: string; name: string; input: Record<string, unknown>; result?: ToolResult; partial?: boolean }
  | { type: "result"; key: string; ok: boolean; text: string; costUsd?: number; durationMs?: number; turns?: number }
  | { type: "error"; key: string; text: string }
  | { type: "system"; key: string; text: string };

export interface ToolResult {
  text: string;
  isError: boolean;
}

export interface Thread {
  items: ThreadItem[];
  running: boolean;
  model?: string;
  lastSeq: number;
}

function contentText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((block) => {
        if (typeof block === "string") return block;
        if (block && typeof block === "object") {
          const b = block as { type?: string; text?: string; source?: unknown };
          if (b.type === "text") return b.text ?? "";
          if (b.type === "image") return "[image]";
          if (b.type === "tool_reference") return "";
        }
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return content == null ? "" : JSON.stringify(content);
}

interface Partial {
  messageId: string;
  blocks: Map<number, { type: string; text: string; name?: string; id?: string }>;
}

export function buildThread(events: ChatEvent[]): Thread {
  const items: ThreadItem[] = [];
  const tools = new Map<string, Extract<ThreadItem, { type: "tool" }>>();
  const finalized = new Map<string, number>(); // message id -> blocks seen as final
  let partial: Partial | null = null;
  let running = false;
  let model: string | undefined;
  let lastSeq = 0;

  for (const event of events) {
    lastSeq = Math.max(lastSeq, event.seq);
    const k = `e${event.seq}`;
    if (event.kind === "user") {
      items.push({ type: "user", key: k, text: String(event.data?.content ?? "") });
      running = true;
      partial = null;
      continue;
    }
    if (event.kind === "error") {
      items.push({ type: "error", key: k, text: String(event.data?.message ?? "Error") });
      continue;
    }
    if (event.kind === "turn_end") {
      running = false;
      partial = null;
      continue;
    }
    if (event.kind !== "claude") continue;
    const d = event.data ?? {};
    switch (d.type) {
      case "system":
        if (d.subtype === "init") model = d.model;
        break;
      case "stream_event": {
        const se = d.event ?? {};
        if (se.type === "message_start") {
          partial = { messageId: se.message?.id ?? k, blocks: new Map() };
        } else if (partial && se.type === "content_block_start") {
          const cb = se.content_block ?? {};
          partial.blocks.set(se.index, { type: cb.type, text: cb.type === "text" ? (cb.text ?? "") : "", name: cb.name, id: cb.id });
        } else if (partial && se.type === "content_block_delta") {
          const block = partial.blocks.get(se.index);
          const delta = se.delta ?? {};
          if (block) {
            if (delta.type === "text_delta") block.text += delta.text ?? "";
            else if (delta.type === "thinking_delta") block.text += delta.thinking ?? "";
            else if (delta.type === "input_json_delta") block.text += delta.partial_json ?? "";
          }
        }
        break;
      }
      case "assistant": {
        const message = d.message ?? {};
        const id = message.id ?? k;
        (message.content ?? []).forEach((block: any, i: number) => {
          const key = `${k}-${i}`;
          if (block.type === "text" && block.text) items.push({ type: "assistant", key, text: block.text });
          else if (block.type === "thinking" && block.thinking) items.push({ type: "thinking", key, text: block.thinking });
          else if (block.type === "tool_use") {
            const tool = { type: "tool" as const, key, id: block.id, name: block.name, input: block.input ?? {} };
            tools.set(block.id, tool);
            items.push(tool);
          }
        });
        finalized.set(id, (finalized.get(id) ?? 0) + (message.content?.length ?? 0));
        break;
      }
      case "user": {
        const content = d.message?.content;
        if (Array.isArray(content)) {
          for (const block of content) {
            if (block?.type === "tool_result") {
              const tool = tools.get(block.tool_use_id);
              const result = { text: contentText(block.content), isError: !!block.is_error };
              if (tool) tool.result = result;
            }
          }
        }
        break;
      }
      case "result":
        items.push({
          type: "result",
          key: k,
          ok: d.subtype === "success" && !d.is_error,
          text: d.subtype === "success" ? "" : String(d.result ?? d.subtype ?? ""),
          costUsd: d.total_cost_usd,
          durationMs: d.duration_ms,
          turns: d.num_turns,
        });
        break;
      default:
        break;
    }
  }

  // The block being streamed right now (not yet emitted as a final assistant event).
  if (running && partial) {
    const done = finalized.get(partial.messageId) ?? 0;
    [...partial.blocks.entries()]
      .filter(([index]) => index >= done)
      .sort(([a], [b]) => a - b)
      .forEach(([index, block]) => {
        const key = `partial-${partial!.messageId}-${index}`;
        if (block.type === "text" && block.text) items.push({ type: "assistant", key, text: block.text, partial: true });
        else if (block.type === "thinking" && block.text) items.push({ type: "thinking", key, text: block.text, partial: true });
        else if (block.type === "tool_use") items.push({ type: "tool", key, id: block.id ?? key, name: block.name ?? "tool", input: {}, partial: true });
      });
  }
  return { items, running, model, lastSeq };
}

/** A one-line description of a tool call for the thread. */
export function describeTool(name: string, input: Record<string, unknown>): string {
  const s = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));
  switch (name) {
    case "Bash":
      return s(input.description) || s(input.command);
    case "Read":
    case "Write":
    case "Edit":
    case "NotebookEdit":
      return s(input.file_path ?? input.notebook_path);
    case "Grep":
    case "Glob":
      return s(input.pattern);
    case "WebSearch":
      return s(input.query);
    case "WebFetch":
      return s(input.url);
    case "Skill":
      return s(input.skill ?? input.name ?? input.command);
    case "Task":
    case "Agent":
      return s(input.description ?? input.prompt);
    case "TodoWrite":
      return "Update plan";
    default:
      return Object.keys(input).length ? s(Object.values(input)[0]).slice(0, 120) : "";
  }
}
