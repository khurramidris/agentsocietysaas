import { CloudUploadOutlined, DeleteOutlined, DownloadOutlined, PauseCircleOutlined, PlusOutlined, ReloadOutlined, SyncOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  AutoComplete,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Flex,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Switch,
  Table,
  Tag,
  Typography,
  Upload,
} from "antd";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { api, ApiError, downloadFile, request, sbx } from "@/api/client";
import type { ModelRoute, Org, Project } from "@/api/types";
import { SandboxBadge } from "@/components/SandboxBadge";
import { dateTime } from "@/lib/format";
import { useMeta } from "@/meta";

import { SandboxGate, useProjectCtx, useSandboxAction } from "./context";

interface WorkspaceSettings {
  overrides: Record<string, string>;
  overridable_keys: string[];
  effective: Record<string, string>;
  user_env_keys: string[];
}

/** PATCH the project; offers a restart when the change only applies to a fresh sandbox. */
function useUpdateProject() {
  const { project } = useProjectCtx();
  const queryClient = useQueryClient();
  const { message, modal } = App.useApp();
  const action = useSandboxAction(project.id);
  const [busy, setBusy] = React.useState(false);
  const save = async (patch: Record<string, unknown>, done = "Saved") => {
    setBusy(true);
    try {
      const updated = await api.patch<Project>(`/api/v1/projects/${project.id}`, patch);
      queryClient.setQueryData<Project>(["project", project.id], { ...updated, role: project.role });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      message.success(done);
      if (updated.restart_required) {
        modal.confirm({
          title: "Restart the workspace to apply?",
          content: "These settings take effect when the workspace restarts. Running simulations and AI scientist turns are interrupted; files are kept.",
          okText: "Restart now",
          cancelText: "Later",
          onOk: () => action.mutateAsync("restart"),
        });
      }
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not save");
    } finally {
      setBusy(false);
    }
  };
  return { save, busy };
}

function GeneralCard() {
  const { project, canWrite } = useProjectCtx();
  const { save, busy } = useUpdateProject();
  return (
    <Card title="Project">
      <Form
        layout="vertical"
        disabled={!canWrite}
        requiredMark={false}
        initialValues={{ name: project.name, description: project.description }}
        onFinish={(v) => save(v, "Project saved")}
      >
        <Form.Item name="name" label="Name" rules={[{ required: true, whitespace: true, max: 200 }]}>
          <Input />
        </Form.Item>
        <Form.Item name="description" label="Description" rules={[{ max: 5000 }]} extra="The research question itself lives in TOPIC.md; edit it from the Overview page.">
          <Input.TextArea rows={3} placeholder="What this project studies, for your collaborators" />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={busy}>
          Save
        </Button>
      </Form>
    </Card>
  );
}

const MODEL_FIELDS = [
  {
    name: "sim_model",
    label: "Simulated agents",
    kind: "chat",
    help: "Every simulated person and the environment router call this model (AGENTSOCIETY_LLM_MODEL).",
  },
  {
    name: "coder_model",
    label: "Code generation",
    kind: "chat",
    help: "Writes environment tool calls and custom modules (AGENTSOCIETY_CODER_LLM_MODEL). Empty: the simulation model.",
  },
  { name: "embedding_model", label: "Embeddings", kind: "embedding", help: "Agent memory and retrieval (AGENTSOCIETY_EMBEDDING_MODEL)." },
  { name: "chat_model", label: "AI social scientist", kind: "chat", help: "Plans the research, writes code and runs the tools in the AI Scientist chat." },
  { name: "chat_small_model", label: "AI social scientist (fast)", kind: "chat", help: "Used by the AI scientist for quick background steps." },
] as const;

function priceLabel(m: ModelRoute): string {
  if (m.org_id) return "your key";
  const p = m.pricing_usd_per_mtok;
  if (m.kind === "embedding") return `$${p.input}/M tokens`;
  return `$${p.input} in · $${p.output} out /M tokens`;
}

