import {
  ApiOutlined,
  CheckOutlined,
  CopyOutlined,
  DeleteOutlined,
  KeyOutlined,
  MailOutlined,
  PlusOutlined,
  ShoppingCartOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
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
  Popconfirm,
  Result,
  Row,
  Segmented,
  Select,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import * as React from "react";
import { Navigate, Route, Routes, useSearchParams } from "react-router-dom";

import { api, ApiError } from "@/api/client";
import type { ApiKey, BillingOverview, Invitation, LedgerEntry, Member, ModelRoute, Plan, Provider, Role, UsageReport } from "@/api/types";
import { atLeast, useAuth } from "@/auth";
import { DailyBars, fillDays } from "@/components/DailyBars";
import { useCurrentOrg } from "@/layout/AppLayout";
import { dateTime, fromNow, tokens, usd } from "@/lib/format";

const ROLES: { value: Role; label: string; help: string }[] = [
  { value: "viewer", label: "Viewer", help: "Read projects, results and datasets" },
  { value: "member", label: "Member", help: "Create and run projects, upload datasets" },
  { value: "admin", label: "Admin", help: "Manage members, models, API keys and billing" },
  { value: "owner", label: "Owner", help: "Everything, including plans and owners" },
];

function AdminOnly({ children, need = "admin" }: { children: React.ReactNode; need?: Role }) {
  const { org } = useAuth();
  if (atLeast(org?.role, need)) return <>{children}</>;
  return <Result status="403" title={`Only organization ${need}s can manage this`} subTitle="Ask an admin of this organization for access." />;
}

function useInvalidate() {
  const queryClient = useQueryClient();
  return (...keys: unknown[][]) => Promise.all(keys.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
}

// ------------------------------------------------------------------ members

function GeneralCard() {
  const { org, reload } = useAuth();
  const { message } = App.useApp();
  const invalidate = useInvalidate();
  const canAdmin = atLeast(org?.role, "admin");
  const rename = async ({ name }: { name: string }) => {
    try {
      await api.patch(`/api/v1/orgs/${org!.id}`, { name });
      await reload();
      await invalidate(["org", org!.id]);
      message.success("Organization renamed");
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not rename");
    }
  };
  return (
    <Card title="Organization">
      <Form layout="inline" initialValues={{ name: org?.name }} onFinish={rename} disabled={!canAdmin} key={org?.id}>
        <Form.Item name="name" rules={[{ required: true, max: 200 }]} style={{ minWidth: 320 }}>
          <Input />
        </Form.Item>
        <Button htmlType="submit">Rename</Button>
      </Form>
    </Card>
  );
}

function InviteModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { org } = useAuth();
  const invalidate = useInvalidate();
  const [form] = Form.useForm<{ email: string; role: Role }>();
  const [result, setResult] = React.useState<Invitation | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState(false);
  const submit = async (values: { email: string; role: Role }) => {
    setSaving(true);
    setError(null);
    try {
      setResult(await api.post<Invitation>(`/api/v1/orgs/${org!.id}/invitations`, values));
      await invalidate(["members", org!.id]);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not invite");
    } finally {
      setSaving(false);
    }
  };
  const close = () => {
    setResult(null);
    setError(null);
    form.resetFields();
    onClose();
  };
  return (
    <Modal
      open={open}
      onCancel={close}
      title="Invite a member"
      destroyOnHidden
      footer={
        result ? (
          <Button type="primary" onClick={close}>
            Done
          </Button>
        ) : (
          <Button type="primary" icon={<MailOutlined />} loading={saving} onClick={() => form.submit()}>
            Create invitation
          </Button>
        )
      }
    >
      {result ? (
        <Flex vertical gap={12}>
          <Alert type="success" showIcon title={`Invitation for ${result.email} created`} description="Send them this link. It expires in 7 days and works only for that email address." />
          <Typography.Paragraph copyable={{ text: result.accept_url }} className="mono" style={{ wordBreak: "break-all" }}>
            {result.accept_url}
          </Typography.Paragraph>
        </Flex>
      ) : (
        <Form form={form} layout="vertical" onFinish={submit} initialValues={{ role: "member" }} requiredMark={false}>
          <Form.Item name="email" label="Email" rules={[{ required: true, type: "email" }]}>
            <Input autoFocus placeholder="colleague@university.edu" />
          </Form.Item>
          <Form.Item name="role" label="Role">
            <Select
              options={ROLES.filter((r) => r.value !== "owner" || org?.role === "owner").map((r) => ({
                value: r.value,
                label: (
                  <Flex justify="space-between" gap={12}>
                    <span>{r.label}</span>
                    <Typography.Text type="secondary">{r.help}</Typography.Text>
                  </Flex>
                ),
              }))}
            />
          </Form.Item>
          {error && <Alert type="error" showIcon title={error} />}
        </Form>
      )}
    </Modal>
  );
}

function MembersTab() {
  const { org, user } = useAuth();
  const { message } = App.useApp();
  const invalidate = useInvalidate();
  const [inviting, setInviting] = React.useState(false);
  const canAdmin = atLeast(org?.role, "admin");
  const members = useQuery({
    queryKey: ["members", org?.id],
    queryFn: () => api.get<{ members: Member[]; invitations: Invitation[] }>(`/api/v1/orgs/${org!.id}/members`),
    enabled: !!org,
  });

  const call = async (fn: () => Promise<unknown>, done: string) => {
    try {
      await fn();
      message.success(done);
      await invalidate(["members", org!.id]);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Failed");
    }
  };

  return (
    <Flex vertical gap={16}>
      <GeneralCard />
      <Card
        title="Members"
        extra={
          canAdmin && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setInviting(true)}>
              Invite
            </Button>
          )
        }
      >
        <Table<Member>
          rowKey="user_id"
          loading={members.isLoading}
          dataSource={members.data?.members ?? []}
          pagination={false}
          columns={[
            {
              title: "Member",
              key: "member",
              render: (_, m) => (
                <div>
                  <Typography.Text strong>{m.name || m.email}</Typography.Text>
                  {m.user_id === user?.id && <Tag style={{ marginLeft: 8 }}>you</Tag>}
                  <div>
                    <Typography.Text type="secondary">{m.email}</Typography.Text>
                  </div>
                </div>
              ),
            },
            {
              title: "Role",
              key: "role",
              width: 170,
              render: (_, m) =>
                canAdmin && m.user_id !== user?.id ? (
                  <Select
                    size="small"
                    value={m.role}
                    style={{ width: 130 }}
                    onChange={(role) => call(() => api.patch(`/api/v1/orgs/${org!.id}/members/${m.user_id}`, { role }), `${m.email} is now ${role}`)}
                    options={ROLES.map((r) => ({ value: r.value, label: r.label, disabled: r.value === "owner" && org?.role !== "owner" }))}
                  />
                ) : (
                  <Tag>{m.role}</Tag>
                ),
            },
            { title: "Joined", dataIndex: "joined_at", width: 140, render: (v: string) => fromNow(v) },
            {
              title: "",
              key: "actions",
              width: 60,
              render: (_, m) =>
                canAdmin &&
                m.user_id !== user?.id && (
                  <Popconfirm title={`Remove ${m.email}?`} description="Their API keys for this organization are revoked." onConfirm={() => call(() => api.delete(`/api/v1/orgs/${org!.id}/members/${m.user_id}`), "Member removed")}>
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                ),
            },
          ]}
        />
      </Card>
      {(members.data?.invitations.length ?? 0) > 0 && (
        <Card title="Pending invitations">
          <Table<Invitation>
            rowKey="id"
            dataSource={members.data?.invitations ?? []}
            pagination={false}
            columns={[
              { title: "Email", dataIndex: "email" },
              { title: "Role", dataIndex: "role", width: 120, render: (r: string) => <Tag>{r}</Tag> },
              { title: "Expires", dataIndex: "expires_at", width: 180, render: dateTime },
              {
                title: "",
                key: "actions",
                width: 90,
                render: (_, inv) =>
                  canAdmin && (
                    <Button size="small" type="link" danger onClick={() => call(() => api.delete(`/api/v1/orgs/${org!.id}/invitations/${inv.id}`), "Invitation revoked")}>
                      Revoke
                    </Button>
                  ),
              },
            ]}
          />
        </Card>
      )}
      <InviteModal open={inviting} onClose={() => setInviting(false)} />
    </Flex>
  );
}

