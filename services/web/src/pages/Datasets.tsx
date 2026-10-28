import {
  ArrowLeftOutlined,
  CloudDownloadOutlined,
  CloudUploadOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  FolderAddOutlined,
  ImportOutlined,
  InboxOutlined,
  PlusOutlined,
  SearchOutlined,
  SendOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  AutoComplete,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Flex,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Progress,
  Result,
  Row,
  Segmented,
  Select,
  Space,
  Steps,
  Table,
  Tabs,
  Tag,
  Typography,
  Upload,
} from "antd";
import * as React from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";

import { api, ApiError, sbx } from "@/api/client";
import type { Dataset, DatasetFile, Project, SandboxStatus, ToolJob } from "@/api/types";
import { atLeast, useAuth } from "@/auth";
import { Loading } from "@/components/Loading";
import { bytes, dateTime, fromNow } from "@/lib/format";
import { buildPackage, MAX_BROWSER_PACKAGE_BYTES, type DatasetMeta, type PackageFile, readmeTemplate, SLUG, slugify, uploadPackage } from "@/lib/datasets";
import { MarkdownRenderer } from "@upstream/components/MarkdownRenderer";

const STATUS: Record<Dataset["status"], { color: string; label: string }> = {
  draft: { color: "default", label: "Draft" },
  uploaded: { color: "blue", label: "Private to your organization" },
  pending_review: { color: "gold", label: "In review" },
  published: { color: "green", label: "Public" },
  rejected: { color: "red", label: "Changes requested" },
};

const LICENSES = ["CC BY 4.0", "CC BY-SA 4.0", "CC BY-NC 4.0", "CC0 1.0", "ODC-By 1.0", "ODbL 1.0", "MIT", "Proprietary"];

function useCategories() {
  return useQuery({
    queryKey: ["dataset-categories"],
    queryFn: () => api.get<{ categories: { value: string; label: string }[] }>("/api/v1/data/categories"),
    staleTime: Infinity,
  });
}

function useOrgDatasets() {
  const { org } = useAuth();
  return useQuery({
    queryKey: ["datasets", "org", org?.id],
    queryFn: () => api.get<{ datasets: Dataset[] }>("/api/user/data/datasets", { org_id: org!.id }),
    enabled: !!org && atLeast(org.role, "member"),
  });
}

function StatusTag({ ds }: { ds: Dataset }) {
  const look = ds.status === "published" && ds.visibility !== "public" ? STATUS.uploaded : STATUS[ds.status];
  return <Tag color={look.color}>{look.label}</Tag>;
}

// ------------------------------------------------------------------ wizard

interface WizardProps {
  open: boolean;
  onClose: () => void;
  /** Upload a new version of this dataset instead of creating one. */
  existing?: Dataset;
}