function ModelsCard() {
  const { project, canWrite } = useProjectCtx();
  const { data: meta } = useMeta();
  const { save, busy } = useUpdateProject();
  const [form] = Form.useForm();
  const catalog = useQuery({
    queryKey: ["org-models", project.org_id],
    queryFn: () => api.get<{ models: ModelRoute[]; mock_enabled: boolean }>(`/api/v1/orgs/${project.org_id}/models`),
  });
  const models = (catalog.data?.models ?? []).filter((m) => m.enabled);
  const known = new Set(models.map((m) => m.alias));
  const defaults: Record<string, string | undefined> = {
    sim_model: meta?.defaults.sim_model,
    coder_model: "same as simulated agents",
    embedding_model: meta?.defaults.embedding_model,
    chat_model: meta?.defaults.chat_model,
    chat_small_model: "deployment default",
  };

  const submit = (values: Record<string, string | number | undefined>) => {
    // An empty field removes the project override, falling back to the deployment default.
    const settings = Object.fromEntries(
      [...MODEL_FIELDS.map((f) => f.name), "embedding_dims"].map((k) => {
        const v = values[k];
        return [k, v === undefined || v === "" || v === null ? null : typeof v === "string" ? v.trim() : v];
      }),
    );
    return save({ settings }, "Model settings saved");
  };

  return (
    <Card title="Models" extra={<Typography.Text type="secondary">Calls are metered through the platform gateway</Typography.Text>}>
      <Form form={form} layout="vertical" disabled={!canWrite} initialValues={project.settings} onFinish={submit}>
        {MODEL_FIELDS.map((f) => (
          <Form.Item
            key={f.name}
            name={f.name}
            label={f.label}
            extra={f.help}
            rules={[
              {
                warningOnly: true,
                validator: async (_, v?: string) => {
                  if (v && catalog.data && !catalog.data.mock_enabled && !known.has(v.trim())) throw new Error("Not in your model catalog");
                },
              },
            ]}
          >
            <AutoComplete
              allowClear
              placeholder={defaults[f.name] ? `Default: ${defaults[f.name]}` : "Deployment default"}
              options={models
                .filter((m) => m.kind === f.kind)
                .map((m) => ({
                  value: m.alias,
                  label: (
                    <Flex justify="space-between" gap={12}>
                      <span>{m.alias}</span>
                      <Typography.Text type="secondary">{priceLabel(m)}</Typography.Text>
                    </Flex>
                  ),
                }))}
            />
          </Form.Item>
        ))}
        <Form.Item name="embedding_dims" label="Embedding dimensions" extra="AGENTSOCIETY_EMBEDDING_DIMS; must match what the embedding model returns.">
          <InputNumber min={8} max={8192} placeholder="1024" style={{ width: 160 }} />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={busy}>
          Save models
        </Button>
      </Form>
    </Card>
  );
}

