import {
  BulbOutlined,
  CheckCircleFilled,
  CloseCircleFilled,
  CodeOutlined,
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
  GlobalOutlined,
  LoadingOutlined,
  PlusOutlined,
  RobotOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  ToolOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Sender, Welcome } from "@ant-design/x";
import { MarkdownRenderer } from "@upstream/components/MarkdownRenderer";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Avatar, Button, Collapse, Empty, Flex, Popconfirm, Select, Space, Tag, Tooltip, Typography } from "antd";
import * as React from "react";
import { useNavigate, useParams } from "react-router-dom";

import { api, ApiError, sbx, streamSSE } from "@/api/client";
import type { ChatEvent, ChatSession, ModelRoute } from "@/api/types";
import { useAuth } from "@/auth";
import { fromNow, usd } from "@/lib/format";

import { buildThread, describeTool, type ThreadItem } from "./chatModel";
import { SandboxGate, useProjectCtx } from "./context";

const PERMISSION_MODES = [
  { value: "bypassPermissions", label: "Autonomous" },
  { value: "acceptEdits", label: "Accept edits" },
  { value: "plan", label: "Plan only" },
];

const STARTERS = [
  "Run the full research pipeline for TOPIC.md and report after each stage.",
  "Search the literature for my research question and summarise the key findings.",
  "Design an experiment with 20 agents to test my first hypothesis, then run it.",
  "Create a custom environment module for a small online community with posting and likes.",
];

const TOOL_ICONS: Record<string, React.ReactNode> = {
  Bash: <CodeOutlined />,
  Read: <FileTextOutlined />,
  Write: <EditOutlined />,
  Edit: <EditOutlined />,
  Grep: <SearchOutlined />,
  Glob: <SearchOutlined />,
  WebSearch: <GlobalOutlined />,
  WebFetch: <GlobalOutlined />,
  Skill: <ThunderboltOutlined />,
};

function useChatModels() {
  const { org } = useAuth();
  return useQuery({
    queryKey: ["models", org?.id],
    queryFn: () => api.get<{ models: ModelRoute[] }>(`/api/v1/orgs/${org!.id}/models`),
    enabled: !!org,
    select: (d) => d.models.filter((m) => m.kind === "chat" && m.enabled),
  });
}

function ToolCard({ item, onOpenFile }: { item: Extract<ThreadItem, { type: "tool" }>; onOpenFile: (path: string) => void }) {
  const summary = describeTool(item.name, item.input);
  const filePath = typeof item.input.file_path === "string" ? item.input.file_path : null;
  const status = item.partial || !item.result ? <LoadingOutlined /> : item.result.isError ? <CloseCircleFilled style={{ color: "#d1242f" }} /> : <CheckCircleFilled style={{ color: "#1a7f37" }} />;
  const details: React.ReactNode[] = [];
  if (item.name === "Bash" && typeof item.input.command === "string") details.push(<pre key="cmd" className="mono">$ {item.input.command}</pre>);
  else if (item.name === "Edit" || item.name === "Write") {
    const text = String(item.input.new_string ?? item.input.content ?? "");
    if (text) details.push(<pre key="body" className="mono">{text.length > 4000 ? `${text.slice(0, 4000)}\n…` : text}</pre>);
  } else if (Object.keys(item.input).length) details.push(<pre key="input" className="mono">{JSON.stringify(item.input, null, 2)}</pre>);
  if (item.result?.text) {
    const text = item.result.text;
    details.push(
      <pre key="result" className="mono" style={{ color: item.result.isError ? "#d1242f" : undefined, borderTop: "1px solid var(--app-border)", paddingTop: 8, marginTop: 8 }}>
        {text.length > 8000 ? `${text.slice(0, 8000)}\n… (${text.length - 8000} more characters)` : text}
      </pre>,
    );
  }
  return (
    <div className="chat-tool">
      <Collapse
        ghost
        size="small"
        items={[
          {
            key: "t",
            label: (
              <Space size={8} style={{ fontSize: 13 }}>
                {status}
                {TOOL_ICONS[item.name] ?? <ToolOutlined />}
                <Typography.Text strong>{item.name}</Typography.Text>
                <Typography.Text type="secondary" ellipsis style={{ maxWidth: 560 }}>
                  {summary}
                </Typography.Text>
                {filePath && (
                  <Button
                    size="small"
                    type="link"
                    onClick={(e) => {
                      e.stopPropagation();
                      onOpenFile(filePath);
                    }}
                  >
                    open
                  </Button>
                )}
              </Space>
            ),
            children: details.length ? details : <Typography.Text type="secondary">No output</Typography.Text>,
          },
        ]}
      />
    </div>
  );
}

