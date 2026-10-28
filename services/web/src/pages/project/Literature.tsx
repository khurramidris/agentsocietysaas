import {
  CloudDownloadOutlined,
  ExportOutlined,
  FileSearchOutlined,
  ImportOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  SyncOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Checkbox, Drawer, Empty, Flex, Form, Input, InputNumber, Space, Table, Tabs, Tag, Tooltip, Typography, Upload } from "antd";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { api, ApiError, downloadFile, request, sbx } from "@/api/client";
import type { SearchHit, SearchSources, ToolJob } from "@/api/types";
import { PaperHit, SourceTags } from "@/components/PaperHit";
import { dateTime } from "@/lib/format";

import { SandboxGate, useProjectCtx } from "./context";

interface Entry {
  title: string;
  journal?: string | null;
  doi?: string | null;
  abstract?: string | null;
  file_path: string;
  file_type: string;
  source: "literature_search" | "user_upload";
  query?: string | null;
  saved_at: string;
  extra_fields?: Record<string, unknown> | null;
}

const INDEX = "papers/literature_index.json";

function useLibraryOp() {
  const { project } = useProjectCtx();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  return React.useCallback(
    async (body: Record<string, unknown>, done?: string) => {
      try {
        const r = await api.post<{ ok: boolean; error?: string; [k: string]: unknown }>(sbx(project.id, "literature/library"), body);
        if (!r.ok) throw new Error(r.error || "The operation failed");
        if (done) message.success(done);
        await queryClient.invalidateQueries({ queryKey: ["lit-index", project.id] });
        return r;
      } catch (e) {
        message.error(e instanceof ApiError || e instanceof Error ? e.message : "The operation failed");
        return null;
      }
    },
    [project.id, message, queryClient],
  );
}

async function waitForJob(projectId: string, job: ToolJob, onUpdate: (j: ToolJob) => void): Promise<ToolJob> {
  let current = job;
  while (current.status === "running") {
    await new Promise((r) => setTimeout(r, 2000));
    current = await api.get<ToolJob>(sbx(projectId, `tools/jobs/${current.id}`));
    onUpdate(current);
  }
  return current;
}

function SearchDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { project } = useProjectCtx();
  const { message } = App.useApp();
  const op = useLibraryOp();
  const queryClient = useQueryClient();
  const [hits, setHits] = React.useState<SearchHit[] | null>(null);
  const [sources, setSources] = React.useState<SearchSources>({});
  const [busy, setBusy] = React.useState(false);
  const [job, setJob] = React.useState<ToolJob | null>(null);
  const [added, setAdded] = React.useState<Set<string>>(new Set());

  const quick = async (values: { query: string; limit?: number; year_from?: number; year_to?: number }) => {
    setBusy(true);
    try {
      const r = await api.post<{ results: SearchHit[]; sources: SearchSources }>(`/api/v1/orgs/${project.org_id}/literature/search`, {
        ...values,
        limit: values.limit ?? 15,
      });
      setHits(r.results);
      setSources(r.sources);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Search failed");
    } finally {
      setBusy(false);
    }
  };

  const deep = async (values: { query: string; limit?: number; year_from?: number; year_to?: number; multi?: boolean }) => {
    const args = [values.query, "--limit", String(values.limit ?? 10)];
    if (values.year_from) args.push("--year-from", String(values.year_from));
    if (values.year_to) args.push("--year-to", String(values.year_to));
    if (values.multi) args.push("--multi-query");
    const started = await api.post<ToolJob>(sbx(project.id, "tools/run"), { tool: "literature-search", args, wait: false, timeout: 1800 });
    setJob(started);
    const finished = await waitForJob(project.id, started, setJob);
    setJob(finished);
    await queryClient.invalidateQueries({ queryKey: ["lit-index", project.id] });
  };

  const add = async (hit: SearchHit) => {
    const id = hit.doi || hit.url;
    if (!id) return;
    const r = await op({ op: "add-id", identifier: id }, `Added "${hit.title.slice(0, 60)}"`);
    if (r) setAdded((s) => new Set(s).add(id));
  };

  return (
    <Drawer open={open} onClose={onClose} size={900} title="Search literature" destroyOnHidden>
      <Tabs
        items={[
          {
            key: "quick",
            label: "Quick search",
            children: (
              <>
                <Typography.Paragraph type="secondary">
                  OpenAlex, arXiv, Crossref and your organization's library. Add any result to this workspace by its DOI or arXiv id.
                </Typography.Paragraph>
                <Form layout="inline" onFinish={quick} style={{ rowGap: 8 }}>
                  <Form.Item name="query" rules={[{ required: true }]} style={{ flex: 1, minWidth: 280 }}>
                    <Input prefix={<SearchOutlined />} placeholder="e.g. recommender systems echo chambers polarization" />
                  </Form.Item>
                  <Form.Item name="year_from">
                    <InputNumber placeholder="from year" min={1900} max={2100} />
                  </Form.Item>
                  <Form.Item name="year_to">
                    <InputNumber placeholder="to year" min={1900} max={2100} />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" loading={busy}>
                    Search
                  </Button>
                </Form>
                <SourceTags sources={sources} />
                {hits && (
                  <Flex vertical gap={8} style={{ marginTop: 12 }}>
                    {hits.length === 0 && <Empty description="No results" />}
                    {hits.map((hit, i) => {
                      const id = hit.doi || hit.url || `${i}`;
                      return (
                        <PaperHit
                          key={id}
                          hit={hit}
                          actions={
                            (hit.doi || hit.url) &&
                            hit.source !== "local" && (
                              <Button size="small" icon={<PlusOutlined />} disabled={added.has(id)} onClick={() => add(hit)}>
                                {added.has(id) ? "Added" : "Add"}
                              </Button>
                            )
                          }
                        />
                      );
                    })}
                  </Flex>
                )}
              </>
            ),
          },
          {
            key: "deep",
            label: "Deep search (AgentSociety)",
            children: (
              <>
                <Typography.Paragraph type="secondary">
                  Runs the AgentSociety <code>literature-search</code> tool in the workspace: it searches, saves the papers under <code>papers/</code>, updates the
                  library index and writes a summary, exactly as the research skills do.
                </Typography.Paragraph>
                <Form layout="vertical" onFinish={deep} initialValues={{ limit: 10 }}>
                  <Form.Item name="query" label="Query" rules={[{ required: true }]}>
                    <Input.TextArea rows={2} placeholder="How do engagement-optimised feeds affect opinion polarization?" />
                  </Form.Item>
                  <Space wrap>
                    <Form.Item name="limit" label="Papers">
                      <InputNumber min={1} max={50} />
                    </Form.Item>
                    <Form.Item name="year_from" label="From year">
                      <InputNumber min={1900} max={2100} />
                    </Form.Item>
                    <Form.Item name="year_to" label="To year">
                      <InputNumber min={1900} max={2100} />
                    </Form.Item>
                    <Form.Item name="multi" label=" " valuePropName="checked">
                      <Checkbox>Split into sub-queries</Checkbox>
                    </Form.Item>
                  </Space>
                  <Button type="primary" htmlType="submit" icon={<FileSearchOutlined />} loading={job?.status === "running"}>
                    Run search
                  </Button>
                </Form>
                {job && (
                  <div style={{ marginTop: 16 }}>
                    <Tag color={job.status === "succeeded" ? "green" : job.status === "running" ? "processing" : "red"}>{job.status}</Tag>
                    {job.output !== undefined && <pre className="log-view" style={{ maxHeight: 360, marginTop: 8 }}>{job.output || "…"}</pre>}
                  </div>
                )}
              </>
            ),
          },
        ]}
      />
    </Drawer>
  );
}