function WorkspaceCard() {
  const { project, status, running, canWrite } = useProjectCtx();
  const { data: meta } = useMeta();
  const { message } = App.useApp();
  const action = useSandboxAction(project.id);
  const { save, busy } = useUpdateProject();
  const org = useQuery({ queryKey: ["org", project.org_id], queryFn: () => api.get<Org>(`/api/v1/orgs/${project.org_id}`) });
  const allowed = org.data?.limits.sandbox_sizes ?? ["small"];
  const [working, setWorking] = React.useState<string | null>(null);

  const run = async (name: string, fn: () => Promise<string>) => {
    setWorking(name);
    try {
      message.success(await fn());
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Failed");
    } finally {
      setWorking(null);
    }
  };

  const versions = status?.agent?.versions ?? {};
  return (
    <Card title="Workspace">
      <Descriptions
        size="small"
        column={{ xs: 1, md: 2 }}
        style={{ marginBottom: 20 }}
        items={[
          { key: "status", label: "Status", children: <SandboxBadge status={project.sandbox.status} error={project.sandbox.error} /> },
          { key: "started", label: "Started", children: project.sandbox.started_at ? dateTime(project.sandbox.started_at) : "—" },
          { key: "as2", label: "AgentSociety", children: versions.agentsociety2 ?? "—" },
          { key: "claude", label: "Claude Code", children: versions.claude_code ?? "—" },
          { key: "driver", label: "Runtime", children: status?.driver ?? "—" },
          { key: "backend", label: "Simulation backend", children: status?.agent?.backend ? (status.agent.backend.running ? "running" : "stopped") : "—" },
        ]}
      />
      <Form
        layout="vertical"
        disabled={!canWrite}
        initialValues={{ sandbox_size: project.sandbox.size, ide_enabled: project.settings.ide_enabled ?? meta?.features.ide ?? false }}
        onFinish={(v: { sandbox_size: string; ide_enabled: boolean }) =>
          save({ sandbox_size: v.sandbox_size, ...(meta?.features.ide ? { settings: { ide_enabled: v.ide_enabled } } : {}) }, "Workspace settings saved")
        }
      >
        <Form.Item name="sandbox_size" label="Size" extra="CPU and memory for simulations. Larger sizes run more agents in parallel.">
          <Select
            style={{ maxWidth: 420 }}
            options={Object.entries(meta?.sandbox_sizes ?? { small: { cpus: 2, memory_gb: 4 } }).map(([size, spec]) => ({
              value: size,
              disabled: !allowed.includes(size),
              label: `${size} — ${spec.cpus} vCPU, ${spec.memory_gb} GB${meta ? ` · $${meta.sandbox_hourly_usd[size]?.toFixed(2)}/h` : ""}${allowed.includes(size) ? "" : " (upgrade)"}`,
            }))}
          />
        </Form.Item>
        {meta?.features.ide && (
          <Form.Item name="ide_enabled" label="VS Code in the browser" valuePropName="checked" extra="Run code-server in the workspace for full IDE access.">
            <Switch />
          </Form.Item>
        )}
        <Button type="primary" htmlType="submit" loading={busy}>
          Save
        </Button>
      </Form>
      {canWrite && (
        <Flex wrap gap={8} style={{ marginTop: 20, paddingTop: 16, borderTop: "1px solid var(--ant-color-border-secondary, #f0f0f0)" }}>
          <Popconfirm title="Restart the workspace? Running simulations are interrupted." onConfirm={() => action.mutate("restart")}>
            <Button icon={<ReloadOutlined />} loading={action.isPending && action.variables === "restart"}>
              Restart workspace
            </Button>
          </Popconfirm>
          {project.sandbox.status !== "stopped" && (
            <Popconfirm title="Stop the workspace? Files are kept." onConfirm={() => action.mutate("stop")}>
              <Button icon={<PauseCircleOutlined />} loading={action.isPending && action.variables === "stop"}>
                Stop workspace
              </Button>
            </Popconfirm>
          )}
          <Button
            icon={<SyncOutlined />}
            disabled={!running}
            loading={working === "skills"}
            onClick={() =>
              run("skills", async () => {
                const r = await api.post<{ synced: string[]; warnings: string[] }>(sbx(project.id, "workspace/init"), { force_sync: true });
                return `Research skills re-installed${r.warnings.length ? ` (${r.warnings.length} warnings)` : ""}`;
              })
            }
          >
            Re-install research skills
          </Button>
          <Button
            icon={<ReloadOutlined />}
            disabled={!running}
            loading={working === "backend"}
            onClick={() =>
              run("backend", async () => {
                const r = await api.post<{ restarted: boolean; healthy?: boolean }>(sbx(project.id, "backend/restart"));
                return r.healthy === false ? "Backend restarted but is not healthy yet" : "Simulation backend restarted";
              })
            }
          >
            Restart simulation backend
          </Button>
        </Flex>
      )}
    </Card>
  );
}