function ThreadView({ items, onOpenFile }: { items: ThreadItem[]; onOpenFile: (path: string) => void }) {
  return (
    <>
      {items.map((item) => {
        switch (item.type) {
          case "user":
            return (
              <Flex key={item.key} justify="end" gap={8}>
                <div style={{ background: "#1f4fd8", color: "#fff", padding: "10px 14px", borderRadius: 12, maxWidth: "75%", whiteSpace: "pre-wrap" }}>{item.text}</div>
                <Avatar icon={<UserOutlined />} />
              </Flex>
            );
          case "assistant":
            return (
              <Flex key={item.key} gap={8} align="start">
                <Avatar style={{ background: "#0f172a", flex: "none" }} icon={<RobotOutlined />} />
                <div style={{ background: "#fff", border: "1px solid var(--app-border)", padding: "8px 14px", borderRadius: 12, maxWidth: "85%", minWidth: 0 }}>
                  {item.partial ? <div style={{ whiteSpace: "pre-wrap" }}>{item.text}▋</div> : <MarkdownRenderer content={item.text} isDark={false} />}
                </div>
              </Flex>
            );
          case "thinking":
            return (
              <div key={item.key} style={{ marginLeft: 40 }}>
                <Collapse size="small" ghost items={[{ key: "k", label: <Typography.Text type="secondary"><BulbOutlined /> Thinking{item.partial ? "…" : ""}</Typography.Text>, children: <Typography.Paragraph type="secondary" style={{ whiteSpace: "pre-wrap" }}>{item.text}</Typography.Paragraph> }]} />
              </div>
            );
          case "tool":
            return (
              <div key={item.key} style={{ marginLeft: 40 }}>
                <ToolCard item={item} onOpenFile={onOpenFile} />
              </div>
            );
          case "result":
            return (
              <Flex key={item.key} justify="center">
                <Tag color={item.ok ? "green" : "red"}>
                  {item.ok ? "Done" : `Stopped: ${item.text}`}
                  {item.durationMs != null && ` · ${(item.durationMs / 1000).toFixed(1)}s`}
                  {item.turns != null && ` · ${item.turns} steps`}
                  {item.costUsd != null && ` · ${usd(item.costUsd)}`}
                </Tag>
              </Flex>
            );
          case "error":
            return (
              <Flex key={item.key} justify="center">
                <Tag color="red">{item.text}</Tag>
              </Flex>
            );
          default:
            return null;
        }
      })}
    </>
  );
}