function DatasetWizard({ open, onClose, existing }: WizardProps) {
  const { org, user } = useAuth();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const categories = useCategories();
  const [form] = Form.useForm<DatasetMeta>();
  const [step, setStep] = React.useState(0);
  const [mode, setMode] = React.useState<"files" | "package">("files");
  const [files, setFiles] = React.useState<PackageFile[]>([]);
  const [pkg, setPkg] = React.useState<File | null>(null);
  const [readme, setReadme] = React.useState("");
  const [packing, setPacking] = React.useState<number | null>(null);
  const [sending, setSending] = React.useState<number | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [meta, setMeta] = React.useState<DatasetMeta | null>(null);
  const idTouched = React.useRef(false);

  const total = files.reduce((n, f) => n + f.file.size, 0);
  const busy = packing !== null || sending !== null;

  const reset = () => {
    setStep(0);
    setFiles([]);
    setPkg(null);
    setReadme("");
    setPacking(null);
    setSending(null);
    setError(null);
    setMeta(null);
    idTouched.current = false;
    form.resetFields();
  };

  const close = () => {
    if (busy) return;
    reset();
    onClose();
  };

  const next = async () => {
    const values = await form.validateFields();
    const m: DatasetMeta = { ...values, tags: values.tags ?? [], description: values.description ?? "" };
    setMeta(m);
    if (!readme) setReadme(readmeTemplate(m));
    setStep(1);
  };

  const addFiles = (list: File[]) =>
    setFiles((cur) => {
      const seen = new Set(cur.map((f) => f.path));
      const added = list
        .map((file) => ({ path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name, file }))
        .filter((f) => !seen.has(f.path));
      return [...cur, ...added];
    });

  const submit = async () => {
    if (!meta || !org) return;
    if (mode === "files" && !files.length) return message.warning("Add at least one data file");
    if (mode === "package" && !pkg) return message.warning("Choose a package (.zip)");
    setStep(2);
    setError(null);
    try {
      await api.post("/api/user/data/datasets", meta, { org_id: org.id });
      let blob: Blob;
      if (mode === "files") {
        setPacking(0);
        blob = await buildPackage(meta, readme, files, setPacking);
      } else {
        blob = pkg!;
      }
      setSending(0);
      await uploadPackage(meta.id, org.id, blob, setSending);
      await queryClient.invalidateQueries({ queryKey: ["datasets"] });
      await queryClient.invalidateQueries({ queryKey: ["dataset", meta.id] });
      message.success(existing ? "New version uploaded" : "Dataset uploaded");
      const id = meta.id;
      reset();
      onClose();
      navigate(`/datasets/${id}`);
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : "Upload failed");
    } finally {
      setPacking(null);
      setSending(null);
    }
  };

  const initial: Partial<DatasetMeta> = existing
    ? { ...existing, tags: existing.tags ?? [] }
    : { category: "other", version: "1.0.0", license: "CC BY 4.0", author: user?.name || "", tags: [] };

  return (
    <Modal
      open={open}
      onCancel={close}
      title={existing ? `Upload a new version of ${existing.name}` : "New dataset"}
      width={760}
      maskClosable={!busy}
      closable={!busy}
      destroyOnHidden
      footer={
        <Flex justify="space-between">
          <Button onClick={() => setStep((s) => Math.max(0, s - 1))} disabled={step === 0 || busy}>
            Back
          </Button>
          {step === 0 && (
            <Button type="primary" onClick={next}>
              Next
            </Button>
          )}
          {step === 1 && (
            <Button type="primary" icon={<CloudUploadOutlined />} onClick={submit}>
              Upload
            </Button>
          )}
          {step === 2 && error && (
            <Button type="primary" onClick={submit}>
              Retry
            </Button>
          )}
        </Flex>
      }
    >
      <Steps size="small" current={step} style={{ margin: "8px 0 20px" }} items={[{ title: "Describe" }, { title: "Add data" }, { title: "Upload" }]} />
      <div style={{ display: step === 0 ? "block" : "none" }}>
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          initialValues={initial}
          onValuesChange={(changed: Partial<DatasetMeta>) => {
            if ("id" in changed) idTouched.current = true;
            if ("name" in changed && !idTouched.current && !existing) form.setFieldsValue({ id: slugify(changed.name ?? "") });
          }}
        >
          <Row gutter={16}>
            <Col span={14}>
              <Form.Item name="name" label="Name" rules={[{ required: true, max: 300 }]}>
                <Input placeholder="US voter attitudes survey 2024" autoFocus />
              </Form.Item>
            </Col>
            <Col span={10}>
              <Form.Item
                name="id"
                label="ID"
                rules={[{ required: true }, { pattern: SLUG, message: "lowercase letters, digits, - and _" }]}
                extra="Used by use-dataset download <id>"
              >
                <Input className="mono" disabled={!!existing} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="description" label="Description">
            <Input.TextArea rows={2} placeholder="What the data contains, who collected it, and what it is good for" />
          </Form.Item>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="category" label="Category" rules={[{ required: true }]}>
                <Select options={categories.data?.categories ?? []} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="version" label="Version" rules={[{ required: true, max: 40 }]}>
                <Input placeholder="1.0.0" />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="license" label="License">
                <AutoComplete options={LICENSES.map((value) => ({ value }))} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="author" label="Author" rules={[{ required: true }]}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="tags" label="Tags">
                <Select mode="tags" tokenSeparators={[","]} placeholder="survey, polarization" />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </div>
      {step === 1 && (
        <Flex vertical gap={12}>
          <Segmented
            value={mode}
            onChange={(v) => setMode(v as "files" | "package")}
            options={[
              { value: "files", label: "Add files" },
              { value: "package", label: "I have a package (.zip)" },
            ]}
          />
          {mode === "files" ? (
            <>
              <Upload.Dragger
                multiple
                showUploadList={false}
                beforeUpload={(file, list) => {
                  if (file === list[0]) addFiles(list);
                  return Upload.LIST_IGNORE;
                }}
              >
                <p className="ant-upload-drag-icon">
                  <InboxOutlined />
                </p>
                <p className="ant-upload-text">Drop data files here, or click to choose</p>
                <p className="ant-upload-hint">CSV, JSON, Parquet, … — they go under data/ in the package.</p>
              </Upload.Dragger>
              <Flex justify="space-between" align="center">
                <Upload
                  directory
                  showUploadList={false}
                  beforeUpload={(file, list) => {
                    if (file === list[0]) addFiles(list);
                    return Upload.LIST_IGNORE;
                  }}
                >
                  <Button size="small" icon={<FolderAddOutlined />}>
                    Add a folder
                  </Button>
                </Upload>
                <Typography.Text type={total > MAX_BROWSER_PACKAGE_BYTES ? "danger" : "secondary"}>
                  {files.length} files · {bytes(total)}
                </Typography.Text>
              </Flex>
              {total > MAX_BROWSER_PACKAGE_BYTES && (
                <Alert
                  type="warning"
                  showIcon
                  title="This is large for an upload from the browser"
                  description="Packages over 2 GB are better built in a project workspace with the create-dataset tool (or ask the AI scientist)."
                />
              )}
              {files.length > 0 && (
                <List
                  size="small"
                  bordered
                  style={{ maxHeight: 180, overflow: "auto" }}
                  dataSource={files}
                  renderItem={(f) => (
                    <List.Item
                      actions={[
                        <Button key="rm" size="small" type="text" icon={<DeleteOutlined />} onClick={() => setFiles((cur) => cur.filter((x) => x.path !== f.path))} />,
                      ]}
                    >
                      <span className="mono">data/{f.path}</span>
                      <Typography.Text type="secondary">{bytes(f.file.size)}</Typography.Text>
                    </List.Item>
                  )}
                />
              )}
              <div>
                <Typography.Text strong>README.md</Typography.Text>
                <Typography.Paragraph type="secondary" style={{ marginBottom: 6 }}>
                  Researchers and the AI scientist read this before using the data: describe files, columns, units and provenance.
                </Typography.Paragraph>
                <Input.TextArea className="mono" rows={10} value={readme} onChange={(e) => setReadme(e.target.value)} />
              </div>
            </>
          ) : (
            <>
              <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
                A ZIP made by <code>create-dataset pack</code>: <code>dataset.json</code> (with id <code>{meta?.id}</code>), <code>README.md</code> and{" "}
                <code>data/</code>. Its dataset.json fields take precedence over the details above.
              </Typography.Paragraph>
              <Upload.Dragger
                accept=".zip,application/zip"
                showUploadList={false}
                beforeUpload={(file) => {
                  setPkg(file);
                  return Upload.LIST_IGNORE;
                }}
              >
                <p className="ant-upload-drag-icon">
                  <InboxOutlined />
                </p>
                <p className="ant-upload-text">{pkg ? `${pkg.name} · ${bytes(pkg.size)}` : "Drop the package here, or click to choose"}</p>
              </Upload.Dragger>
            </>
          )}
        </Flex>
      )}
      {step === 2 && (
        <Flex vertical gap={12} style={{ padding: "8px 0" }}>
          {mode === "files" && (
            <div>
              <Typography.Text>Packaging</Typography.Text>
              <Progress percent={Math.round((packing ?? (sending !== null || error ? 1 : 0)) * 100)} status={packing !== null ? "active" : undefined} />
            </div>
          )}
          <div>
            <Typography.Text>Uploading</Typography.Text>
            <Progress percent={Math.round((sending ?? 0) * 100)} status={error ? "exception" : sending !== null ? "active" : undefined} />
          </div>
          {error && <Alert type="error" showIcon title="Upload failed" description={error} />}
        </Flex>
      )}
    </Modal>
  );
}