/** Upstream tuning knobs the platform lets a project override (sandboxd OVERRIDABLE_KEYS). */
const ADVANCED: { key: string; label: string; help: string; input: "thinking" | "effort" | "json" | "int" | "number" }[] = [
  {
    key: "AGENTSOCIETY_LLM_THINKING",
    label: "Agent reasoning",
    help: "on / off for model thinking on agent calls. Unset sends no extra parameters (the provider's default).",
    input: "thinking",
  },
  { key: "AGENTSOCIETY_LLM_REASONING_EFFORT", label: "Reasoning effort", help: "Sent as reasoning_effort; with reasoning off the default is minimal.", input: "effort" },
  { key: "AGENTSOCIETY_LLM_EXTRA_BODY", label: "Extra request body", help: 'JSON object merged into every agent request, e.g. {"enable_thinking": false}.', input: "json" },
  { key: "AGENTSOCIETY_CODER_LLM_THINKING", label: "Code generation reasoning", help: "Overrides agent reasoning for code generation.", input: "thinking" },
  { key: "AGENTSOCIETY_CODER_LLM_REASONING_EFFORT", label: "Code generation effort", help: "Overrides reasoning effort for code generation.", input: "effort" },
  { key: "AGENTSOCIETY_CODER_LLM_EXTRA_BODY", label: "Code generation extra body", help: "Overrides the extra request body for code generation.", input: "json" },
  { key: "AGENTSOCIETY_BATCH_SIZE", label: "Agents per batch", help: "Agents stepped by one Ray task (default 256).", input: "int" },
  { key: "AGENTSOCIETY_LLM_RAY_MAX_WORKERS", label: "Max Ray workers", help: "Upper bound on parallel agent batches (default: CPU count).", input: "int" },
  { key: "AGENTSOCIETY_LLM_RAY_CONCURRENCY", label: "LLM concurrency", help: "Initial concurrent LLM requests per worker; adapts automatically (default 16).", input: "int" },
  {
    key: "AGENTSOCIETY_ENV_ACTOR_MAX_CONCURRENCY",
    label: "Environment concurrency",
    help: "Parallel environment requests when every module is concurrency-safe (default 64).",
    input: "int",
  },
  { key: "AGENTSOCIETY_LLM_REQUEST_TIMEOUT", label: "LLM request timeout (s)", help: "Per-request timeout for agent LLM calls (default 60).", input: "number" },
];
const ADVANCED_KEYS = new Set(ADVANCED.map((a) => a.key));

function AdvancedInput({
  input,
  value,
  onChange,
  placeholder,
}: {
  input: (typeof ADVANCED)[number]["input"];
  value?: string;
  onChange?: (v: string) => void;
  placeholder?: string;
}) {
  switch (input) {
    case "thinking":
      return (
        <Select
          value={value ?? ""}
          onChange={onChange}
          style={{ width: 220 }}
          options={[
            { value: "", label: "Provider default" },
            { value: "on", label: "On" },
            { value: "off", label: "Off" },
          ]}
        />
      );
    case "effort":
      return (
        <AutoComplete
          value={value}
          onChange={onChange}
          allowClear
          placeholder={placeholder ?? "unset"}
          style={{ width: 220 }}
          options={["minimal", "low", "medium", "high"].map((v) => ({ value: v }))}
        />
      );
    case "json":
      return <Input.TextArea className="mono" rows={2} value={value} onChange={(e) => onChange?.(e.target.value)} placeholder={placeholder ?? "{}"} />;
    default:
      return (
        <InputNumber
          value={value === undefined || value === "" ? null : Number(value)}
          onChange={(v) => onChange?.(v === null ? "" : String(v))}
          min={input === "int" ? 1 : 0.1}
          step={input === "int" ? 1 : 5}
          precision={input === "int" ? 0 : undefined}
          placeholder={placeholder ?? "default"}
          style={{ width: 220 }}
        />
      );
  }
}

