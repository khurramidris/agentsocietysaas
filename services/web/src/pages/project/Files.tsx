import "@/lib/monaco";

import {
  CloudDownloadOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeInvisibleOutlined,
  EyeOutlined,
  FileAddOutlined,
  FolderAddOutlined,
  ReloadOutlined,
  SaveOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import Editor from "@monaco-editor/react";
import { MarkdownRenderer } from "@upstream/components/MarkdownRenderer";
import { App, Breadcrumb, Button, Empty, Flex, Input, Modal, Popconfirm, Result, Segmented, Space, Spin, Table, Tooltip, Tree, Typography, Upload } from "antd";
import type { TreeDataNode } from "antd";
import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { api, ApiError, blobUrl, downloadFile, request, sbx } from "@/api/client";
import type { FileContent, FileEntry } from "@/api/types";
import { bytes, fromNow } from "@/lib/format";
import { languageFor } from "@/lib/monaco";
import { InitConfigView, SimSettingsView } from "@/upstream/embeds";

import { SandboxGate, useProjectCtx } from "./context";

const IMAGE = /\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i;
const PDF = /\.pdf$/i;
const HTML = /\.html?$/i;
const MARKDOWN = /\.(md|markdown)$/i;
const TABLE = /\.(csv|tsv)$/i;
const BINARY = /\.(zip|gz|tgz|tar|parquet|db|sqlite|duckdb|pkl|pickle|npy|npz|xlsx?|docx?|pptx?|mp4|mp3|wav|whl|so|pyc|bin)$/i;

type Node = TreeDataNode & { key: string; entry?: FileEntry };

function toNodes(entries: FileEntry[]): Node[] {
  return entries.map((e) => ({
    key: e.path,
    title: e.name,
    isLeaf: e.type === "file",
    entry: e,
    children: e.type === "directory" && e.children ? toNodes(e.children) : undefined,
  }));
}

function replaceChildren(nodes: Node[], key: string, children: Node[]): Node[] {
  return nodes.map((n) => {
    if (n.key === key) return { ...n, children };
    if (n.children) return { ...n, children: replaceChildren(n.children as Node[], key, children) };
    return n;
  });
}

function findEntry(nodes: Node[], key: string): FileEntry | undefined {
  for (const n of nodes) {
    if (n.key === key) return n.entry;
    if (n.children) {
      const hit = findEntry(n.children as Node[], key);
      if (hit) return hit;
    }
  }
  return undefined;
}

function parent(path: string): string {
  return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
}

function parseTable(text: string, sep: string, limit = 500): { columns: string[]; rows: Record<string, string>[] } {
  const lines = text.split(/\r?\n/).filter((l) => l.length > 0);
  const split = (line: string) => {
    if (sep !== ",") return line.split(sep);
    const out: string[] = [];
    let cur = "";
    let quoted = false;
    for (let i = 0; i < line.length; i += 1) {
      const ch = line[i];
      if (quoted && ch === '"' && line[i + 1] === '"') {
        cur += '"';
        i += 1;
      } else if (ch === '"') quoted = !quoted;
      else if (ch === "," && !quoted) {
        out.push(cur);
        cur = "";
      } else cur += ch;
    }
    out.push(cur);
    return out;
  };
  const columns = lines.length ? split(lines[0]) : [];
  const rows = lines.slice(1, limit + 1).map((line, i) => {
    const cells = split(line);
    return Object.fromEntries([["__key", String(i)], ...columns.map((c, j) => [c || `col${j}`, cells[j] ?? ""])]);
  });
  return { columns, rows };
}

function BlobFrame({ projectId, path, kind }: { projectId: string; path: string; kind: "image" | "pdf" | "html" }) {
  const [url, setUrl] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => {
    let revoked = false;
    let created: string | null = null;
    blobUrl(sbx(projectId, "files/raw"), { path })
      .then((u) => {
        if (revoked) URL.revokeObjectURL(u);
        else {
          created = u;
          setUrl(u);
        }
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      revoked = true;
      if (created) URL.revokeObjectURL(created);
    };
  }, [projectId, path]);
  if (error) return <Result status="warning" title="Could not load the file" subTitle={error} />;
  if (!url) return <Spin style={{ margin: 40 }} />;
  if (kind === "image") return <img src={url} alt={path} style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", margin: "auto", display: "block" }} />;
  // Reports often run scripts (plots); the sandbox keeps them away from this origin.
  return <iframe title={path} src={url} sandbox={kind === "html" ? "allow-scripts allow-popups" : undefined} style={{ width: "100%", height: "100%", border: 0, background: "#fff" }} />;
}

function FileView({ path, onChanged }: { path: string; onChanged: () => void }) {
  const { project, canWrite } = useProjectCtx();
  const { message } = App.useApp();
  const [file, setFile] = React.useState<FileContent | null>(null);
  const [state, setState] = React.useState<"loading" | "text" | "binary" | "large" | "error">("loading");
  const [error, setError] = React.useState("");
  const [draft, setDraft] = React.useState("");
  const [mode, setMode] = React.useState<string>("preview");
  const [saving, setSaving] = React.useState(false);
  const name = path.split("/").pop() ?? path;
  const isInitConfig = /(^|\/)init\/init_config\.json$/.test(path);
  const isSimSettings = /(^|\/)SIM_SETTINGS\.json$/.test(path);
  const hasRich = isInitConfig || isSimSettings || MARKDOWN.test(path) || TABLE.test(path);
  const visual = IMAGE.test(path) ? "image" : PDF.test(path) ? "pdf" : HTML.test(path) ? "html" : null;

  const load = React.useCallback(async () => {
    setState("loading");
    if ((visual === "image" || visual === "pdf") || BINARY.test(path)) {
      setState("binary");
      return;
    }
    try {
      const f = await api.get<FileContent>(sbx(project.id, "files/content"), { path });
      setFile(f);
      setDraft(f.content);
      setState("text");
    } catch (e) {
      if (e instanceof ApiError && e.status === 415) setState("binary");
      else if (e instanceof ApiError && e.status === 413) setState("large");
      else {
        setError(e instanceof ApiError ? e.message : String(e));
        setState("error");
      }
    }
  }, [project.id, path, visual]);

  React.useEffect(() => {
    setMode(hasRich || visual === "html" ? "preview" : "edit");
    void load();
  }, [load, hasRich, visual]);

  const dirty = file !== null && draft !== file.content;
  const save = React.useCallback(async () => {
    if (!file) return;
    setSaving(true);
    try {
      const r = await api.put<{ mtime: number; size: number }>(sbx(project.id, "files/content"), { path, content: draft, expected_mtime: file.mtime });
      setFile({ ...file, content: draft, mtime: r.mtime, size: r.size });
      message.success(`Saved ${name}`);
      onChanged();
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [file, project.id, path, draft, message, name, onChanged]);

  const savedByEditor = React.useCallback(
    (text: string) => {
      setFile((f) => (f ? { ...f, content: text } : f));
      setDraft(text);
      message.success(`Saved ${name}`);
    },
    [message, name],
  );

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s" && dirty && canWrite) {
        e.preventDefault();
        void save();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dirty, save, canWrite]);

  const download = () => downloadFile(sbx(project.id, "files/raw"), name, { path, download: true });

  let body: React.ReactNode;
  if (state === "loading") body = <Spin style={{ margin: 40 }} />;
  else if (state === "error") body = <Result status="warning" title="Could not open the file" subTitle={error} />;
  else if (visual && (state === "binary" || mode === "preview")) body = <BlobFrame projectId={project.id} path={path} kind={visual} />;
  else if (state === "binary" || state === "large")
    body = (
      <Result
        status="info"
        title={state === "large" ? "This file is too large to edit in the browser" : "No preview for this file type"}
        extra={<Button icon={<CloudDownloadOutlined />} onClick={download}>Download</Button>}
      />
    );
  else if (mode === "preview" && isInitConfig) body = <InitConfigView projectId={project.id} path={path} initialConfig={safeJson(file!.content)} onSaved={savedByEditor} />;
  else if (mode === "preview" && isSimSettings) body = <SimSettingsView projectId={project.id} path={path} initialSettings={safeJson(file!.content)} onSaved={savedByEditor} />;
  else if (mode === "preview" && MARKDOWN.test(path))
    body = (
      <div style={{ padding: "16px 24px", overflow: "auto", height: "100%" }}>
        <MarkdownRenderer content={draft} isDark={false} />
      </div>
    );
  else if (mode === "preview" && TABLE.test(path)) {
    const { columns, rows } = parseTable(draft, path.endsWith(".tsv") ? "\t" : ",");
    body = (
      <div style={{ padding: 12, overflow: "auto", height: "100%" }}>
        <Table
          size="small"
          rowKey="__key"
          dataSource={rows}
          pagination={{ pageSize: 50, showSizeChanger: false }}
          scroll={{ x: "max-content" }}
          columns={columns.map((c, j) => ({ title: c, dataIndex: c || `col${j}`, key: `${j}`, ellipsis: true }))}
        />
        {draft.split("\n").length > 502 && <Typography.Text type="secondary">Showing the first 500 rows.</Typography.Text>}
      </div>
    );
  } else
    body = (
      <Editor
        height="100%"
        path={path}
        language={languageFor(path)}
        value={draft}
        onChange={(v) => setDraft(v ?? "")}
        options={{ readOnly: !canWrite, minimap: { enabled: false }, fontSize: 13, wordWrap: languageFor(path) === "markdown" ? "on" : "off", scrollBeyondLastLine: false }}
      />
    );

  const modes = [
    ...(hasRich || visual === "html" ? [{ label: isInitConfig || isSimSettings ? "Visual editor" : "Preview", value: "preview" }] : []),
    ...(state === "text" ? [{ label: "Source", value: "edit" }] : []),
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <Flex justify="space-between" align="center" gap={8} style={{ padding: "8px 12px", borderBottom: "1px solid var(--app-border)", background: "#fff" }}>
        <Space size={8} style={{ minWidth: 0 }}>
          <Typography.Text strong ellipsis>
            {name}
            {dirty ? " •" : ""}
          </Typography.Text>
          {file && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {bytes(file.size)} · modified {fromNow(file.mtime)}
            </Typography.Text>
          )}
        </Space>
        <Space>
          {modes.length > 1 && <Segmented size="small" value={mode} onChange={(v) => setMode(String(v))} options={modes} />}
          {state === "text" && canWrite && mode === "edit" && (
            <Button type="primary" size="small" icon={<SaveOutlined />} disabled={!dirty} loading={saving} onClick={save}>
              Save
            </Button>
          )}
          <Tooltip title="Download">
            <Button size="small" icon={<CloudDownloadOutlined />} onClick={download} />
          </Tooltip>
          <Tooltip title="Reload">
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()} />
          </Tooltip>
        </Space>
      </Flex>
      <div style={{ flex: 1, minHeight: 0, overflow: mode === "preview" && (isInitConfig || isSimSettings) ? "auto" : "hidden", background: "#fff" }}>{body}</div>
    </div>
  );
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