function LiteratureInner() {
  const { project, canWrite } = useProjectCtx();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const op = useLibraryOp();
  const [searching, setSearching] = React.useState(false);
  const [selected, setSelected] = React.useState<React.Key[]>([]);
  const [doi, setDoi] = React.useState("");
  const [busy, setBusy] = React.useState<string | null>(null);

  const index = useQuery({
    queryKey: ["lit-index", project.id],
    queryFn: async () => {
      try {
        const f = await api.get<{ content: string }>(sbx(project.id, "files/content"), { path: INDEX });
        return (JSON.parse(f.content) as { entries: Entry[] }).entries ?? [];
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return [];
        throw e;
      }
    },
  });
  const entries = (index.data ?? []).map((e, i) => ({ ...e, key: i }));
  const ids = selected.length ? selected.map(Number) : undefined;

  const run = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    try {
      await fn();
    } finally {
      setBusy(null);
    }
  };

  const uploadPaper = async (file: File) => {
    const form = new FormData();
    form.set("directory", "papers");
    form.set("overwrite", "true");
    form.append("files", file, file.name);
    try {
      await request(sbx(project.id, "files/upload"), { method: "POST", body: form });
      if (/\.bib$/i.test(file.name)) await op({ op: "import-bib", file: `papers/${file.name}` }, `Imported ${file.name}`);
      else await op({ op: "add-file", file: `papers/${file.name}` }, `Added ${file.name}`);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : `Could not upload ${file.name}`);
    }
  };

  return (
    <div style={{ maxWidth: 1280, margin: "0 auto" }}>
      <Flex justify="space-between" align="center" wrap gap={12} style={{ marginBottom: 16 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            Workspace library
          </Typography.Title>
          <Typography.Text type="secondary">
            Papers in <code>papers/</code>, indexed in <code>{INDEX}</code> — what the AI scientist cites and reads.
          </Typography.Text>
        </div>
        <Space wrap>
          <Button icon={<ReloadOutlined />} onClick={() => void index.refetch()} />
          {canWrite && (
            <>
              <Button type="primary" icon={<SearchOutlined />} onClick={() => setSearching(true)}>
                Search literature
              </Button>
              <Upload accept=".pdf,.md,.markdown,.txt,.bib" multiple showUploadList={false} customRequest={({ file }) => void uploadPaper(file as File)}>
                <Button icon={<UploadOutlined />}>Upload papers or .bib</Button>
              </Upload>
            </>
          )}
        </Space>
      </Flex>
      {canWrite && (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Flex gap={8} wrap align="center">
            <Input
              style={{ maxWidth: 420 }}
              placeholder="Add by DOI or arXiv id, e.g. 10.1038/s41586-021-03666-1"
              value={doi}
              onChange={(e) => setDoi(e.target.value)}
              onPressEnter={() => doi && run("add", async () => (await op({ op: "add-id", identifier: doi }, "Added to the library")) && setDoi(""))}
            />
            <Button icon={<PlusOutlined />} disabled={!doi} loading={busy === "add"} onClick={() => run("add", async () => (await op({ op: "add-id", identifier: doi }, "Added to the library")) && setDoi(""))}>
              Add
            </Button>
            <div style={{ flex: 1 }} />
            <Tooltip title="Fill in missing metadata and download open-access PDFs">
              <Button icon={<SyncOutlined />} loading={busy === "sync"} onClick={() => run("sync", () => op({ op: "sync", entries: ids }, "Library synced"))}>
                Sync{ids ? ` (${ids.length})` : ""}
              </Button>
            </Tooltip>
            <Button icon={<CloudDownloadOutlined />} loading={busy === "pdf"} onClick={() => run("pdf", () => op({ op: "download-pdf", entries: ids }, "PDF download finished"))}>
              Get PDFs{ids ? ` (${ids.length})` : ""}
            </Button>
            <Button
              icon={<ExportOutlined />}
              loading={busy === "bib"}
              onClick={() =>
                run("bib", async () => {
                  if (await op({ op: "export-bib", entries: ids })) await downloadFile(sbx(project.id, "files/raw"), "library.bib", { path: "papers/library.bib", download: true });
                })
              }
            >
              Export BibTeX
            </Button>
          </Flex>
        </Card>
      )}
      <Table
        size="middle"
        loading={index.isLoading}
        dataSource={entries}
        rowSelection={canWrite ? { selectedRowKeys: selected, onChange: setSelected } : undefined}
        locale={{ emptyText: <Empty description="No papers yet. Search the literature or upload PDFs." /> }}
        pagination={{ pageSize: 25, hideOnSinglePage: true }}
        expandable={{ expandedRowRender: (e) => <Typography.Paragraph style={{ margin: 0 }}>{e.abstract || "No abstract."}</Typography.Paragraph>, rowExpandable: (e) => !!e.abstract }}
        columns={[
          {
            title: "Title",
            dataIndex: "title",
            render: (title: string, e: Entry) => (
              <a onClick={() => navigate(`/p/${project.id}/files?path=${encodeURIComponent(e.file_path)}`)}>{title}</a>
            ),
          },
          { title: "Venue", dataIndex: "journal", width: 200, ellipsis: true, render: (v?: string | null) => v || "—" },
          {
            title: "DOI",
            dataIndex: "doi",
            width: 180,
            ellipsis: true,
            render: (v?: string | null) => (v ? <a href={`https://doi.org/${v}`} target="_blank" rel="noreferrer">{v}</a> : "—"),
          },
          {
            title: "Source",
            dataIndex: "source",
            width: 130,
            render: (s: string, e: Entry) => <Tooltip title={e.query ? `query: ${e.query}` : undefined}><Tag>{s === "user_upload" ? "uploaded" : "search"}</Tag></Tooltip>,
          },
          { title: "Saved", dataIndex: "saved_at", width: 150, render: dateTime },
        ]}
      />
      {canWrite && (
        <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
          <ImportOutlined /> Tip: select rows to sync, fetch PDFs or export only those entries.
        </Typography.Paragraph>
      )}
      <SearchDrawer open={searching} onClose={() => setSearching(false)} />
    </div>
  );
}

export default function Literature() {
  return (
    <SandboxGate what="the literature library">
      <LiteratureInner />
    </SandboxGate>
  );
}