function AdvancedCard({ ws }: { ws: WorkspaceSettings }) {
  const { project, canWrite } = useProjectCtx();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<Record<string, string>>();
  const [saving, setSaving] = React.useState(false);
  // Model keys are normally set in "Models"; an override here would silently win over it.
  const modelOverrides = Object.keys(ws.overrides).filter((k) => !ADVANCED_KEYS.has(k));

  const put = async (overrides: Record<string, string>, done: string) => {
    setSaving(true);
    try {
      await api.put(sbx(project.id, "workspace/settings"), { overrides, restart_backend: true });
      await queryClient.invalidateQueries({ queryKey: ["ws-settings", project.id] });
      message.success(done);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not save");
    } finally {
      setSaving(false);
    }
  };

  const submit = (values: Record<string, string | undefined>) => {
    const kept = Object.fromEntries(modelOverrides.map((k) => [k, ws.overrides[k]]));
    const tuned = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== undefined && v !== ""));
    return put({ ...kept, ...(tuned as Record<string, string>) }, "Simulation settings saved; the simulation backend restarted");
  };

  return (
    <Card title="Simulation engine" extra={<Typography.Text type="secondary">Written to the workspace .env</Typography.Text>}>
      <Typography.Paragraph type="secondary">
        Tuning for agentsociety2 runs. Leave a field empty to use the default. New runs pick changes up; runs already in progress keep their settings.
      </Typography.Paragraph>
      {modelOverrides.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          title="This workspace overrides model settings"
          description={
            <>
              {modelOverrides.map((k) => (
                <div key={k} className="mono">
                  {k}={ws.overrides[k]}
                </div>
              ))}
              These win over the project's model settings above.
            </>
          }
          action={
            canWrite && (
              <Button
                size="small"
                onClick={() =>
                  put(
                    Object.fromEntries(Object.entries(ws.overrides).filter(([k]) => ADVANCED_KEYS.has(k))),
                    "Model overrides cleared; the project's model settings apply",
                  )
                }
              >
                Clear
              </Button>
            )
          }
        />
      )}
      <Form form={form} layout="horizontal" labelCol={{ flex: "230px" }} labelWrap disabled={!canWrite} initialValues={ws.overrides} onFinish={submit}>
        {ADVANCED.map((a) => (
          <Form.Item
            key={a.key}
            name={a.key}
            label={a.label}
            tooltip={a.key}
            extra={a.help}
            rules={
              a.input === "json"
                ? [
                    {
                      validator: async (_, v?: string) => {
                        if (!v) return;
                        let parsed: unknown;
                        try {
                          parsed = JSON.parse(v);
                        } catch {
                          throw new Error("Not valid JSON");
                        }
                        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Must be a JSON object");
                      },
                    },
                  ]
                : undefined
            }
          >
            <AdvancedInput input={a.input} placeholder={ws.effective[a.key]} />
          </Form.Item>
        ))}
        <Flex gap={8}>
          <Button type="primary" htmlType="submit" loading={saving}>
            Save and restart backend
          </Button>
          <Button onClick={() => form.resetFields()}>Reset</Button>
        </Flex>
      </Form>
    </Card>
  );
}

