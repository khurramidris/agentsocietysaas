import { ClearOutlined, CodeOutlined, ReloadOutlined, RobotOutlined, ScanOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Col, Popconfirm, Row, Space, Statistic, Table, Tabs, Typography } from "antd";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { api, ApiError, as2, sbx } from "@/api/client";
import type { ChatSession } from "@/api/types";
import { ModuleCatalogView } from "@/upstream/embeds";
import { MarkdownRenderer } from "@upstream/components/MarkdownRenderer";

import { SandboxGate, useProjectCtx } from "./context";

interface CustomModule {
  type: string;
  class_name: string;
  description: string;
  init_description?: string;
  is_custom: boolean;
}

interface CustomList {
  success: boolean;
  agents: CustomModule[];
  envs: CustomModule[];
  total_agents: number;
  total_envs: number;
}

const PROMPTS = {
  env: "Create a custom environment module for this research question (for example a small online community where agents post, follow and like), register it, write a test and run it. Explain how to use it in an experiment.",
  agent: "Create a custom agent class for this research question with the profile fields and decision logic the hypothesis needs, register it, test it and explain how to use it in an experiment.",
};

function CustomModules() {
  const { project, canWrite } = useProjectCtx();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [busy, setBusy] = React.useState<string | null>(null);
  const list = useQuery({ queryKey: ["custom-modules", project.id], queryFn: () => api.get<CustomList>(as2(project.id, "api/v1/custom/list")) });

  const act = async (name: string, fn: () => Promise<string>) => {
    setBusy(name);
    try {
      message.success(await fn());
      await queryClient.invalidateQueries({ queryKey: ["custom-modules", project.id] });
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Failed");
    } finally {
      setBusy(null);
    }
  };

  const ask = async (prompt: string, title: string) => {
    try {
      const session = await api.post<ChatSession>(sbx(project.id, "chat/sessions"), { title });
      await api.post(sbx(project.id, `chat/sessions/${session.id}/messages`), { content: prompt });
      navigate(`/p/${project.id}/chat/${session.id}`);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not reach the AI scientist");
    }
  };

  // Rows come from the upstream backend's /custom/list (class name, short and full description).
  const columns = [
    { title: "Class", key: "class", width: 260, render: (r: CustomModule) => <code>{r.class_name}</code> },
    { title: "Description", key: "description", render: (r: CustomModule) => r.description || "—" },
  ];
  const table = (rows: CustomModule[] | undefined) => (
    <Table<CustomModule>
      size="small"
      rowKey="class_name"
      loading={list.isLoading}
      dataSource={rows ?? []}
      columns={columns}
      pagination={false}
      locale={{ emptyText: "None registered yet. Write one under custom/ (or ask the AI scientist), then Scan & register." }}
      expandable={{
        rowExpandable: (r) => !!r.init_description,
        expandedRowRender: (r) => (
          <div style={{ maxWidth: 900 }}>
            <MarkdownRenderer content={r.init_description ?? ""} isDark={false} />
          </div>
        ),
      }}
    />
  );

  return (
    <Flex2>
      <Row gutter={16}>
        <Col xs={24} md={8}>
          <Card>
            <Statistic title="Custom agents" value={list.data?.total_agents ?? 0} />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card>
            <Statistic title="Custom environment modules" value={list.data?.total_envs ?? 0} />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card styles={{ body: { display: "flex", flexDirection: "column", gap: 8 } }}>
            {canWrite && (
              <>
                <Button icon={<RobotOutlined />} onClick={() => ask(PROMPTS.env, "Create an environment module")}>
                  Create an environment module with AI
                </Button>
                <Button icon={<RobotOutlined />} onClick={() => ask(PROMPTS.agent, "Create an agent class")}>
                  Create an agent class with AI
                </Button>
              </>
            )}
          </Card>
        </Col>
      </Row>
      <Card
        title="Custom modules in custom/"
        extra={
          <Space>
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void list.refetch()} />
            {canWrite && (
              <>
                <Button
                  size="small"
                  icon={<ScanOutlined />}
                  loading={busy === "scan"}
                  onClick={() =>
                    act("scan", async () => {
                      const r = await api.post<{ agents_found: number; envs_found: number; errors: string[] }>(as2(project.id, "api/v1/custom/scan"), {});
                      await api.post(sbx(project.id, "workspace/module-info/refresh"));
                      return `Found ${r.agents_found} agents and ${r.envs_found} environment modules${r.errors.length ? ` (${r.errors.length} errors)` : ""}`;
                    })
                  }
                >
                  Scan & register
                </Button>
                <Popconfirm title="Remove generated registrations for custom modules?" onConfirm={() => act("clean", async () => (await api.post<{ message: string }>(as2(project.id, "api/v1/custom/clean"), {})).message)}>
                  <Button size="small" icon={<ClearOutlined />} loading={busy === "clean"}>
                    Clean
                  </Button>
                </Popconfirm>
                <Button size="small" icon={<CodeOutlined />} onClick={() => navigate(`/p/${project.id}/files?path=custom`)}>
                  Open custom/
                </Button>
              </>
            )}
          </Space>
        }
      >
        <Tabs
          items={[
            { key: "agents", label: "Agents", children: table(list.data?.agents) },
            { key: "envs", label: "Environment modules", children: table(list.data?.envs) },
          ]}
        />
      </Card>
    </Flex2>
  );
}

function Flex2({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>{children}</div>;
}

export default function Modules() {
  const { project } = useProjectCtx();
  const [tab, setTab] = React.useState("catalog");
  return (
    <div style={{ maxWidth: 1400, margin: "0 auto" }}>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        Agents & environment modules
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        Every agent class and environment module AgentSociety can simulate, with the parameters your experiments pre-fill. Custom modules you or the AI
        scientist write under <code>custom/</code> appear here once registered.
      </Typography.Paragraph>
      <SandboxGate what="the module catalog">
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            { key: "catalog", label: "Catalog & parameters", children: tab === "catalog" ? <ModuleCatalogView projectId={project.id} /> : null },
            { key: "custom", label: "Custom modules", children: <CustomModules /> },
          ]}
        />
      </SandboxGate>
    </div>
  );
}