// ------------------------------------------------------------- add to project

async function waitUntilRunning(projectId: string, timeoutMs = 180_000): Promise<void> {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const s = await api.get<SandboxStatus>(`/api/v1/projects/${projectId}/sandbox`);
    if (s.status === "running" && s.agent) return;
    if (s.status === "error" || s.status === "stopped") throw new Error(s.error || "The workspace did not start");
    await new Promise((r) => setTimeout(r, 2000));
  }
  throw new Error("The workspace is taking too long to start");
}

function AddToProjectModal({ dataset, open, onClose }: { dataset: Dataset; open: boolean; onClose: () => void }) {
  const { org } = useAuth();
  const navigate = useNavigate();
  const [projectId, setProjectId] = React.useState<string>();
  const [phase, setPhase] = React.useState<"idle" | "starting" | "downloading" | "done" | "error">("idle");
  const [detail, setDetail] = React.useState("");
  const projects = useQuery({
    queryKey: ["projects", org?.id],
    queryFn: () => api.get<{ projects: Project[] }>(`/api/v1/orgs/${org!.id}/projects`),
    enabled: open && !!org,
  });
  const writable = atLeast(org?.role, "member") ? (projects.data?.projects ?? []) : [];

  const run = async () => {
    if (!projectId) return;
    const project = writable.find((p) => p.id === projectId);
    try {
      if (project && project.sandbox.status !== "running") {
        setPhase("starting");
        await api.post(`/api/v1/projects/${projectId}/sandbox/start`);
        await waitUntilRunning(projectId);
      }
      setPhase("downloading");
      await api.post(sbx(projectId, "files/mkdir"), { path: "datasets" });
      const job = await api.post<ToolJob>(sbx(projectId, "tools/run"), { tool: "use-dataset", args: ["download", dataset.id], wait: true, timeout: 1800 });
      if (job.status !== "succeeded") throw new Error((job.output || "").trim().split("\n").slice(-6).join("\n") || `use-dataset ${job.status}`);
      setPhase("done");
    } catch (e) {
      setDetail(e instanceof ApiError || e instanceof Error ? e.message : "Download failed");
      setPhase("error");
    }
  };

  const close = () => {
    if (phase === "starting" || phase === "downloading") return;
    setPhase("idle");
    setDetail("");
    onClose();
  };

  return (
    <Modal
      open={open}
      onCancel={close}
      title={`Add ${dataset.name} to a project`}
      destroyOnHidden
      footer={
        phase === "done" ? (
          <Button type="primary" onClick={() => navigate(`/p/${projectId}/files?path=${encodeURIComponent(`datasets/${dataset.id}`)}`)}>
            Open in project files
          </Button>
        ) : (
          <Button type="primary" icon={<ImportOutlined />} disabled={!projectId} loading={phase === "starting" || phase === "downloading"} onClick={run}>
            {phase === "starting" ? "Starting workspace…" : phase === "downloading" ? "Downloading…" : "Download into workspace"}
          </Button>
        )
      }
    >
      <Typography.Paragraph type="secondary">
        Runs <code>use-dataset download {dataset.id}</code> in the project, which saves the data under <code>datasets/{dataset.id}/</code> where experiments,
        analysis scripts and the AI scientist can use it.
      </Typography.Paragraph>
      <Select
        style={{ width: "100%" }}
        placeholder="Choose a project"
        value={projectId}
        onChange={(v) => {
          setProjectId(v);
          setPhase("idle");
        }}
        loading={projects.isLoading}
        options={writable.map((p) => ({ value: p.id, label: `${p.name}${p.sandbox.status === "running" ? "" : " (will start)"}` }))}
      />
      {phase === "done" && <Alert style={{ marginTop: 16 }} type="success" showIcon title={`Saved to datasets/${dataset.id}/`} />}
      {phase === "error" && <Alert style={{ marginTop: 16 }} type="error" showIcon title="Could not add the dataset" description={<pre className="mono">{detail}</pre>} />}
    </Modal>
  );
}

