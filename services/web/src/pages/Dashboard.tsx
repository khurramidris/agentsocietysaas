import { ExperimentOutlined, PlusOutlined, RocketOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Col, Empty, Flex, Form, Input, Modal, Row, Select, Skeleton, Switch, Typography } from "antd";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { api, ApiError } from "@/api/client";
import type { Project } from "@/api/types";
import { atLeast, useAuth } from "@/auth";
import { SandboxBadge } from "@/components/SandboxBadge";
import { useCurrentOrg } from "@/layout/AppLayout";
import { fromNow } from "@/lib/format";
import { useMeta } from "@/meta";

const EXAMPLE_TOPICS = [
  "How do recommendation feeds shape political polarization over time?",
  "Does a universal basic income change labour participation in a simulated town?",
  "How does misinformation spread differently in tightly vs loosely connected communities?",
];

export function NewProjectModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { org } = useAuth();
  const { data: orgDetail } = useCurrentOrg();
  const { data: meta } = useMeta();
  const [form] = Form.useForm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const create = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.post<Project>(`/api/v1/orgs/${org!.id}/projects`, values),
    onSuccess: async (project) => {
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
      onClose();
      form.resetFields();
      if (project.sandbox.error) message.warning(project.sandbox.error);
      navigate(`/p/${project.id}`);
    },
    onError: (e) => message.error(e instanceof ApiError ? e.message : "Could not create the project"),
  });
  const sizes = orgDetail?.limits.sandbox_sizes ?? ["small"];

  return (
    <Modal
      title="New research project"
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText="Create project"
      confirmLoading={create.isPending}
      width={620}
      destroyOnHidden
    >
      <Typography.Paragraph type="secondary">
        Each project is a private research workspace with the full AgentSociety toolkit: an AI social scientist, literature search, LLM agent
        simulations, replay, analysis and paper writing.
      </Typography.Paragraph>
      <Form form={form} layout="vertical" initialValues={{ sandbox_size: "small", start: true }} onFinish={(v) => create.mutate(v)} requiredMark={false}>
        <Form.Item name="name" label="Project name" rules={[{ required: true, max: 200 }]}>
          <Input placeholder="Echo chambers" autoFocus />
        </Form.Item>
        <Form.Item name="topic" label="Research question" extra="Written to TOPIC.md; the AI social scientist starts from it.">
          <Input.TextArea rows={3} placeholder={EXAMPLE_TOPICS[0]} />
        </Form.Item>
        <Flex gap={8} wrap style={{ marginTop: -12, marginBottom: 16 }}>
          {EXAMPLE_TOPICS.map((t) => (
            <Button key={t} size="small" type="dashed" onClick={() => form.setFieldsValue({ topic: t })}>
              {t.length > 52 ? `${t.slice(0, 52)}…` : t}
            </Button>
          ))}
        </Flex>
        <Row gutter={16}>
          <Col span={14}>
            <Form.Item name="sandbox_size" label="Workspace size">
              <Select
                options={Object.entries(meta?.sandbox_sizes ?? { small: { cpus: 2, memory_gb: 4 } }).map(([size, spec]) => ({
                  value: size,
                  disabled: !sizes.includes(size),
                  label: `${size} — ${spec.cpus} vCPU, ${spec.memory_gb} GB${meta ? ` · $${meta.sandbox_hourly_usd[size]?.toFixed(2)}/h` : ""}${sizes.includes(size) ? "" : " (upgrade)"}`,
                }))}
              />
            </Form.Item>
          </Col>
          <Col span={10}>
            <Form.Item name="start" label="Start workspace now" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Col>
        </Row>
      </Form>
    </Modal>
  );
}

export function DashboardPage() {
  const { org } = useAuth();
  const [open, setOpen] = React.useState(false);
  const navigate = useNavigate();
  const { data, isLoading } = useQuery({
    queryKey: ["projects", org?.id],
    queryFn: () => api.get<{ projects: Project[] }>(`/api/v1/orgs/${org!.id}/projects`),
    enabled: !!org,
    refetchInterval: 15_000,
  });
  const canCreate = atLeast(org?.role, "member");
  const projects = data?.projects ?? [];

  return (
    <div className="page">
      <Flex justify="space-between" align="center" style={{ marginBottom: 20 }}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            Projects
          </Typography.Title>
          <Typography.Text type="secondary">{org?.name}</Typography.Text>
        </div>
        {canCreate && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            New project
          </Button>
        )}
      </Flex>
      {isLoading ? (
        <Skeleton active />
      ) : projects.length === 0 ? (
        <Card>
          <Empty
            image={<RocketOutlined style={{ fontSize: 48, color: "#1f4fd8" }} />}
            description={
              <span>
                No projects yet. Create one and ask the AI social scientist to take a research question from literature review to a draft paper.
              </span>
            }
          >
            {canCreate && (
              <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
                Create your first project
              </Button>
            )}
          </Empty>
        </Card>
      ) : (
        <Row gutter={[16, 16]}>
          {projects.map((p) => (
            <Col key={p.id} xs={24} md={12} xl={8}>
              <Card hoverable onClick={() => navigate(`/p/${p.id}`)} style={{ height: "100%" }} styles={{ body: { display: "flex", flexDirection: "column", gap: 8, height: "100%" } }}>
                <Flex justify="space-between" align="start" gap={8}>
                  <Typography.Title level={5} style={{ margin: 0 }} ellipsis>
                    <ExperimentOutlined style={{ color: "#1f4fd8", marginRight: 8 }} />
                    {p.name}
                  </Typography.Title>
                  <SandboxBadge status={p.sandbox.status} />
                </Flex>
                <Typography.Paragraph type="secondary" ellipsis={{ rows: 3 }} style={{ marginBottom: 0, flex: 1 }}>
                  {p.topic || p.description || "No research question yet."}
                </Typography.Paragraph>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  Updated {fromNow(p.updated_at)} · {p.sandbox.size}
                </Typography.Text>
              </Card>
            </Col>
          ))}
        </Row>
      )}
      <NewProjectModal open={open} onClose={() => setOpen(false)} />
    </div>
  );
}