function FilesInner() {
  const { project, canWrite } = useProjectCtx();
  const { message, modal } = App.useApp();
  const [params, setParams] = useSearchParams();
  const selected = params.get("path") ?? "";
  const [nodes, setNodes] = React.useState<Node[]>([]);
  const [expanded, setExpanded] = React.useState<React.Key[]>([]);
  const [showHidden, setShowHidden] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [prompt, setPrompt] = React.useState<{ kind: "file" | "folder" | "rename"; value: string } | null>(null);

  const list = React.useCallback(
    (path: string, depth = 1) => api.get<{ entries: FileEntry[] }>(sbx(project.id, "files/tree"), { path, depth, show_hidden: showHidden }).then((r) => toNodes(r.entries)),
    [project.id, showHidden],
  );

  const loadRoot = React.useCallback(async () => {
    setLoading(true);
    try {
      let root = await list("", 1);
      // Reveal the selected file: load each ancestor directory.
      const ancestors: string[] = [];
      const parts = selected.split("/").filter(Boolean);
      for (let i = 1; i < parts.length; i += 1) ancestors.push(parts.slice(0, i).join("/"));
      for (const dir of ancestors) {
        try {
          root = replaceChildren(root, dir, await list(dir, 1));
        } catch {
          break;
        }
      }
      setNodes(root);
      setExpanded((prev) => Array.from(new Set([...prev.filter((k) => findEntry(root, String(k))), ...ancestors])));
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not list files");
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list]);

  React.useEffect(() => {
    void loadRoot();
  }, [loadRoot]);

  const refreshDir = React.useCallback(
    async (dir: string) => {
      if (!dir) return loadRoot();
      const children = await list(dir, 1);
      setNodes((prev) => replaceChildren(prev, dir, children));
    },
    [list, loadRoot],
  );

  const selectedEntry = findEntry(nodes, selected);
  const selectedIsDir = selectedEntry?.type === "directory";
  const currentDir = selected ? (selectedIsDir ? selected : parent(selected)) : "";

  const select = (path: string) => setParams(path ? { path } : {}, { replace: false });

  const submitPrompt = async () => {
    if (!prompt) return;
    const value = prompt.value.trim().replace(/^\/+/, "");
    if (!value) return;
    try {
      if (prompt.kind === "file") {
        const target = currentDir ? `${currentDir}/${value}` : value;
        await api.put(sbx(project.id, "files/content"), { path: target, content: "" });
        await refreshDir(parent(target));
        select(target);
      } else if (prompt.kind === "folder") {
        const target = currentDir ? `${currentDir}/${value}` : value;
        await api.post(sbx(project.id, "files/mkdir"), { path: target });
        await refreshDir(parent(target));
      } else {
        await api.post(sbx(project.id, "files/move"), { src: selected, dst: value });
        await loadRoot();
        select(value);
      }
      setPrompt(null);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Failed");
    }
  };

  const remove = async () => {
    try {
      await api.delete(sbx(project.id, "files"), { path: selected, recursive: selectedIsDir });
      await refreshDir(parent(selected));
      select(parent(selected));
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Delete failed");
    }
  };

  const upload = async (file: File, overwrite = false): Promise<void> => {
    const form = new FormData();
    form.set("directory", currentDir);
    form.set("overwrite", String(overwrite));
    form.append("files", file, file.name);
    try {
      await request(sbx(project.id, "files/upload"), { method: "POST", body: form });
      message.success(`Uploaded ${file.name}`);
      await refreshDir(currentDir);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && !overwrite) {
        modal.confirm({ title: `${file.name} already exists`, content: "Replace it?", okText: "Replace", onOk: () => upload(file, true) });
      } else message.error(e instanceof ApiError ? e.message : `Could not upload ${file.name}`);
    }
  };

  const crumbs = selected.split("/").filter(Boolean);

  return (
    <div style={{ display: "flex", height: "100%" }}>
      <div style={{ width: 300, borderRight: "1px solid var(--app-border)", display: "flex", flexDirection: "column", background: "#fff" }}>
        <Flex gap={4} style={{ padding: 8, borderBottom: "1px solid var(--app-border)" }} wrap>
          {canWrite && (
            <>
              <Tooltip title="New file">
                <Button size="small" icon={<FileAddOutlined />} onClick={() => setPrompt({ kind: "file", value: "" })} />
              </Tooltip>
              <Tooltip title="New folder">
                <Button size="small" icon={<FolderAddOutlined />} onClick={() => setPrompt({ kind: "folder", value: "" })} />
              </Tooltip>
              <Upload multiple showUploadList={false} customRequest={({ file }) => void upload(file as File)}>
                <Tooltip title={`Upload into /${currentDir}`}>
                  <Button size="small" icon={<UploadOutlined />} />
                </Tooltip>
              </Upload>
            </>
          )}
          <Tooltip title={showHidden ? "Hide hidden files" : "Show hidden files"}>
            <Button size="small" icon={showHidden ? <EyeOutlined /> : <EyeInvisibleOutlined />} onClick={() => setShowHidden((v) => !v)} />
          </Tooltip>
          <Tooltip title="Refresh">
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void loadRoot()} />
          </Tooltip>
          <Tooltip title="Download the workspace as a ZIP">
            <Button size="small" icon={<CloudDownloadOutlined />} onClick={() => void downloadFile(sbx(project.id, "files/archive"), `${project.slug}.zip`, { path: "" })} />
          </Tooltip>
        </Flex>
        <div style={{ flex: 1, overflow: "auto", padding: "4px 0" }}>
          {loading && nodes.length === 0 ? (
            <Spin style={{ margin: 24 }} />
          ) : (
            <Tree.DirectoryTree
              treeData={nodes}
              selectedKeys={selected ? [selected] : []}
              expandedKeys={expanded}
              onExpand={(keys) => setExpanded(keys)}
              expandAction="click"
              loadData={async (node) => {
                const key = String(node.key);
                if ((node as Node).children?.length) return;
                const children = await list(key, 1);
                setNodes((prev) => replaceChildren(prev, key, children));
              }}
              onSelect={(keys) => keys[0] !== undefined && select(String(keys[0]))}
            />
          )}
        </div>
      </div>
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <Flex justify="space-between" align="center" style={{ padding: "6px 12px", borderBottom: "1px solid var(--app-border)", background: "#fafbfc" }}>
          <Breadcrumb
            items={[
              { title: <a onClick={() => select("")}>workspace</a> },
              ...crumbs.map((c, i) => ({ title: <a onClick={() => select(crumbs.slice(0, i + 1).join("/"))}>{c}</a> })),
            ]}
          />
          {selected && canWrite && (
            <Space size={4}>
              <Tooltip title="Rename or move">
                <Button size="small" type="text" icon={<EditOutlined />} onClick={() => setPrompt({ kind: "rename", value: selected })} />
              </Tooltip>
              <Popconfirm title={`Delete ${selectedIsDir ? "this folder and everything in it" : "this file"}?`} okButtonProps={{ danger: true }} onConfirm={remove}>
                <Button size="small" type="text" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            </Space>
          )}
        </Flex>
        <div style={{ flex: 1, minHeight: 0 }}>
          {selected && !selectedIsDir && selectedEntry !== undefined ? (
            <FileView key={selected} path={selected} onChanged={() => void refreshDir(parent(selected))} />
          ) : selected && selectedEntry === undefined && !loading ? (
            <FileView key={selected} path={selected} onChanged={() => void refreshDir(parent(selected))} />
          ) : (
            <Empty style={{ marginTop: 80 }} description={selectedIsDir ? `Folder /${selected} — choose a file, or upload into this folder` : "Choose a file to view or edit"} />
          )}
        </div>
      </div>
      <Modal
        open={!!prompt}
        title={prompt?.kind === "file" ? "New file" : prompt?.kind === "folder" ? "New folder" : "Rename or move"}
        onOk={submitPrompt}
        onCancel={() => setPrompt(null)}
        okText={prompt?.kind === "rename" ? "Move" : "Create"}
        destroyOnHidden
      >
        {prompt?.kind !== "rename" && (
          <Typography.Paragraph type="secondary">
            In /{currentDir}
          </Typography.Paragraph>
        )}
        <Input
          autoFocus
          value={prompt?.value}
          placeholder={prompt?.kind === "folder" ? "folder name" : prompt?.kind === "file" ? "notes.md" : "new/path/name"}
          onChange={(e) => setPrompt((p) => (p ? { ...p, value: e.target.value } : p))}
          onPressEnter={submitPrompt}
        />
      </Modal>
    </div>
  );
}

export default function Files() {
  return (
    <div style={{ height: "100%" }}>
      <SandboxGate what="the workspace files">
        <FilesInner />
      </SandboxGate>
    </div>
  );
}