// ---------------------------------------------------------------------- hub

function DatasetTable({ rows, loading, showStatus }: { rows: Dataset[]; loading: boolean; showStatus?: boolean }) {
  const categories = useCategories();
  const label = (c: string) => categories.data?.categories.find((x) => x.value === c)?.label ?? c;
  return (
    <Table<Dataset>
      rowKey="id"
      loading={loading}
      dataSource={rows}
      pagination={{ pageSize: 20, hideOnSinglePage: true }}
      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No datasets" /> }}
      columns={[
        {
          title: "Dataset",
          key: "name",
          render: (_, d) => (
            <div style={{ minWidth: 0 }}>
              <Link to={`/datasets/${d.id}`}>
                <Typography.Text strong>{d.name}</Typography.Text>
              </Link>
              <div>
                <Typography.Text type="secondary" className="mono" style={{ fontSize: 12 }}>
                  {d.id}
                </Typography.Text>
              </div>
              {d.description && (
                <Typography.Paragraph type="secondary" ellipsis={{ rows: 1 }} style={{ margin: 0, fontSize: 13 }}>
                  {d.description}
                </Typography.Paragraph>
              )}
            </div>
          ),
        },
        { title: "Category", dataIndex: "category", width: 160, render: (c: string) => <Tag>{label(c)}</Tag> },
        { title: "Version", dataIndex: "version", width: 90 },
        { title: "Size", dataIndex: "package_size_bytes", width: 100, render: (n: number | null) => (n ? bytes(n) : "—") },
        ...(showStatus ? [{ title: "Status", key: "status", width: 210, render: (_: unknown, d: Dataset) => <StatusTag ds={d} /> }] : []),
        { title: "Updated", dataIndex: "updated_at", width: 130, render: (v: string | null) => fromNow(v) },
      ]}
    />
  );
}