// ------------------------------------------------------------------ billing

const TOPUPS = [10, 25, 50, 100];

function planSummary(p: Plan): string {
  if (p.price_usd_month === null) return "Custom limits and pricing";
  const credit = p.monthly_credit_usd > 0 ? `${usd(p.monthly_credit_usd, 0)} credit/mo` : "No monthly credit";
  return `${credit} · ${p.max_projects} projects · ${p.max_running_sandboxes} running · ${p.sandbox_sizes.join(", ")}`;
}

function BillingTab() {
  const { org } = useAuth();
  const { message } = App.useApp();
  const [params, setParams] = useSearchParams();
  const invalidate = useInvalidate();
  const [amount, setAmount] = React.useState<number>(25);
  const [redirecting, setRedirecting] = React.useState<string | null>(null);
  const returned = params.get("paid") ? "paid" : params.get("subscribed") ? "subscribed" : null;
  const billing = useQuery({
    queryKey: ["billing", org?.id],
    queryFn: () => api.get<BillingOverview>(`/api/v1/orgs/${org!.id}/billing`),
    enabled: !!org,
    // Stripe confirms by webhook, usually within seconds of returning here.
    refetchInterval: returned ? 3000 : false,
  });

  React.useEffect(() => {
    if (!returned) return;
    message.success(returned === "paid" ? "Payment received. Credit appears as soon as Stripe confirms it." : "Subscription started.");
    const t = setTimeout(() => {
      params.delete("paid");
      params.delete("subscribed");
      setParams(params, { replace: true });
      void invalidate(["org", org!.id]);
    }, 15_000);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [returned]);

  const checkout = async (kind: "topup" | "subscribe", body: object) => {
    setRedirecting(kind);
    try {
      const { checkout_url } = await api.post<{ checkout_url: string }>(`/api/v1/orgs/${org!.id}/billing/${kind}`, body);
      window.location.assign(checkout_url);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not start checkout");
      setRedirecting(null);
    }
  };

  const b = billing.data;
  if (!b) return <Card loading />;
  const current = b.plans.find((p) => p.id === b.plan);
  const canPay = atLeast(org?.role, "admin") && b.stripe_enabled;

  return (
    <Flex vertical gap={16}>
      <Row gutter={[16, 16]}>
        <Col xs={24} md={8}>
          <Card style={{ height: "100%" }}>
            <Statistic title="Credit balance" value={usd(b.balance_usd)} valueStyle={{ fontSize: 34, color: b.balance_usd <= 0 ? "#d03b3b" : undefined }} />
            <Typography.Text type="secondary">{b.balance_usd <= 0 ? "Model calls are paused until you add credit." : "Prepaid; model and compute usage is deducted as it happens."}</Typography.Text>
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card style={{ height: "100%" }}>
            <Statistic title="Spent in the last 30 days" value={usd(b.spent_30d_usd)} />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card style={{ height: "100%" }}>
            <Statistic title="Plan" value={current?.label ?? b.plan} />
            <Typography.Text type="secondary">{current ? planSummary(current) : ""}</Typography.Text>
          </Card>
        </Col>
      </Row>
      {!b.stripe_enabled && (
        <Alert type="info" showIcon title="Online payments are not configured on this deployment" description="Ask the platform operator to add credit or change your plan." />
      )}
      <Card title="Plans">
        <Row gutter={[12, 12]}>
          {b.plans.map((p) => (
            <Col key={p.id} xs={24} sm={12} xl={6}>
              <Card size="small" style={{ borderColor: p.id === b.plan ? "#1f4fd8" : undefined, height: "100%" }}>
                <Flex vertical gap={4}>
                  <Flex justify="space-between" align="center">
                    <Typography.Text strong>{p.label}</Typography.Text>
                    {p.id === b.plan && <Tag color="blue">current</Tag>}
                  </Flex>
                  <Typography.Title level={4} style={{ margin: 0 }}>
                    {p.price_usd_month === null ? "Contact us" : p.price_usd_month === 0 ? "Free" : `${usd(p.price_usd_month, 0)}/mo`}
                  </Typography.Title>
                  <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                    {planSummary(p)}
                  </Typography.Text>
                  {p.id !== b.plan && p.price_usd_month !== null && p.price_usd_month > 0 && (
                    <Button
                      size="small"
                      style={{ marginTop: 8, alignSelf: "flex-start" }}
                      disabled={!b.stripe_enabled || org?.role !== "owner"}
                      loading={redirecting === "subscribe"}
                      onClick={() => checkout("subscribe", { plan: p.id })}
                    >
                      {org?.role === "owner" ? "Upgrade" : "Owners can upgrade"}
                    </Button>
                  )}
                </Flex>
              </Card>
            </Col>
          ))}
        </Row>
      </Card>
      <Card title="Add credit">
        <Flex gap={12} wrap align="center">
          <Segmented
            value={TOPUPS.includes(amount) ? amount : "custom"}
            onChange={(v) => v !== "custom" && setAmount(Number(v))}
            options={[...TOPUPS.map((v) => ({ value: v, label: `$${v}` })), { value: "custom", label: "Other" }]}
            disabled={!canPay}
          />
          <InputNumber prefix="$" min={5} max={10000} precision={0} value={amount} onChange={(v) => setAmount(v ?? 5)} style={{ width: 140 }} disabled={!canPay} />
          <Button type="primary" icon={<ShoppingCartOutlined />} disabled={!canPay} loading={redirecting === "topup"} onClick={() => checkout("topup", { amount_usd: amount })}>
            Pay {usd(amount, 0)} with Stripe
          </Button>
        </Flex>
      </Card>
      <Card title="Credit history">
        <Table<LedgerEntry>
          rowKey="id"
          size="small"
          dataSource={b.ledger}
          pagination={{ pageSize: 15, hideOnSinglePage: true }}
          columns={[
            { title: "Date", dataIndex: "created_at", width: 180, render: dateTime },
            { title: "Description", dataIndex: "description" },
            { title: "Type", dataIndex: "kind", width: 120, render: (k: string) => <Tag>{k}</Tag> },
            {
              title: "Amount",
              dataIndex: "delta_usd",
              width: 120,
              align: "right",
              render: (v: number) => <span style={{ fontVariantNumeric: "tabular-nums", color: v >= 0 ? "#006300" : undefined }}>{`${v >= 0 ? "+" : "−"}${usd(Math.abs(v))}`}</span>,
            },
            { title: "Balance", dataIndex: "balance_after_usd", width: 120, align: "right", render: (v: number) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{usd(v)}</span> },
          ]}
        />
      </Card>
    </Flex>
  );
}

// -------------------------------------------------------------------- usage

function UsageTab() {
  const { org } = useAuth();
  const [days, setDays] = React.useState(30);
  const [view, setView] = React.useState<"chart" | "table">("chart");
  const usage = useQuery({
    queryKey: ["usage", org?.id, days],
    queryFn: () => api.get<UsageReport>(`/api/v1/orgs/${org!.id}/usage`, { days }),
    enabled: !!org,
    placeholderData: (prev) => prev,
  });
  const u = usage.data;
  const daily = React.useMemo(() => fillDays((u?.by_day ?? []).map((d) => ({ date: d.date, value: d.cost_usd })), days), [u, days]);
  const total = (u?.by_day ?? []).reduce((n, d) => n + d.cost_usd, 0);
  const models = (u?.by_model ?? []).filter((m) => m.kind !== "compute");
  const requests = models.reduce((n, m) => n + m.requests, 0);
  const tokensIn = models.reduce((n, m) => n + m.input_tokens, 0);
  const tokensOut = models.reduce((n, m) => n + m.output_tokens, 0);
  const computeHours = (u?.by_model ?? []).filter((m) => m.kind === "compute").reduce((n, m) => n + m.seconds, 0) / 3600;
  const money = (v: number) => (v === 0 ? "$0" : v < 0.01 ? `$${v.toFixed(3)}` : v < 10 ? `$${v.toFixed(2)}` : `$${Math.round(v).toLocaleString()}`);

  return (
    <Flex vertical gap={16} style={{ opacity: usage.isFetching && usage.isPlaceholderData ? 0.6 : 1, transition: "opacity 120ms" }}>
      <Segmented
        value={days}
        onChange={(v) => setDays(Number(v))}
        options={[
          { value: 7, label: "Last 7 days" },
          { value: 30, label: "Last 30 days" },
          { value: 90, label: "Last 90 days" },
        ]}
        style={{ alignSelf: "flex-start" }}
      />
      <Row gutter={16}>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="Spend" value={usd(total)} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="Model requests" value={requests.toLocaleString()} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="Tokens in / out" value={`${tokens(tokensIn)} / ${tokens(tokensOut)}`} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="Workspace hours" value={computeHours < 10 ? computeHours.toFixed(1) : Math.round(computeHours).toLocaleString()} />
          </Card>
        </Col>
      </Row>
      <Card
        title="Daily spend"
        extra={
          <Segmented
            size="small"
            value={view}
            onChange={(v) => setView(v as "chart" | "table")}
            options={[
              { value: "chart", label: "Chart" },
              { value: "table", label: "Table" },
            ]}
          />
        }
      >
        {view === "chart" ? (
          <DailyBars data={daily} format={money} label={`Daily spend over the last ${days} days`} />
        ) : (
          <Table
            size="small"
            rowKey="date"
            dataSource={[...daily].reverse()}
            pagination={{ pageSize: 15, hideOnSinglePage: true }}
            columns={[
              { title: "Date", dataIndex: "date" },
              { title: "Spend", dataIndex: "value", align: "right", render: (v: number) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{usd(v, 4)}</span> },
            ]}
          />
        )}
      </Card>
      <Row gutter={16}>
        <Col xs={24} xl={14}>
          <Card title="By model">
            <Table
              size="small"
              rowKey={(r) => `${r.kind}:${r.model}`}
              dataSource={[...(u?.by_model ?? [])].sort((a, b) => b.cost_usd - a.cost_usd)}
              pagination={false}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No usage in this period" /> }}
              columns={[
                { title: "Model", dataIndex: "model", render: (m: string, r) => <span><code>{m}</code> <Tag>{r.kind}</Tag></span> },
                { title: "Requests", dataIndex: "requests", align: "right", width: 100, render: (n: number, r) => (r.kind === "compute" ? "—" : n.toLocaleString()) },
                {
                  title: "Tokens in / out · time",
                  key: "tok",
                  align: "right",
                  width: 150,
                  render: (_, r) => (r.kind === "compute" ? `${(r.seconds / 3600).toFixed(1)} h` : `${tokens(r.input_tokens)} / ${tokens(r.output_tokens)}`),
                },
                { title: "Cost", dataIndex: "cost_usd", align: "right", width: 110, render: (v: number) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{usd(v, 4)}</span> },
              ]}
            />
          </Card>
        </Col>
        <Col xs={24} xl={10}>
          <Card title="By project">
            <Table
              size="small"
              rowKey={(r) => r.project_id ?? "none"}
              dataSource={[...(u?.by_project ?? [])].sort((a, b) => b.cost_usd - a.cost_usd)}
              pagination={false}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No usage in this period" /> }}
              columns={[
                { title: "Project", dataIndex: "project_name" },
                { title: "Cost", dataIndex: "cost_usd", align: "right", width: 110, render: (v: number) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{usd(v, 4)}</span> },
              ]}
            />
          </Card>
        </Col>
      </Row>
    </Flex>
  );
}

// ------------------------------------------------------------------- models

function ProviderModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { org } = useAuth();
  const { message } = App.useApp();
  const invalidate = useInvalidate();
  const [form] = Form.useForm();
  const kind = Form.useWatch("kind", form);
  const [saving, setSaving] = React.useState(false);
  const submit = async (values: Record<string, unknown>) => {
    setSaving(true);
    try {
      await api.post(`/api/v1/orgs/${org!.id}/providers`, values);
      await invalidate(["org-models", org!.id]);
      message.success("Provider added");
      form.resetFields();
      onClose();
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not add the provider");
    } finally {
      setSaving(false);
    }
  };
  return (
    <Modal open={open} onCancel={onClose} title="Connect your own provider" okText="Add provider" onOk={() => form.submit()} confirmLoading={saving} destroyOnHidden>
      <Typography.Paragraph type="secondary">
        Bring your own API key. Calls routed to it are billed by your provider, not deducted from your credit. The key is encrypted at rest and never shown
        again.
      </Typography.Paragraph>
      <Form form={form} layout="vertical" onFinish={submit} initialValues={{ kind: "openai" }} requiredMark={false}>
        <Form.Item name="name" label="Name" rules={[{ required: true, max: 120 }]}>
          <Input placeholder="Our OpenAI account" />
        </Form.Item>
        <Form.Item name="kind" label="API format">
          <Select
            options={[
              { value: "openai", label: "OpenAI-compatible (OpenAI, DeepSeek, vLLM, OpenRouter, …)" },
              { value: "anthropic", label: "Anthropic" },
              { value: "litellm", label: "LiteLLM proxy" },
            ]}
          />
        </Form.Item>
        <Form.Item
          name="api_base"
          label="API base URL"
          extra={kind === "anthropic" ? "Leave empty for https://api.anthropic.com" : kind === "openai" ? "Leave empty for https://api.openai.com/v1" : "Your proxy's URL"}
          rules={[{ type: "url" }, { required: kind === "litellm" }]}
        >
          <Input placeholder={kind === "anthropic" ? "https://api.anthropic.com" : "https://api.openai.com/v1"} />
        </Form.Item>
        <Form.Item name="api_key" label="API key" rules={[{ required: true }]}>
          <Input.Password autoComplete="off" />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function RouteModal({ open, onClose, providers }: { open: boolean; onClose: () => void; providers: Provider[] }) {
  const { org } = useAuth();
  const { message } = App.useApp();
  const invalidate = useInvalidate();
  const [form] = Form.useForm();
  const [saving, setSaving] = React.useState(false);
  const submit = async (values: Record<string, unknown>) => {
    setSaving(true);
    try {
      await api.post(`/api/v1/orgs/${org!.id}/models`, values);
      await invalidate(["org-models", org!.id]);
      message.success("Model added");
      form.resetFields();
      onClose();
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not add the model");
    } finally {
      setSaving(false);
    }
  };
  return (
    <Modal open={open} onCancel={onClose} title="Add a model" okText="Add model" onOk={() => form.submit()} confirmLoading={saving} destroyOnHidden>
      <Form form={form} layout="vertical" onFinish={submit} initialValues={{ kind: "chat", provider_id: providers[0]?.id }} requiredMark={false}>
        <Form.Item name="provider_id" label="Provider" rules={[{ required: true }]}>
          <Select options={providers.map((p) => ({ value: p.id, label: `${p.name} (${p.kind})` }))} />
        </Form.Item>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="upstream_model" label="Provider's model name" rules={[{ required: true }]}>
              <Input placeholder="gpt-5.5" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="alias" label="Name in this platform" rules={[{ required: true, max: 160 }]} extra="What projects select; may equal the provider's name.">
              <Input placeholder="our-gpt-5.5" />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="kind" label="Type">
          <Segmented
            options={[
              { value: "chat", label: "Chat / completion" },
              { value: "embedding", label: "Embedding" },
            ]}
          />
        </Form.Item>
        <Form.Item name="description" label="Description">
          <Input />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function ModelsTab() {
  const { org } = useAuth();
  const { message } = App.useApp();
  const invalidate = useInvalidate();
  const canAdmin = atLeast(org?.role, "admin");
  const [adding, setAdding] = React.useState<"provider" | "route" | null>(null);
  const catalog = useQuery({
    queryKey: ["org-models", org?.id],
    queryFn: () => api.get<{ models: ModelRoute[]; providers: Provider[]; mock_enabled: boolean }>(`/api/v1/orgs/${org!.id}/models`),
    enabled: !!org,
  });
  const providers = catalog.data?.providers ?? [];
  const models = catalog.data?.models ?? [];
  const providerName = (id: string) => providers.find((p) => p.id === id)?.name;

  const remove = async (path: string, done: string) => {
    try {
      await api.delete(`/api/v1/orgs/${org!.id}/${path}`);
      await invalidate(["org-models", org!.id]);
      message.success(done);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Failed");
    }
  };

  const price = (m: ModelRoute) =>
    m.org_id
      ? "billed by your provider"
      : m.kind === "embedding"
        ? `${usd(m.pricing_usd_per_mtok.input)}/M`
        : `${usd(m.pricing_usd_per_mtok.input)} in · ${usd(m.pricing_usd_per_mtok.output)} out /M`;

  return (
    <Flex vertical gap={16}>
      {catalog.data?.mock_enabled && (
        <Alert type="info" showIcon title="Demo mode" description="This deployment answers unknown model names with a deterministic mock model, so you can try everything without spending credit." />
      )}
      <Card
        title="Models available to your projects"
        extra={
          canAdmin && (
            <Button icon={<PlusOutlined />} disabled={!providers.length} onClick={() => setAdding("route")}>
              Add a model
            </Button>
          )
        }
      >
        <Table<ModelRoute>
          rowKey="id"
          loading={catalog.isLoading}
          dataSource={models}
          pagination={false}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No models configured" /> }}
          columns={[
            {
              title: "Model",
              key: "alias",
              render: (_, m) => (
                <div>
                  <code>{m.alias}</code> <Tag>{m.kind}</Tag>
                  {m.org_id ? <Tag color="purple">your key</Tag> : <Tag color="blue">platform</Tag>}
                  {!m.enabled && <Tag color="red">disabled</Tag>}
                  {m.description && (
                    <div>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {m.description}
                      </Typography.Text>
                    </div>
                  )}
                </div>
              ),
            },
            { title: "Provider model", key: "upstream", render: (_, m) => <span className="mono">{m.org_id ? `${providerName(m.provider_id) ?? "?"} / ` : ""}{m.upstream_model}</span> },
            { title: "Price per million tokens", key: "price", width: 260, render: (_, m) => price(m) },
            {
              title: "",
              key: "actions",
              width: 56,
              render: (_, m) =>
                canAdmin &&
                m.org_id && (
                  <Popconfirm title={`Remove ${m.alias}?`} onConfirm={() => remove(`models/${m.id}`, "Model removed")}>
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                ),
            },
          ]}
        />
      </Card>
      <Card
        title="Your providers (bring your own key)"
        extra={
          canAdmin && (
            <Button icon={<ApiOutlined />} onClick={() => setAdding("provider")}>
              Connect a provider
            </Button>
          )
        }
      >
        <Table<Provider>
          rowKey="id"
          dataSource={providers}
          pagination={false}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="Using platform models only" /> }}
          columns={[
            { title: "Name", dataIndex: "name" },
            { title: "Format", dataIndex: "kind", width: 120, render: (k: string) => <Tag>{k}</Tag> },
            { title: "API base", dataIndex: "api_base", render: (v: string) => <span className="mono">{v}</span> },
            { title: "Key", dataIndex: "has_api_key", width: 80, render: (v: boolean) => (v ? <CheckOutlined /> : "—") },
            {
              title: "",
              key: "actions",
              width: 56,
              render: (_, p) =>
                canAdmin && (
                  <Popconfirm title={`Remove ${p.name}?`} description="Models routed to it are removed too." onConfirm={() => remove(`providers/${p.id}`, "Provider removed")}>
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                ),
            },
          ]}
        />
      </Card>
      <ProviderModal open={adding === "provider"} onClose={() => setAdding(null)} />
      <RouteModal open={adding === "route"} onClose={() => setAdding(null)} providers={providers} />
    </Flex>
  );
}

// ----------------------------------------------------------------- API keys

function ApiKeysTab() {
  const { org } = useAuth();
  const { message } = App.useApp();
  const invalidate = useInvalidate();
  const [form] = Form.useForm<{ name: string; role: Role }>();
  const [creating, setCreating] = React.useState(false);
  const [secret, setSecret] = React.useState<ApiKey | null>(null);
  const keys = useQuery({
    queryKey: ["api-keys", org?.id],
    queryFn: () => api.get<{ api_keys: ApiKey[] }>(`/api/v1/orgs/${org!.id}/api-keys`),
    enabled: !!org && atLeast(org.role, "admin"),
  });

  const create = async (values: { name: string; role: Role }) => {
    setCreating(true);
    try {
      setSecret(await api.post<ApiKey>(`/api/v1/orgs/${org!.id}/api-keys`, values));
      form.resetFields();
      await invalidate(["api-keys", org!.id]);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not create the key");
    } finally {
      setCreating(false);
    }
  };

  const revoke = async (key: ApiKey) => {
    try {
      await api.delete(`/api/v1/orgs/${org!.id}/api-keys/${key.id}`);
      await invalidate(["api-keys", org!.id]);
      message.success("Key revoked");
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not revoke");
    }
  };

  const origin = window.location.origin;
  return (
    <AdminOnly>
      <Flex vertical gap={16}>
        <Card title="Create a key">
          <Typography.Paragraph type="secondary">
            For scripts, CI and local tools: call the REST API (<a href="/api/docs" target="_blank" rel="noreferrer">reference</a>), the LLM gateway (OpenAI- and
            Anthropic-compatible, metered against this organization) and the dataset hub.
          </Typography.Paragraph>
          <Form form={form} layout="inline" onFinish={create} initialValues={{ role: "member" }} style={{ rowGap: 8 }}>
            <Form.Item name="name" rules={[{ required: true, max: 200 }]}>
              <Input placeholder="Key name, e.g. analysis notebook" style={{ width: 280 }} />
            </Form.Item>
            <Form.Item name="role">
              <Select
                style={{ width: 140 }}
                options={ROLES.filter((r) => r.value !== "owner" && atLeast(org?.role, r.value)).map((r) => ({ value: r.value, label: r.label }))}
              />
            </Form.Item>
            <Button type="primary" htmlType="submit" icon={<KeyOutlined />} loading={creating}>
              Create key
            </Button>
          </Form>
        </Card>
        <Card title="Keys">
          <Table<ApiKey>
            rowKey="id"
            loading={keys.isLoading}
            dataSource={keys.data?.api_keys ?? []}
            pagination={{ pageSize: 20, hideOnSinglePage: true }}
            columns={[
              { title: "Name", dataIndex: "name" },
              { title: "Key", dataIndex: "prefix", render: (p: string) => <span className="mono">{p}…</span> },
              { title: "Role", dataIndex: "role", width: 100, render: (r: string) => <Tag>{r}</Tag> },
              { title: "Created", dataIndex: "created_at", width: 140, render: (v: string) => fromNow(v) },
              { title: "Last used", dataIndex: "last_used_at", width: 140, render: (v: string | null) => (v ? fromNow(v) : "never") },
              {
                title: "",
                key: "actions",
                width: 110,
                render: (_, k) =>
                  k.revoked_at ? (
                    <Tooltip title={dateTime(k.revoked_at)}>
                      <Tag icon={<StopOutlined />}>revoked</Tag>
                    </Tooltip>
                  ) : (
                    <Popconfirm title={`Revoke ${k.name}?`} description="Anything using it stops working immediately." onConfirm={() => revoke(k)}>
                      <Button size="small" danger>
                        Revoke
                      </Button>
                    </Popconfirm>
                  ),
              },
            ]}
          />
        </Card>
      </Flex>
      <Modal open={!!secret} onCancel={() => setSecret(null)} title="Your new API key" footer={<Button type="primary" onClick={() => setSecret(null)}>Done</Button>} width={680}>
        <Alert type="warning" showIcon title="Copy it now. It is not shown again." style={{ marginBottom: 12 }} />
        <Typography.Paragraph copyable={{ text: secret?.secret, icon: <CopyOutlined /> }} className="mono" style={{ wordBreak: "break-all", fontSize: 13 }}>
          {secret?.secret}
        </Typography.Paragraph>
        <Typography.Text type="secondary">Examples</Typography.Text>
        <pre className="log-view" style={{ marginTop: 6 }}>{`# REST API
curl -H "Authorization: Bearer $KEY" ${origin}/api/v1/orgs/${org?.id}/projects

# LLM gateway (OpenAI-compatible)
OPENAI_BASE_URL=${origin}/llm/v1  OPENAI_API_KEY=$KEY

# Local AgentSociety workspace (.env)
AGENTSOCIETY_LLM_API_BASE=${origin}/llm/v1
AGENTSOCIETY_LLM_API_KEY=$KEY`}</pre>
      </Modal>
    </AdminOnly>
  );
}

// ---------------------------------------------------------------------- page

const TITLES: Record<string, string> = { members: "Members", billing: "Billing", usage: "Usage", models: "Models", "api-keys": "API keys" };

function Section({ name, children }: { name: string; children: React.ReactNode }) {
  const { data: org } = useCurrentOrg();
  return (
    <div className="page">
      <Flex align="baseline" gap={12} style={{ marginBottom: 16 }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          {TITLES[name]}
        </Typography.Title>
        <Typography.Text type="secondary">{org?.name}</Typography.Text>
      </Flex>
      {children}
    </div>
  );
}

export default function OrgPage() {
  return (
    <Routes>
      <Route index element={<Navigate to="members" replace />} />
      <Route path="members" element={<Section name="members"><MembersTab /></Section>} />
      <Route path="billing" element={<Section name="billing"><BillingTab /></Section>} />
      <Route path="usage" element={<Section name="usage"><UsageTab /></Section>} />
      <Route path="models" element={<Section name="models"><ModelsTab /></Section>} />
      <Route path="api-keys" element={<Section name="api-keys"><ApiKeysTab /></Section>} />
      <Route path="*" element={<Navigate to="members" replace />} />
    </Routes>
  );
}