function useSessionEvents(projectId: string, sessionId: string | undefined) {
  const [events, setEvents] = React.useState<ChatEvent[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [following, setFollowing] = React.useState(0);
  const queryClient = useQueryClient();
  const seen = React.useRef(new Set<number>());

  const add = React.useCallback((incoming: ChatEvent[]) => {
    const fresh = incoming.filter((e) => !seen.current.has(e.seq));
    if (!fresh.length) return;
    fresh.forEach((e) => seen.current.add(e.seq));
    setEvents((prev) => [...prev, ...fresh].sort((a, b) => a.seq - b.seq));
  }, []);

  React.useEffect(() => {
    seen.current = new Set();
    setEvents([]);
    if (!sessionId) return;
    let cancelled = false;
    setLoading(true);
    api
      .get<{ events: ChatEvent[]; status: string }>(sbx(projectId, `chat/sessions/${sessionId}/events`), { after: 0, limit: 20000 })
      .then((r) => {
        if (cancelled) return;
        add(r.events);
        if (r.status === "running") setFollowing((n) => n + 1);
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [projectId, sessionId, add]);

  // Follow the live stream while a turn runs; resumes from the last event after drops.
  React.useEffect(() => {
    if (!sessionId || following === 0) return;
    const controller = new AbortController();
    let attempts = 0;
    const follow = async () => {
      while (!controller.signal.aborted && attempts < 20) {
        const after = Math.max(0, ...seen.current);
        let idle = false;
        try {
          await streamSSE(
            sbx(projectId, `chat/sessions/${sessionId}/stream`),
            (e) => {
              if (e.event === "idle") {
                idle = true;
                return;
              }
              try {
                add([JSON.parse(e.data) as ChatEvent]);
                attempts = 0;
              } catch {
                /* keep-alive or partial */
              }
            },
            controller.signal,
            { after },
          );
        } catch (err) {
          if (controller.signal.aborted) return;
          if (err instanceof ApiError && err.status < 500 && err.status !== 409) return;
        }
        if (idle) break;
        attempts += 1;
        await new Promise((r) => setTimeout(r, Math.min(1000 * attempts, 5000)));
      }
      void queryClient.invalidateQueries({ queryKey: ["chat-sessions", projectId] });
    };
    void follow();
    return () => controller.abort();
  }, [projectId, sessionId, following, add, queryClient]);

  return { events, loading, follow: () => setFollowing((n) => n + 1) };
}

function ChatInner() {
  const { project, canWrite } = useProjectCtx();
  const { sessionId: routeSession } = useParams();
  const sessionId = routeSession === "new" ? undefined : routeSession;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const models = useChatModels();
  const [draft, setDraft] = React.useState("");
  const [model, setModel] = React.useState<string | undefined>(project.settings.chat_model);
  const [mode, setMode] = React.useState("bypassPermissions");
  const bottom = React.useRef<HTMLDivElement>(null);

  const sessions = useQuery({
    queryKey: ["chat-sessions", project.id],
    queryFn: () => api.get<{ sessions: ChatSession[] }>(sbx(project.id, "chat/sessions")),
    refetchInterval: 10_000,
  });
  const { events, loading, follow } = useSessionEvents(project.id, sessionId);
  const thread = React.useMemo(() => buildThread(events), [events]);

  React.useEffect(() => {
    // Plain /chat opens the most recent conversation; /chat/new starts an empty one.
    if (!routeSession && sessions.data?.sessions.length) navigate(`/p/${project.id}/chat/${sessions.data.sessions[0].id}`, { replace: true });
  }, [routeSession, sessions.data, navigate, project.id]);

  React.useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [thread.items.length, thread.items[thread.items.length - 1]]);

  const send = async (content: string) => {
    const text = content.trim();
    if (!text) return;
    try {
      let sid = sessionId;
      if (!sid) {
        const session = await api.post<ChatSession>(sbx(project.id, "chat/sessions"), { title: text.slice(0, 80) });
        sid = session.id;
        navigate(`/p/${project.id}/chat/${sid}`);
      }
      await api.post(sbx(project.id, `chat/sessions/${sid}/messages`), { content: text, model, permission_mode: mode });
      setDraft("");
      follow();
      void queryClient.invalidateQueries({ queryKey: ["chat-sessions", project.id] });
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not send the message");
    }
  };

  const interrupt = async () => {
    if (sessionId) await api.post(sbx(project.id, `chat/sessions/${sessionId}/interrupt`));
  };

  const remove = async (sid: string) => {
    await api.delete(sbx(project.id, `chat/sessions/${sid}`));
    await queryClient.invalidateQueries({ queryKey: ["chat-sessions", project.id] });
    if (sid === sessionId) navigate(`/p/${project.id}/chat`);
  };

  const openFile = (path: string) => navigate(`/p/${project.id}/files?path=${encodeURIComponent(path.replace(/^\/data\/workspace\/?/, "").replace(/^.*\/workspace\//, ""))}`);

  return (
    <div style={{ display: "flex", height: "100%" }}>
      <div style={{ width: 260, borderRight: "1px solid var(--app-border)", background: "#fff", display: "flex", flexDirection: "column" }}>
        <div style={{ padding: 12 }}>
          <Button block icon={<PlusOutlined />} onClick={() => navigate(`/p/${project.id}/chat/new`)} disabled={!canWrite}>
            New chat
          </Button>
        </div>
        <div style={{ overflowY: "auto", flex: 1 }}>
          {(sessions.data?.sessions ?? []).map((s) => (
            <div
              key={s.id}
              onClick={() => navigate(`/p/${project.id}/chat/${s.id}`)}
              style={{ padding: "8px 12px", cursor: "pointer", background: s.id === sessionId ? "var(--vscode-list-activeSelectionBackground)" : undefined, display: "flex", gap: 8, alignItems: "center" }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <Typography.Text ellipsis style={{ display: "block" }}>
                  {s.status === "running" && <LoadingOutlined style={{ marginRight: 6 }} />}
                  {s.title}
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {fromNow(s.updated_at)} · {s.turns} turn{s.turns === 1 ? "" : "s"}
                </Typography.Text>
              </div>
              {canWrite && (
                <Popconfirm title="Delete this chat?" onConfirm={(e) => { e?.stopPropagation(); void remove(s.id); }} onCancel={(e) => e?.stopPropagation()}>
                  <Button size="small" type="text" icon={<DeleteOutlined />} onClick={(e) => e.stopPropagation()} aria-label="Delete chat" />
                </Popconfirm>
              )}
            </div>
          ))}
          {sessions.data?.sessions.length === 0 && <Empty style={{ marginTop: 40 }} image={Empty.PRESENTED_IMAGE_SIMPLE} description="No chats yet" />}
        </div>
      </div>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, background: "var(--app-bg)" }}>
        <div className="chat-thread" style={{ flex: 1 }}>
          {!sessionId || (!loading && thread.items.length === 0) ? (
            <div style={{ maxWidth: 760, margin: "40px auto", width: "100%" }}>
              <Welcome
                icon={<Avatar size={48} style={{ background: "#1f4fd8" }} icon={<RobotOutlined />} />}
                title="AI Social Scientist"
                description="Your research assistant runs inside this workspace with every AgentSociety skill: literature search, hypothesis design, experiment configuration, simulation, analysis and paper writing. Ask in plain language."
              />
              <Flex vertical gap={8} style={{ marginTop: 16 }}>
                {STARTERS.map((s) => (
                  <Button key={s} style={{ justifyContent: "flex-start", height: "auto", whiteSpace: "normal", textAlign: "left", padding: "8px 12px" }} onClick={() => setDraft(s)} disabled={!canWrite}>
                    {s}
                  </Button>
                ))}
              </Flex>
            </div>
          ) : (
            <div style={{ maxWidth: 980, width: "100%", margin: "0 auto", display: "flex", flexDirection: "column", gap: 12 }}>
              <ThreadView items={thread.items} onOpenFile={openFile} />
              {thread.running && (
                <Flex gap={8} style={{ marginLeft: 40 }}>
                  <LoadingOutlined /> <Typography.Text type="secondary">Working…</Typography.Text>
                </Flex>
              )}
              <div ref={bottom} />
            </div>
          )}
        </div>
        {canWrite && (
          <div style={{ padding: "12px 20px 16px", background: "#fff", borderTop: "1px solid var(--app-border)" }}>
            <div style={{ maxWidth: 980, margin: "0 auto" }}>
              <Sender
                value={draft}
                onChange={(v) => setDraft(v)}
                onSubmit={(v) => void send(v)}
                loading={thread.running}
                onCancel={() => void interrupt()}
                placeholder="Ask the AI social scientist… (Shift+Enter for a new line)"
                autoSize={{ minRows: 2, maxRows: 10 }}
                footer={
                  <Flex gap={8} align="center" style={{ fontSize: 12 }}>
                    <Select
                      size="small"
                      value={model}
                      allowClear
                      placeholder="Default model"
                      style={{ minWidth: 200 }}
                      onChange={setModel}
                      options={(models.data ?? []).map((m) => ({ value: m.alias, label: m.description || m.alias }))}
                      aria-label="Model"
                    />
                    <Tooltip title="Autonomous: the assistant runs commands and edits files on its own. Plan only: it proposes a plan without changing anything.">
                      <Select size="small" value={mode} onChange={setMode} options={PERMISSION_MODES} style={{ width: 140 }} aria-label="Permission mode" />
                    </Tooltip>
                    {thread.model && <Typography.Text type="secondary">model: {thread.model}</Typography.Text>}
                  </Flex>
                }
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function Chat() {
  return (
    <div style={{ height: "100%" }}>
      <SandboxGate what="the AI social scientist">
        <ChatInner />
      </SandboxGate>
    </div>
  );
}