function DatasetHub() {
  const { org } = useAuth();
  const categories = useCategories();
  const [q, setQ] = React.useState("");
  const [query, setQuery] = React.useState("");
  const [category, setCategory] = React.useState<string>();
  const [open, setOpen] = React.useState(false);
  const canWrite = atLeast(org?.role, "member");
  const browse = useQuery({
    queryKey: ["datasets", "browse", query, category],
    queryFn: () => api.get<{ datasets: Dataset[]; total: number }>("/api/v1/data/datasets", { q: query || undefined, category, limit: 500 }),
  });
  const mine = useOrgDatasets();
  const filteredMine = (mine.data?.datasets ?? []).filter(
    (d) => (!category || d.category === category) && (!query || `${d.name} ${d.id} ${d.description}`.toLowerCase().includes(query.toLowerCase())),
  );

  return (
    <div className="page">
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }} wrap gap={12}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            Datasets
          </Typography.Title>
          <Typography.Text type="secondary">Agent profiles, surveys and experiment outputs to reuse across projects.</Typography.Text>
        </div>
        {canWrite && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            New dataset
          </Button>
        )}
      </Flex>
      <Flex gap={8} wrap style={{ marginBottom: 12 }}>
        <Input.Search
          allowClear
          prefix={<SearchOutlined />}
          placeholder="Search datasets"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onSearch={(v) => setQuery(v.trim())}
          style={{ maxWidth: 420 }}
        />
        <Select allowClear placeholder="All categories" style={{ width: 200 }} value={category} onChange={setCategory} options={categories.data?.categories ?? []} />
      </Flex>
      <Card styles={{ body: { paddingTop: 4 } }}>
        <Tabs
          items={[
            { key: "browse", label: "Browse", children: <DatasetTable rows={browse.data?.datasets ?? []} loading={browse.isLoading} /> },
            ...(canWrite
              ? [
                  {
                    key: "mine",
                    label: `${org?.name ?? "Your organization"} (${mine.data?.datasets.length ?? 0})`,
                    children: <DatasetTable rows={filteredMine} loading={mine.isLoading} showStatus />,
                  },
                ]
              : []),
          ]}
        />
      </Card>
      <DatasetWizard open={open} onClose={() => setOpen(false)} />
    </div>
  );
}

// ------------------------------------------------------------------- detail

