import { PlayCircleOutlined, QuestionCircleOutlined, ReloadOutlined, StopOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Col, Drawer, Flex, Input, Row, Select, Space, Table, Tag, Typography } from "antd";
import * as React from "react";

import { api, ApiError, sbx } from "@/api/client";
import type { ToolJob } from "@/api/types";
import { dateTime } from "@/lib/format";

import { SandboxGate, useProjectCtx } from "./context";

/** The research tools of the AgentSociety skills (TOOL_SCRIPTS in the extension's ags.py). */
export const TOOLS: { name: string; summary: string; example: string }[] = [
  { name: "literature-search", summary: "Search the literature and save papers to papers/ with a summary.", example: '"opinion dynamics echo chambers" --limit 10' },
  { name: "literature-full-text", summary: "Fetch full texts for saved papers.", example: "--help" },
  { name: "research-pipeline", summary: "Inspect and advance the research pipeline stages.", example: "where-am-i --json" },
  { name: "hypothesis", summary: "Create, list, inspect or delete hypotheses.", example: "list --json" },
  { name: "scan-modules", summary: "List agent classes and environment modules available to experiments.", example: "--help" },
  { name: "experiment-config", summary: "Generate and validate experiment configurations.", example: "--help" },
  { name: "run-experiment", summary: "Start, monitor or stop simulation runs.", example: "--help" },
  { name: "analysis", summary: "Run the analysis harness: EDA, statistics, figures and HTML reports.", example: "--help" },
  { name: "create-agent", summary: "Scaffold a custom agent class under custom/agents.", example: "--help" },
  { name: "create-env-module-validate", summary: "Validate a custom environment module.", example: "--help" },
  { name: "create-env-module-resolve-sources", summary: "Resolve the sources a custom environment module needs.", example: "--help" },
  { name: "use-dataset", summary: "Search and download datasets from the dataset hub.", example: "search" },
  { name: "create-dataset", summary: "Package, validate and publish a dataset.", example: "--help" },
];

/** Split a command line like a POSIX shell (quotes and backslash escapes, no expansion). */
export function splitArgs(line: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quote: '"' | "'" | null = null;
  let has = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quote) {
      if (ch === quote) quote = null;
      else if (ch === "\\" && quote === '"' && i + 1 < line.length) cur += line[++i];
      else cur += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      has = true;
    } else if (ch === "\\" && i + 1 < line.length) {
      cur += line[++i];
      has = true;
    } else if (/\s/.test(ch)) {
      if (has || cur) out.push(cur);
      cur = "";
      has = false;
    } else {
      cur += ch;
    }
  }
  if (has || cur) out.push(cur);
  return out;
}

const STATUS: Record<string, string> = { running: "processing", succeeded: "success", failed: "error", cancelled: "warning", timeout: "error" };

function ToolsInner() {
  const { project, canWrite } = useProjectCtx();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [tool, setTool] = React.useState(TOOLS[2].name);
  const [args, setArgs] = React.useState(TOOLS[2].example);
  const [running, setRunning] = React.useState(false);
  const [open, setOpen] = React.useState<string | null>(null);

  const jobs = useQuery({
    queryKey: ["jobs", project.id],
    queryFn: () => api.get<{ jobs: ToolJob[] }>(sbx(project.id, "tools/jobs")),
    refetchInterval: (q) => (q.state.data?.jobs.some((j) => j.status === "running") ? 2000 : 15_000),
  });
  const job = useQuery({
    queryKey: ["job", project.id, open],
    queryFn: () => api.get<ToolJob>(sbx(project.id, `tools/jobs/${open}`)),
    enabled: !!open,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false),
  });

  const run = async (argv: string[]) => {
    setRunning(true);
    try {
      const started = await api.post<ToolJob>(sbx(project.id, "tools/run"), { tool, args: argv, wait: false, timeout: 3600 });
      setOpen(started.id);
      await queryClient.invalidateQueries({ queryKey: ["jobs", project.id] });
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not start the tool");
    } finally {
      setRunning(false);
    }
  };

  const current = TOOLS.find((t) => t.name === tool);
  return (
    <div style={{ maxWidth: 1280, margin: "0 auto" }}>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        Research tools
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        The command-line tools behind the AgentSociety research skills, run inside the workspace (the AI scientist uses the same tools).
      </Typography.Paragraph>
      <Row gutter={16}>
        <Col xs={24} lg={10}>
          <Card title="Run a tool">
            <Flex vertical gap={12}>
              <Select
                value={tool}
                onChange={(v) => {
                  setTool(v);
                  setArgs(TOOLS.find((t) => t.name === v)?.example ?? "");
                }}
                options={TOOLS.map((t) => ({ value: t.name, label: t.name }))}
                showSearch
              />
              <Typography.Text type="secondary">{current?.summary}</Typography.Text>
              <Input.TextArea className="mono" rows={3} value={args} onChange={(e) => setArgs(e.target.value)} placeholder="arguments" />
              <Space>
                <Button type="primary" icon={<PlayCircleOutlined />} loading={running} disabled={!canWrite} onClick={() => run(splitArgs(args))}>
                  Run
                </Button>
                <Button icon={<QuestionCircleOutlined />} disabled={!canWrite} onClick={() => run(["--help"])}>
                  Help
                </Button>
              </Space>
            </Flex>
          </Card>
        </Col>
        <Col xs={24} lg={14}>
          <Card title="Runs" extra={<Button size="small" icon={<ReloadOutlined />} onClick={() => void jobs.refetch()} />}>
            <Table
              size="small"
              rowKey="id"
              dataSource={jobs.data?.jobs ?? []}
              pagination={{ pageSize: 10, hideOnSinglePage: true }}
              onRow={(j) => ({ onClick: () => setOpen(j.id), style: { cursor: "pointer" } })}
              columns={[
                { title: "Tool", dataIndex: "tool", render: (t: string) => <code>{t}</code> },
                { title: "Arguments", dataIndex: "args", ellipsis: true, render: (a: string[]) => <span className="mono">{a.join(" ")}</span> },
                { title: "Status", dataIndex: "status", width: 110, render: (s: string) => <Tag color={STATUS[s]}>{s}</Tag> },
                { title: "Started", dataIndex: "started_at", width: 150, render: dateTime },
              ]}
            />
          </Card>
        </Col>
      </Row>
      <Drawer
        open={!!open}
        onClose={() => setOpen(null)}
        size={820}
        title={job.data ? `${job.data.tool} ${job.data.args.join(" ")}` : "Run"}
        extra={
          job.data?.status === "running" &&
          canWrite && (
            <Button danger icon={<StopOutlined />} onClick={() => api.post(sbx(project.id, `tools/jobs/${open}/cancel`)).then(() => job.refetch())}>
              Cancel
            </Button>
          )
        }
      >
        {job.data && (
          <>
            <Space style={{ marginBottom: 12 }}>
              <Tag color={STATUS[job.data.status]}>{job.data.status}</Tag>
              {job.data.exit_code !== null && <Typography.Text type="secondary">exit code {job.data.exit_code}</Typography.Text>}
            </Space>
            <pre className="log-view" style={{ minHeight: 200 }}>
              {job.data.output || (job.data.status === "running" ? "Running…" : "No output")}
            </pre>
          </>
        )}
      </Drawer>
    </div>
  );
}

export default function Tools() {
  return (
    <SandboxGate what="the research tools">
      <ToolsInner />
    </SandboxGate>
  );
}