function EnvVarsCard({ ws }: { ws: WorkspaceSettings }) {
  const { project, canWrite } = useProjectCtx();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<{ key: string; value: string }>();
  const [saving, setSaving] = React.useState(false);

  const put = async (body: { values?: Record<string, string>; unset?: string[] }, done: string) => {
    setSaving(true);
    try {
      await api.put(sbx(project.id, "workspace/user-env"), body);
      await queryClient.invalidateQueries({ queryKey: ["ws-settings", project.id] });
      message.success(done);
      return true;
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not save");
      return false;
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card title="Environment variables">
      <Typography.Paragraph type="secondary">
        Your own variables and API keys for custom modules, analysis scripts and the AI scientist. Values are stored in the workspace <code>.env</code>, never
        shown again and never included in exports. They apply to processes started after saving.
      </Typography.Paragraph>
      <Table
        size="small"
        rowKey="key"
        pagination={false}
        dataSource={ws.user_env_keys.map((key) => ({ key }))}
        locale={{ emptyText: "No variables yet" }}
        style={{ marginBottom: 16 }}
        columns={[
          { title: "Name", dataIndex: "key", render: (k: string) => <code>{k}</code> },
          { title: "Value", key: "value", render: () => <Typography.Text type="secondary">••••••••</Typography.Text> },
          {
            title: "",
            key: "actions",
            width: 140,
            align: "right",
            render: (_: unknown, row: { key: string }) =>
              canWrite && (
                <Flex gap={4} justify="end">
                  <Button size="small" type="link" onClick={() => form.setFieldsValue({ key: row.key, value: "" })}>
                    Replace
                  </Button>
                  <Popconfirm title={`Remove ${row.key}?`} onConfirm={() => put({ unset: [row.key] }, `Removed ${row.key}`)}>
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </Flex>
              ),
          },
        ]}
      />
      {canWrite && (
        <Form
          form={form}
          layout="inline"
          onFinish={async (v) => {
            if (await put({ values: { [v.key]: v.value } }, `Saved ${v.key}`)) form.resetFields();
          }}
          style={{ rowGap: 8 }}
        >
          <Form.Item name="key" rules={[{ required: true, pattern: /^[A-Z][A-Z0-9_]{0,127}$/, message: "UPPER_SNAKE_CASE" }]}>
            <Input placeholder="NAME" className="mono" style={{ width: 220 }} onChange={(e) => form.setFieldsValue({ key: e.target.value.toUpperCase() })} />
          </Form.Item>
          <Form.Item name="value" rules={[{ required: true, message: "Value required" }]}>
            <Input.Password placeholder="value" style={{ width: 280 }} autoComplete="new-password" />
          </Form.Item>
          <Button htmlType="submit" icon={<PlusOutlined />} loading={saving}>
            Save variable
          </Button>
        </Form>
      )}
    </Card>
  );
}

function WorkspaceEnvironment() {
  const { project } = useProjectCtx();
  const ws = useQuery({ queryKey: ["ws-settings", project.id], queryFn: () => api.get<WorkspaceSettings>(sbx(project.id, "workspace/settings")) });
  if (!ws.data) return <Card loading />;
  return (
    <>
      <AdvancedCard key={JSON.stringify(ws.data.overrides)} ws={ws.data} />
      <EnvVarsCard ws={ws.data} />
    </>
  );
}

function DataCard() {
  const { project, running, canWrite } = useProjectCtx();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [busy, setBusy] = React.useState<"export" | "import" | null>(null);
  const [pending, setPending] = React.useState<File | null>(null);
  const [overwrite, setOverwrite] = React.useState(false);

  const importArchive = (file: File) => {
    setPending(null);
    setBusy("import");
    const form = new FormData();
    form.append("archive", file);
    request<{ written: number; skipped: number }>(sbx(project.id, "workspace/import"), { method: "POST", body: form, query: { overwrite } })
      .then(async (r) => {
        message.success(`Imported ${r.written} files${r.skipped ? `, skipped ${r.skipped}` : ""}`);
        await queryClient.invalidateQueries({ queryKey: ["ws-info", project.id] });
      })
      .catch((e) => message.error(e instanceof ApiError ? e.message : "Import failed"))
      .finally(() => setBusy(null));
  };

  const exportArchive = () => {
    setBusy("export");
    downloadFile(sbx(project.id, "files/archive"), `${project.slug}.zip`, { path: "" })
      .catch((e) => message.error(e instanceof ApiError ? e.message : "Export failed"))
      .finally(() => setBusy(null));
  };

  return (
    <Card title="Import & export">
      <Typography.Paragraph type="secondary">
        Move work between the platform and a local AgentSociety setup. The archive is a plain workspace folder (TOPIC.md, papers/, hypothesis_*/, custom/ …)
        that the AgentSociety VS Code extension opens as-is. Secrets (<code>.env</code>) and git history are never included.
      </Typography.Paragraph>
      <Flex wrap gap={8}>
        <Button icon={<DownloadOutlined />} disabled={!running} loading={busy === "export"} onClick={exportArchive}>
          Download workspace (.zip)
        </Button>
        {canWrite && (
          <Upload
            accept=".zip,application/zip"
            showUploadList={false}
            disabled={!running}
            beforeUpload={(file) => {
              setOverwrite(false);
              setPending(file);
              return Upload.LIST_IGNORE;
            }}
          >
            <Button icon={<CloudUploadOutlined />} disabled={!running} loading={busy === "import"}>
              Import a workspace (.zip)
            </Button>
          </Upload>
        )}
      </Flex>
      {!running && (
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          Start the workspace to import or export.
        </Typography.Paragraph>
      )}
      <Modal open={!!pending} title={`Import ${pending?.name ?? ""}`} okText="Import" onOk={() => pending && importArchive(pending)} onCancel={() => setPending(null)} destroyOnHidden>
        <Typography.Paragraph>Files from the archive are added to this workspace.</Typography.Paragraph>
        <Checkbox checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)}>
          Replace files that already exist
        </Checkbox>
      </Modal>
    </Card>
  );
}

