import { CheckOutlined, CloseOutlined, DollarOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App,
  Button,
  Card,
  Col,
  Empty,
  Flex,
  Form,
  Input,
  InputNumber,
  Modal,
  Result,
  Row,
  Segmented,
  Select,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import * as React from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api, ApiError } from "@/api/client";
import type { AdminStats, AdminUser, AuditEvent, ModelRoute, Org, Provider } from "@/api/types";
import { useAuth } from "@/auth";
import { bytes, dateTime, fromNow, usd } from "@/lib/format";
import { useMeta } from "@/meta";

function useAction() {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  return async (fn: () => Promise<unknown>, done: string, ...keys: string[][]) => {
    try {
      await fn();
      message.success(done);
      await Promise.all(keys.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
      return true;
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Failed");
      return false;
    }
  };
}

function Overview() {
  const act = useAction();
  const stats = useQuery({ queryKey: ["admin", "stats"], queryFn: () => api.get<AdminStats>("/api/v1/admin/stats"), refetchInterval: 30_000 });
  const s = stats.data;
  const tiles: [string, React.ReactNode][] = s
    ? [
        ["Users", s.users],
        ["Organizations", s.orgs],
        ["Projects", s.projects],
        ["Running workspaces", s.running_sandboxes],
        ["Model spend, 24 h", usd(s.usage_24h_usd)],
        ["Gateway requests, 24 h", s.requests_24h.toLocaleString()],
        ["Datasets awaiting review", s.datasets_pending_review],
      ]
    : [];
  return (
    <Flex vertical gap={16}>
      <Row gutter={[16, 16]}>
        {tiles.map(([label, value]) => (
          <Col key={label} xs={12} md={8} xl={6}>
            <Card loading={stats.isLoading}>
              <Statistic title={label} value={value as string | number} />
            </Card>
          </Col>
        ))}
      </Row>
      <Card title="Workspaces">
        <Flex justify="space-between" align="center" wrap gap={12}>
          <Typography.Text type="secondary">Idle workspaces stop automatically. Run the reaper now to stop idle ones and reconcile crashed sandboxes.</Typography.Text>
          <Button icon={<ReloadOutlined />} onClick={() => act(() => api.post("/api/v1/admin/sandboxes/reap"), "Reaper finished", ["admin", "stats"])}>
            Reap idle workspaces
          </Button>
        </Flex>
      </Card>
    </Flex>
  );
}

function CreditsModal({ org, onClose }: { org: Org | null; onClose: () => void }) {
  const act = useAction();
  const [form] = Form.useForm<{ amount_usd: number; description: string }>();
  return (
    <Modal
      open={!!org}
      onCancel={onClose}
      title={`Adjust credit for ${org?.name ?? ""}`}
      okText="Apply"
      onOk={() => form.submit()}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{ amount_usd: 10, description: "Manual adjustment" }}
        onFinish={async (v) => {
          if (await act(() => api.post(`/api/v1/admin/orgs/${org!.id}/credits`, v), "Credit adjusted", ["admin", "orgs"])) onClose();
        }}
      >
        <Typography.Paragraph type="secondary">Current balance {usd(org?.balance_usd ?? 0)}. Use a negative amount to deduct.</Typography.Paragraph>
        <Form.Item name="amount_usd" label="Amount (USD)" rules={[{ required: true }]}>
          <InputNumber prefix="$" min={-100000} max={100000} step={5} style={{ width: 200 }} />
        </Form.Item>
        <Form.Item name="description" label="Reason (shown in the organization's credit history)" rules={[{ required: true, max: 500 }]}>
          <Input />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function Orgs() {
  const act = useAction();
  const { data: meta } = useMeta();
  const [q, setQ] = React.useState("");
  const [crediting, setCrediting] = React.useState<Org | null>(null);
  const orgs = useQuery({ queryKey: ["admin", "orgs", q], queryFn: () => api.get<{ orgs: Org[] }>("/api/v1/admin/orgs", { q: q || undefined, limit: 500 }) });
  return (
    <Card>
      <Input.Search allowClear placeholder="Search by name or slug" onSearch={setQ} style={{ maxWidth: 360, marginBottom: 12 }} />
      <Table<Org>
        rowKey="id"
        loading={orgs.isLoading}
        dataSource={orgs.data?.orgs ?? []}
        pagination={{ pageSize: 25, hideOnSinglePage: true }}
        columns={[
          {
            title: "Organization",
            key: "name",
            render: (_, o) => (
              <div>
                <Typography.Text strong>{o.name}</Typography.Text> {o.is_personal && <Tag>personal</Tag>}
                <div>
                  <Typography.Text type="secondary" className="mono" style={{ fontSize: 12 }}>
                    {o.id}
                  </Typography.Text>
                </div>
              </div>
            ),
          },
          {
            title: "Plan",
            key: "plan",
            width: 160,
            render: (_, o) => (
              <Select
                size="small"
                value={o.plan}
                style={{ width: 130 }}
                options={(meta?.plans ?? []).map((p) => ({ value: p.id, label: p.label }))}
                onChange={(plan) => act(() => api.patch(`/api/v1/admin/orgs/${o.id}`, { plan }), `${o.name} is now on ${plan}`, ["admin", "orgs"])}
              />
            ),
          },
          {
            title: "Balance",
            dataIndex: "balance_usd",
            width: 120,
            align: "right",
            render: (v: number) => <span style={{ fontVariantNumeric: "tabular-nums", color: v <= 0 ? "#d03b3b" : undefined }}>{usd(v)}</span>,
          },
          { title: "Created", dataIndex: "created_at", width: 130, render: (v: string) => fromNow(v) },
          {
            title: "",
            key: "actions",
            width: 110,
            render: (_, o) => (
              <Button size="small" icon={<DollarOutlined />} onClick={() => setCrediting(o)}>
                Credit
              </Button>
            ),
          },
        ]}
      />
      <CreditsModal org={crediting} onClose={() => setCrediting(null)} />
    </Card>
  );
}

function Users() {
  const act = useAction();
  const { user: me } = useAuth();
  const [q, setQ] = React.useState("");
  const users = useQuery({ queryKey: ["admin", "users", q], queryFn: () => api.get<{ users: AdminUser[] }>("/api/v1/admin/users", { q: q || undefined, limit: 500 }) });
  const patch = (u: AdminUser, body: Partial<AdminUser>, done: string) => act(() => api.patch(`/api/v1/admin/users/${u.id}`, body), done, ["admin", "users"]);
  return (
    <Card>
      <Input.Search allowClear placeholder="Search by email or name" onSearch={setQ} style={{ maxWidth: 360, marginBottom: 12 }} />
      <Table<AdminUser>
        rowKey="id"
        loading={users.isLoading}
        dataSource={users.data?.users ?? []}
        pagination={{ pageSize: 25, hideOnSinglePage: true }}
        columns={[
          {
            title: "User",
            key: "user",
            render: (_, u) => (
              <div>
                <Typography.Text strong>{u.name || u.email}</Typography.Text>
                <div>
                  <Typography.Text type="secondary">{u.email}</Typography.Text>
                </div>
              </div>
            ),
          },
          { title: "Joined", dataIndex: "created_at", width: 130, render: (v: string) => fromNow(v) },
          { title: "Last sign-in", dataIndex: "last_login_at", width: 140, render: (v: string | null) => (v ? fromNow(v) : "never") },
          {
            title: "Active",
            key: "active",
            width: 90,
            render: (_, u) => (
              <Switch
                size="small"
                checked={u.is_active}
                disabled={u.id === me?.id}
                onChange={(v) => patch(u, { is_active: v }, v ? `${u.email} reactivated` : `${u.email} deactivated`)}
              />
            ),
          },
          {
            title: "Platform admin",
            key: "admin",
            width: 130,
            render: (_, u) => (
              <Switch
                size="small"
                checked={u.is_platform_admin}
                disabled={u.id === me?.id}
                onChange={(v) => patch(u, { is_platform_admin: v }, v ? `${u.email} is now an admin` : `${u.email} is no longer an admin`)}
              />
            ),
          },
        ]}
      />
    </Card>
  );
}

interface QueueItem {
  id: string;
  org_id: string;
  name: string;
  category: string;
  version: string;
  package_size_bytes: number | null;
  updated_at: string;
}

function ReviewModal({ item, onClose }: { item: QueueItem | null; onClose: () => void }) {
  const act = useAction();
  const [note, setNote] = React.useState("");
  const review = async (approve: boolean) => {
    if (await act(() => api.post(`/api/v1/admin/datasets/${item!.id}/review`, { approve, note }), approve ? "Published" : "Sent back to the authors", ["admin"], ["datasets"])) {
      setNote("");
      onClose();
    }
  };
  return (
    <Modal
      open={!!item}
      onCancel={onClose}
      title={`Review ${item?.name ?? ""}`}
      destroyOnHidden
      footer={
        <Flex justify="end" gap={8}>
          <Button danger icon={<CloseOutlined />} disabled={!note.trim()} onClick={() => review(false)}>
            Request changes
          </Button>
          <Button type="primary" icon={<CheckOutlined />} onClick={() => review(true)}>
            Approve and publish
          </Button>
        </Flex>
      }
    >
      <Typography.Paragraph>
        Check the README, files and license on the <Link to={`/datasets/${item?.id}`}>dataset page</Link> before publishing it to every user.
      </Typography.Paragraph>
      <Input.TextArea rows={4} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note for the authors (required to request changes)" />
    </Modal>
  );
}

function Datasets() {
  const [status, setStatus] = React.useState("pending_review");
  const [reviewing, setReviewing] = React.useState<QueueItem | null>(null);
  const queue = useQuery({ queryKey: ["admin", "datasets", status], queryFn: () => api.get<{ datasets: QueueItem[] }>("/api/v1/admin/datasets", { status }) });
  return (
    <Card>
      <Segmented
        value={status}
        onChange={(v) => setStatus(String(v))}
        style={{ marginBottom: 12 }}
        options={[
          { value: "pending_review", label: "Awaiting review" },
          { value: "published", label: "Published" },
          { value: "rejected", label: "Changes requested" },
        ]}
      />
      <Table<QueueItem>
        rowKey="id"
        loading={queue.isLoading}
        dataSource={queue.data?.datasets ?? []}
        pagination={{ pageSize: 25, hideOnSinglePage: true }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="Nothing here" /> }}
        columns={[
          {
            title: "Dataset",
            key: "name",
            render: (_, d) => (
              <div>
                <Link to={`/datasets/${d.id}`}>{d.name}</Link>
                <div>
                  <Typography.Text type="secondary" className="mono" style={{ fontSize: 12 }}>
                    {d.id} · v{d.version}
                  </Typography.Text>
                </div>
              </div>
            ),
          },
          { title: "Category", dataIndex: "category", width: 160, render: (c: string) => <Tag>{c}</Tag> },
          { title: "Size", dataIndex: "package_size_bytes", width: 100, render: (n: number | null) => (n ? bytes(n) : "—") },
          { title: "Updated", dataIndex: "updated_at", width: 130, render: (v: string) => fromNow(v) },
          {
            title: "",
            key: "actions",
            width: 100,
            render: (_, d) =>
              status === "pending_review" && (
                <Button size="small" type="primary" onClick={() => setReviewing(d)}>
                  Review
                </Button>
              ),
          },
        ]}
      />
      <ReviewModal item={reviewing} onClose={() => setReviewing(null)} />
    </Card>
  );
}

function PriceInput(props: { value?: number; onChange?: (v: number | null) => void }) {
  return <InputNumber {...props} prefix="$" min={0} step={0.05} style={{ width: "100%" }} />;
}

function AddProviderModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const act = useAction();
  const [form] = Form.useForm();
  return (
    <Modal open={open} onCancel={onClose} title="Add a platform provider" okText="Add" onOk={() => form.submit()} destroyOnHidden>
      <Form
        form={form}
        layout="vertical"
        initialValues={{ kind: "openai" }}
        onFinish={async (v) => {
          if (await act(() => api.post("/api/v1/admin/providers", v), "Provider added", ["admin", "providers"])) onClose();
        }}
      >
        <Form.Item name="name" label="Name" rules={[{ required: true }]}>
          <Input placeholder="OpenAI" />
        </Form.Item>
        <Form.Item name="kind" label="API format">
          <Select
            options={[
              { value: "openai", label: "OpenAI-compatible" },
              { value: "anthropic", label: "Anthropic" },
              { value: "litellm", label: "LiteLLM proxy" },
              { value: "mock", label: "Mock (demo)" },
            ]}
          />
        </Form.Item>
        <Form.Item name="api_base" label="API base URL" extra="Empty: the provider's default">
          <Input />
        </Form.Item>
        <Form.Item name="api_key" label="API key">
          <Input.Password autoComplete="off" />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function AddModelModal({ open, onClose, providers }: { open: boolean; onClose: () => void; providers: Provider[] }) {
  const act = useAction();
  const [form] = Form.useForm();
  return (
    <Modal open={open} onCancel={onClose} title="Add a platform model" okText="Add" onOk={() => form.submit()} width={680} destroyOnHidden>
      <Form
        form={form}
        layout="vertical"
        initialValues={{ kind: "chat", provider_id: providers[0]?.id, long_context_multiplier: 1 }}
        onFinish={async (v) => {
          if (await act(() => api.post("/api/v1/admin/models", v), "Model added", ["admin", "models"])) onClose();
        }}
      >
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="alias" label="Name customers select" rules={[{ required: true }]}>
              <Input placeholder="gpt-5.5" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="upstream_model" label="Provider's model name" rules={[{ required: true }]}>
              <Input placeholder="gpt-5.5" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="provider_id" label="Provider" rules={[{ required: true }]}>
              <Select options={providers.map((p) => ({ value: p.id, label: `${p.name} (${p.kind})` }))} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="kind" label="Type">
              <Segmented
                options={[
                  { value: "chat", label: "Chat" },
                  { value: "embedding", label: "Embedding" },
                ]}
              />
            </Form.Item>
          </Col>
        </Row>
        <Typography.Text type="secondary">Prices charged to customers, USD per million tokens</Typography.Text>
        <Row gutter={12} style={{ marginTop: 8 }}>
          {[
            ["input_usd_per_mtok", "Input"],
            ["output_usd_per_mtok", "Output"],
            ["cache_read_usd_per_mtok", "Cache read"],
            ["cache_write_usd_per_mtok", "Cache write"],
          ].map(([name, label]) => (
            <Col span={6} key={name}>
              <Form.Item name={name} label={label}>
                <PriceInput />
              </Form.Item>
            </Col>
          ))}
        </Row>
        <Form.Item name="description" label="Description">
          <Input />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function Models() {
  const act = useAction();
  const [adding, setAdding] = React.useState<"provider" | "model" | null>(null);
  const providers = useQuery({ queryKey: ["admin", "providers"], queryFn: () => api.get<{ providers: Provider[] }>("/api/v1/admin/providers") });
  const models = useQuery({ queryKey: ["admin", "models"], queryFn: () => api.get<{ models: ModelRoute[] }>("/api/v1/admin/models") });
  const providerName = (id: string) => providers.data?.providers.find((p) => p.id === id)?.name ?? id;
  const editPrice = (m: ModelRoute, field: string, value: number | null) =>
    value !== null && act(() => api.patch(`/api/v1/admin/models/${m.id}`, { [field]: value }), `Updated ${m.alias}`, ["admin", "models"]);

  return (
    <Flex vertical gap={16}>
      <Card
        title="Models"
        extra={
          <Button icon={<PlusOutlined />} disabled={!providers.data?.providers.length} onClick={() => setAdding("model")}>
            Add model
          </Button>
        }
      >
        <Table<ModelRoute>
          rowKey="id"
          loading={models.isLoading}
          dataSource={models.data?.models ?? []}
          pagination={false}
          columns={[
            {
              title: "Model",
              key: "alias",
              render: (_, m) => (
                <div>
                  <code>{m.alias}</code> <Tag>{m.kind}</Tag>
                  <div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {providerName(m.provider_id)} / {m.upstream_model}
                    </Typography.Text>
                  </div>
                </div>
              ),
            },
            ...(["input", "output", "cache_read"] as const).map((k) => ({
              title: `${k.replace("_", " ")} $/M`,
              key: k,
              width: 130,
              render: (_: unknown, m: ModelRoute) => (
                <InputNumber
                  size="small"
                  min={0}
                  step={0.05}
                  defaultValue={m.pricing_usd_per_mtok[k]}
                  onBlur={(e) => {
                    const v = Number(e.target.value);
                    if (!Number.isNaN(v) && v !== m.pricing_usd_per_mtok[k]) void editPrice(m, `${k}_usd_per_mtok`, v);
                  }}
                  style={{ width: 110 }}
                />
              ),
            })),
            {
              title: "Enabled",
              key: "enabled",
              width: 90,
              render: (_, m) => (
                <Switch
                  size="small"
                  checked={m.enabled}
                  onChange={(enabled) => act(() => api.patch(`/api/v1/admin/models/${m.id}`, { enabled }), `${m.alias} ${enabled ? "enabled" : "disabled"}`, ["admin", "models"])}
                />
              ),
            },
          ]}
        />
      </Card>
      <Card
        title="Providers"
        extra={
          <Button icon={<PlusOutlined />} onClick={() => setAdding("provider")}>
            Add provider
          </Button>
        }
      >
        <Table<Provider>
          rowKey="id"
          loading={providers.isLoading}
          dataSource={providers.data?.providers ?? []}
          pagination={false}
          columns={[
            { title: "Name", dataIndex: "name" },
            { title: "Format", dataIndex: "kind", width: 110, render: (k: string) => <Tag>{k}</Tag> },
            { title: "API base", dataIndex: "api_base", render: (v: string) => <span className="mono">{v || "—"}</span> },
            { title: "Key", dataIndex: "has_api_key", width: 70, render: (v: boolean) => (v ? <CheckOutlined /> : "—") },
            {
              title: "Enabled",
              key: "enabled",
              width: 90,
              render: (_, p) => (
                <Switch
                  size="small"
                  checked={p.enabled}
                  onChange={(enabled) => act(() => api.patch(`/api/v1/admin/providers/${p.id}`, { enabled }), `${p.name} ${enabled ? "enabled" : "disabled"}`, ["admin", "providers"])}
                />
              ),
            },
          ]}
        />
      </Card>
      <AddProviderModal open={adding === "provider"} onClose={() => setAdding(null)} />
      <AddModelModal open={adding === "model"} onClose={() => setAdding(null)} providers={providers.data?.providers ?? []} />
    </Flex>
  );
}

function Audit() {
  const [orgId, setOrgId] = React.useState("");
  const events = useQuery({
    queryKey: ["admin", "audit", orgId],
    queryFn: () => api.get<{ events: AuditEvent[] }>("/api/v1/admin/audit", { org_id: orgId || undefined, limit: 500 }),
  });
  return (
    <Card>
      <Input.Search allowClear placeholder="Filter by organization id" onSearch={(v) => setOrgId(v.trim())} style={{ maxWidth: 360, marginBottom: 12 }} />
      <Table<AuditEvent>
        rowKey="id"
        size="small"
        loading={events.isLoading}
        dataSource={events.data?.events ?? []}
        pagination={{ pageSize: 50, hideOnSinglePage: true }}
        expandable={{
          rowExpandable: (e) => !!e.details && Object.keys(e.details).length > 0,
          expandedRowRender: (e) => <pre className="log-view" style={{ margin: 0 }}>{JSON.stringify(e.details, null, 2)}</pre>,
        }}
        columns={[
          { title: "Time", dataIndex: "created_at", width: 180, render: dateTime },
          { title: "Action", dataIndex: "action", render: (a: string) => <code>{a}</code> },
          { title: "Target", key: "target", render: (_, e) => (e.target_type ? <span className="mono">{`${e.target_type}:${e.target_id ?? ""}`}</span> : "—") },
          { title: "User", dataIndex: "user_id", width: 200, render: (v: string | null) => <span className="mono">{v ?? "—"}</span> },
          { title: "Org", dataIndex: "org_id", width: 200, render: (v: string | null) => <span className="mono">{v ?? "—"}</span> },
          { title: "IP", dataIndex: "ip", width: 130 },
        ]}
      />
    </Card>
  );
}

const TABS = [
  { key: "overview", label: "Overview", el: <Overview /> },
  { key: "orgs", label: "Organizations", el: <Orgs /> },
  { key: "users", label: "Users", el: <Users /> },
  { key: "datasets", label: "Dataset review", el: <Datasets /> },
  { key: "models", label: "Models & pricing", el: <Models /> },
  { key: "audit", label: "Audit log", el: <Audit /> },
];

export default function AdminPage() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const params = useParams();
  const tab = (params["*"] || "overview").split("/")[0];
  if (!user?.is_platform_admin) return <Result status="403" title="Platform administrators only" />;
  return (
    <div className="page">
      <Typography.Title level={3} style={{ marginTop: 0 }}>
        Administration
      </Typography.Title>
      <Tabs
        activeKey={TABS.some((t) => t.key === tab) ? tab : "overview"}
        onChange={(k) => navigate(`/admin/${k}`)}
        destroyOnHidden
        items={TABS.map((t) => ({ key: t.key, label: t.label, children: t.el }))}
      />
    </div>
  );
}