function DatasetDetail() {
  const { datasetId = "" } = useParams();
  const { org } = useAuth();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const categories = useCategories();
  const [adding, setAdding] = React.useState(false);
  const [uploading, setUploading] = React.useState(false);
  const [busy, setBusy] = React.useState<string | null>(null);
  const ds = useQuery({ queryKey: ["dataset", datasetId], queryFn: () => api.get<Dataset>(`/api/v1/data/datasets/${datasetId}`) });
  const readme = useQuery({
    queryKey: ["dataset", datasetId, "readme"],
    queryFn: () => api.get<{ content: string }>(`/api/v1/data/datasets/${datasetId}/readme`),
    enabled: ds.isSuccess,
  });
  const files = useQuery({
    queryKey: ["dataset", datasetId, "files"],
    queryFn: () => api.get<{ files: DatasetFile[] }>(`/api/v1/data/datasets/${datasetId}/file-tree`),
    enabled: ds.isSuccess,
  });
  const mine = useOrgDatasets();
  const own = !!mine.data?.datasets.some((d) => d.id === datasetId) && atLeast(org?.role, "member");

  if (ds.isLoading) return <Loading />;
  if (ds.error || !ds.data)
    return (
      <Result
        status="404"
        title="Dataset not found"
        subTitle="It does not exist, or it is private to another organization."
        extra={<Button onClick={() => navigate("/datasets")}>Back to datasets</Button>}
      />
    );
  const d = ds.data;

  const act = async (name: string, fn: () => Promise<unknown>, done: string): Promise<boolean> => {
    setBusy(name);
    try {
      await fn();
      message.success(done);
      await queryClient.invalidateQueries({ queryKey: ["datasets"] });
      await queryClient.invalidateQueries({ queryKey: ["dataset", datasetId] });
      return true;
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Failed");
      return false;
    } finally {
      setBusy(null);
    }
  };

  const download = () =>
    act(
      "download",
      async () => {
        const { url } = await api.post<{ url: string }>(`/api/v1/data/datasets/${d.id}/download`);
        window.location.assign(url);
      },
      "Download started",
    );

  return (
    <div className="page" style={{ maxWidth: 1200 }}>
      <Button type="link" icon={<ArrowLeftOutlined />} onClick={() => navigate("/datasets")} style={{ paddingLeft: 0 }}>
        Datasets
      </Button>
      <Flex justify="space-between" align="start" gap={16} wrap style={{ marginBottom: 16 }}>
        <div style={{ minWidth: 0 }}>
          <Space align="center" wrap>
            <DatabaseOutlined style={{ fontSize: 22, color: "#1f4fd8" }} />
            <Typography.Title level={3} style={{ margin: 0 }}>
              {d.name}
            </Typography.Title>
            <StatusTag ds={d} />
          </Space>
          {d.description && (
            <Typography.Paragraph type="secondary" style={{ margin: "6px 0 0", maxWidth: 760 }}>
              {d.description}
            </Typography.Paragraph>
          )}
        </div>
        <Space wrap>
          <Button icon={<CloudDownloadOutlined />} disabled={!d.package_size_bytes} loading={busy === "download"} onClick={download}>
            Download
          </Button>
          {atLeast(org?.role, "member") && (
            <Button type="primary" icon={<ImportOutlined />} disabled={!d.package_size_bytes} onClick={() => setAdding(true)}>
              Add to a project
            </Button>
          )}
        </Space>
      </Flex>
      {d.status === "rejected" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          title="The reviewers asked for changes before publishing"
          description={d.review_note || "Upload a new version and submit it again."}
        />
      )}
      {d.status === "pending_review" && (
        <Alert type="info" showIcon style={{ marginBottom: 16 }} title="Waiting for review. It becomes public once a platform admin approves it." />
      )}
      {own && d.status === "draft" && (
        <Alert type="info" showIcon style={{ marginBottom: 16 }} title="No data uploaded yet" description="Upload a package to make this dataset usable." />
      )}
      <Row gutter={16}>
        <Col xs={24} lg={16}>
          <Card styles={{ body: { paddingTop: 4 } }}>
            <Tabs
              items={[
                {
                  key: "readme",
                  label: "README",
                  children: readme.data?.content ? (
                    <MarkdownRenderer content={readme.data.content} isDark={false} />
                  ) : (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No README" />
                  ),
                },
                {
                  key: "files",
                  label: `Files (${files.data?.files.length ?? 0})`,
                  children: (
                    <Table<DatasetFile>
                      size="small"
                      rowKey="path"
                      dataSource={files.data?.files ?? []}
                      pagination={{ pageSize: 50, hideOnSinglePage: true }}
                      columns={[
                        { title: "Path", dataIndex: "path", render: (p: string) => <span className="mono">{p}</span> },
                        { title: "Size", dataIndex: "size_bytes", width: 110, render: (n: number) => bytes(n) },
                      ]}
                    />
                  ),
                },
                {
                  key: "use",
                  label: "Use it",
                  children: (
                    <Flex vertical gap={8}>
                      <Typography.Text>In a project, ask the AI scientist or run the tool:</Typography.Text>
                      <Typography.Paragraph copyable={{ text: `use-dataset download ${d.id}` }}>
                        <pre className="log-view" style={{ margin: 0 }}>{`use-dataset readme ${d.id}\nuse-dataset download ${d.id}`}</pre>
                      </Typography.Paragraph>
                      <Typography.Text>
                        From a local AgentSociety workspace, point the upstream skill at this platform (public datasets):
                      </Typography.Text>
                      <Typography.Paragraph copyable={{ text: `use-dataset --server ${window.location.origin} download ${d.id}` }}>
                        <pre className="log-view" style={{ margin: 0 }}>{`use-dataset --server ${window.location.origin} download ${d.id}`}</pre>
                      </Typography.Paragraph>
                    </Flex>
                  ),
                },
              ]}
            />
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title="Details">
            <Descriptions
              column={1}
              size="small"
              items={[
                { key: "id", label: "ID", children: <Typography.Text copyable className="mono">{d.id}</Typography.Text> },
                { key: "category", label: "Category", children: categories.data?.categories.find((c) => c.value === d.category)?.label ?? d.category },
                { key: "version", label: "Version", children: d.version },
                { key: "author", label: "Author", children: d.author || "—" },
                { key: "license", label: "License", children: d.license || "—" },
                { key: "size", label: "Package", children: d.package_size_bytes ? bytes(d.package_size_bytes) : "—" },
                { key: "updated", label: "Updated", children: dateTime(d.updated_at) },
                ...(d.published_at ? [{ key: "published", label: "Published", children: dateTime(d.published_at) }] : []),
                ...(d.tags.length ? [{ key: "tags", label: "Tags", children: d.tags.map((t) => <Tag key={t}>{t}</Tag>) }] : []),
              ]}
            />
          </Card>
          {own && (
            <Card title="Manage" style={{ marginTop: 16 }}>
              <Flex vertical gap={8}>
                <Button icon={<CloudUploadOutlined />} onClick={() => setUploading(true)}>
                  Upload a new version
                </Button>
                {(d.status === "uploaded" || d.status === "rejected") && (
                  <Popconfirm
                    title="Submit for review?"
                    description="Once a platform admin approves it, everyone can find and download this dataset."
                    onConfirm={() => act("submit", () => api.post(`/api/user/data/datasets/${d.id}/submit`, {}, { org_id: org!.id }), "Submitted for review")}
                  >
                    <Button icon={<SendOutlined />} loading={busy === "submit"}>
                      Publish publicly…
                    </Button>
                  </Popconfirm>
                )}
                <Popconfirm
                  title="Delete this dataset?"
                  description="The package is removed for everyone. Copies already downloaded into projects stay."
                  onConfirm={() =>
                    act("delete", () => api.delete(`/api/user/data/datasets/${d.id}`, { org_id: org!.id }), "Dataset deleted").then((ok) => ok && navigate("/datasets"))
                  }
                >
                  <Button danger icon={<DeleteOutlined />} loading={busy === "delete"}>
                    Delete
                  </Button>
                </Popconfirm>
              </Flex>
            </Card>
          )}
        </Col>
      </Row>
      <AddToProjectModal dataset={d} open={adding} onClose={() => setAdding(false)} />
      {own && <DatasetWizard open={uploading} onClose={() => setUploading(false)} existing={d} />}
    </div>
  );
}

export default function DatasetsPage() {
  return (
    <Routes>
      <Route index element={<DatasetHub />} />
      <Route path=":datasetId" element={<DatasetDetail />} />
    </Routes>
  );
}