function DangerCard() {
  const { project } = useProjectCtx();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = React.useState(false);
  const [typed, setTyped] = React.useState("");
  const [deleting, setDeleting] = React.useState(false);

  const remove = async () => {
    setDeleting(true);
    try {
      await api.delete(`/api/v1/projects/${project.id}`);
      message.success(`Deleted ${project.name}`);
      navigate("/");
      queryClient.removeQueries({ queryKey: ["project", project.id] });
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not delete the project");
      setDeleting(false);
    }
  };

  return (
    <Card title={<Typography.Text type="danger">Danger zone</Typography.Text>} style={{ borderColor: "var(--ant-color-error-border, #ffccc7)" }}>
      <Flex justify="space-between" align="center" gap={16} wrap>
        <div>
          <Typography.Text strong>Delete this project</Typography.Text>
          <br />
          <Typography.Text type="secondary">Stops the workspace and permanently deletes its files, runs and chat history.</Typography.Text>
        </div>
        <Button danger icon={<DeleteOutlined />} onClick={() => setOpen(true)}>
          Delete project
        </Button>
      </Flex>
      <Modal
        open={open}
        title={`Delete ${project.name}?`}
        okText="Delete permanently"
        okButtonProps={{ danger: true, disabled: typed !== project.name, loading: deleting }}
        onOk={remove}
        onCancel={() => {
          setOpen(false);
          setTyped("");
        }}
        destroyOnHidden
      >
        <Typography.Paragraph>
          This cannot be undone. Download the workspace first if you want to keep a copy. Type <Tag>{project.name}</Tag> to confirm.
        </Typography.Paragraph>
        <Input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus />
      </Modal>
    </Card>
  );
}

export default function Settings() {
  const { canAdmin } = useProjectCtx();
  return (
    <Flex vertical gap={16} style={{ maxWidth: 980, margin: "0 auto" }}>
      <Typography.Title level={4} style={{ margin: 0 }}>
        Settings
      </Typography.Title>
      <GeneralCard />
      <ModelsCard />
      <WorkspaceCard />
      <SandboxGate what="the workspace environment settings">
        <Flex vertical gap={16}>
          <WorkspaceEnvironment />
        </Flex>
      </SandboxGate>
      <DataCard />
      {canAdmin && <DangerCard />}
    </Flex>
  );
}
