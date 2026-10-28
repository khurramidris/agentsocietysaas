import {
  CaretRightOutlined,
  ClearOutlined,
  EditOutlined,
  FileTextOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  SettingOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Descriptions, Drawer, Empty, Flex, Form, Input, InputNumber, Modal, Popconfirm, Select, Space, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { api, ApiError, sbx, streamSSE } from "@/api/client";
import type { ChatSession, ExperimentStatus, Hypothesis } from "@/api/types";
import { bytes, dateTime } from "@/lib/format";

import { SandboxGate, useProjectCtx } from "./context";

const STATUS_COLORS: Record<string, string> = {
  running: "processing",
  completed: "success",
  failed: "error",
  stopped: "warning",
  configured: "blue",
  not_initialized: "default",
};

export function StatusTag({ status }: { status: string }) {
  return <Tag color={STATUS_COLORS[status] ?? "default"}>{status.replace(/_/g, " ")}</Tag>;
}

interface ExperimentDetail extends ExperimentStatus {
  init_config: { env_modules?: { module_type: string }[]; agents?: { agent_type: string }[] } | null;
  steps: { start_t?: string; steps?: { type: string }[] } | null;
  experiment_md: string | null;
  llm_stats: Record<string, unknown> | null;
  artifacts: { name: string; size: number; mtime: number }[];
}

function LogView({ projectId, h, e, running }: { projectId: string; h: string; e: string; running: boolean }) {
  const [text, setText] = React.useState("");
  const [source, setSource] = React.useState("auto");
  const ref = React.useRef<HTMLPreElement>(null);
  React.useEffect(() => {
    const controller = new AbortController();
    setText("");
    const path = sbx(projectId, `experiments/${h}/${e}/logs${running ? "/stream" : ""}`);
    if (running) {
      let buffer = "";
      void streamSSE(
        path,
        (ev) => {
          if (ev.event === "end") return;
          buffer += `${ev.data}\n`;
          setText(buffer.length > 400_000 ? buffer.slice(-400_000) : buffer);
        },
        controller.signal,
        { source, tail: 500 },
      ).catch(() => undefined);
    } else {
      api
        .get<{ content: string; exists: boolean }>(path, { source, tail: 2000 }, controller.signal)
        .then((r) => setText(r.exists ? r.content : "No log yet."))
        .catch(() => setText("Could not load the log."));
    }
    return () => controller.abort();
  }, [projectId, h, e, running, source]);
  React.useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [text]);
  return (
    <>
      <Flex justify="space-between" style={{ marginBottom: 8 }}>
        <Select
          size="small"
          value={source}
          onChange={setSource}
          style={{ width: 160 }}
          options={[
            { value: "auto", label: "Simulation log" },
            { value: "stdout.log", label: "stdout" },
            { value: "stderr.log", label: "stderr" },
          ]}
        />
        {running && <Tag color="processing">live</Tag>}
      </Flex>
      <pre ref={ref} className="log-view" style={{ height: "calc(100vh - 260px)" }}>
        {text}
      </pre>
    </>
  );
}

function ExperimentDrawer({ target, onClose }: { target: { h: string; e: string } | null; onClose: () => void }) {
  const { project, canWrite } = useProjectCtx();
  const navigate = useNavigate();
  const detail = useQuery({
    queryKey: ["experiment", project.id, target?.h, target?.e],
    queryFn: () => api.get<ExperimentDetail>(sbx(project.id, `experiments/${target!.h}/${target!.e}`)),
    enabled: !!target,
    refetchInterval: (q) => (q.state.data?.is_running ? 3000 : false),
  });
  const d = detail.data;
  const base = target ? `hypothesis_${target.h}/experiment_${target.e}` : "";
  return (
    <Drawer open={!!target} onClose={onClose} size={880} title={target ? `Hypothesis ${target.h} · Experiment ${target.e}` : ""} destroyOnHidden>
      {d && (
        <Tabs
          items={[
            {
              key: "overview",
              label: "Overview",
              children: (
                <Flex vertical gap={16}>
                  <Descriptions
                    size="small"
                    column={2}
                    bordered
                    items={[
                      { key: "s", label: "Status", children: <StatusTag status={d.status} /> },
                      { key: "steps", label: "Simulation steps", children: d.step_count ?? "—" },
                      { key: "start", label: "Started", children: dateTime(d.start_time) },
                      { key: "end", label: "Finished", children: dateTime(d.end_time) },
                      { key: "simt", label: "Simulated time", children: d.simulation_time ?? "—" },
                      { key: "res", label: "Resumed", children: d.resumed ? "yes" : "no" },
                      {
                        key: "agents",
                        label: "Agents",
                        children: d.init_config?.agents ? `${d.init_config.agents.length} (${[...new Set(d.init_config.agents.map((a) => a.agent_type))].join(", ")})` : "—",
                      },
                      { key: "env", label: "Environment modules", children: d.init_config?.env_modules?.map((m) => m.module_type).join(", ") || "—" },
                      { key: "plan", label: "Steps plan", children: d.steps?.steps ? d.steps.steps.map((s) => s.type).join(" → ") : "—", span: 2 },
                    ]}
                  />
                  {d.error && <Typography.Paragraph type="danger" style={{ whiteSpace: "pre-wrap" }}>{d.error}</Typography.Paragraph>}
                  {d.experiment_md && (
                    <Card size="small" title="EXPERIMENT.md">
                      <Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{d.experiment_md}</Typography.Paragraph>
                    </Card>
                  )}
                  <Space wrap>
                    <Button icon={<EditOutlined />} onClick={() => navigate(`/p/${project.id}/files?path=${encodeURIComponent(`${base}/init/init_config.json`)}`)}>
                      Edit agents & environment
                    </Button>
                    <Button icon={<FileTextOutlined />} onClick={() => navigate(`/p/${project.id}/files?path=${encodeURIComponent(`${base}/init/steps.yaml`)}`)}>
                      Edit steps
                    </Button>
                    {d.has_replay && (
                      <Button type="primary" icon={<CaretRightOutlined />} onClick={() => navigate(`/p/${project.id}/replay/${d.hypothesis_id}/${d.experiment_id}`)}>
                        Open replay
                      </Button>
                    )}
                    {canWrite && <Button onClick={() => navigate(`/p/${project.id}/files?path=${encodeURIComponent(`${base}/run`)}`)}>Browse run folder</Button>}
                  </Space>
                </Flex>
              ),
            },
            { key: "logs", label: "Logs", children: <LogView projectId={project.id} h={d.hypothesis_id} e={d.experiment_id} running={d.is_running} /> },
            {
              key: "artifacts",
              label: `Artifacts (${d.artifacts.length})`,
              children: (
                <Table
                  size="small"
                  rowKey="name"
                  dataSource={d.artifacts}
                  pagination={false}
                  columns={[
                    {
                      title: "File",
                      dataIndex: "name",
                      render: (name: string) => <a onClick={() => navigate(`/p/${project.id}/files?path=${encodeURIComponent(`${base}/run/artifacts/${name}`)}`)}>{name}</a>,
                    },
                    { title: "Size", dataIndex: "size", render: bytes, width: 100 },
                    { title: "Modified", dataIndex: "mtime", render: dateTime, width: 160 },
                  ]}
                />
              ),
            },
            {
              key: "llm",
              label: "LLM usage",
              children: d.llm_stats ? <pre className="log-view">{JSON.stringify(d.llm_stats, null, 2)}</pre> : <Empty description="No LLM statistics yet" />,
            },
          ]}
        />
      )}
    </Drawer>
  );
}

const SKELETON_CONFIG = {
  env_modules: [{ module_type: "SimpleSocialSpace", kwargs: { agent_id_name_pairs: [[1, "Alice"], [2, "Bob"]] } }],
  agents: [
    { agent_id: 1, agent_type: "PersonAgent", kwargs: { id: 1, name: "Alice", age: 34, occupation: "teacher" } },
    { agent_id: 2, agent_type: "PersonAgent", kwargs: { id: 2, name: "Bob", age: 51, occupation: "farmer" } },
  ],
};
const SKELETON_STEPS = `start_t: "2026-01-01T08:00:00"
steps:
  - type: run
    num_steps: 3
    tick: 3600
  - type: ask
    question: "What did you do today, and how do you feel about it?"
`;

function NewExperimentModal({ open, onClose, hypotheses }: { open: boolean; onClose: () => void; hypotheses: Hypothesis[] }) {
  const { project } = useProjectCtx();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [form] = Form.useForm();
  const nextH = String(Math.max(0, ...hypotheses.map((h) => Number(h.hypothesis_id) || 0)) + 1);

  const create = async () => {
    const { h, title, e } = await form.validateFields();
    const hypothesis = hypotheses.find((x) => x.hypothesis_id === String(h));
    const exp = String(e ?? Math.max(0, ...(hypothesis?.experiments ?? []).map((x) => Number(x.experiment_id) || 0)) + 1);
    const base = `hypothesis_${h}/experiment_${exp}`;
    try {
      if (!hypothesis) await api.put(sbx(project.id, "files/content"), { path: `hypothesis_${h}/HYPOTHESIS.md`, content: `# ${title || `Hypothesis ${h}`}\n` });
      await api.put(sbx(project.id, "files/content"), { path: `${base}/init/init_config.json`, content: `${JSON.stringify(SKELETON_CONFIG, null, 2)}\n` });
      await api.put(sbx(project.id, "files/content"), { path: `${base}/init/steps.yaml`, content: SKELETON_STEPS });
      await queryClient.invalidateQueries({ queryKey: ["experiments", project.id] });
      onClose();
      navigate(`/p/${project.id}/files?path=${encodeURIComponent(`${base}/init/init_config.json`)}`);
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : "Could not create the experiment");
    }
  };

  return (
    <Modal open={open} onCancel={onClose} onOk={create} okText="Create and edit" title="New experiment" destroyOnHidden>
      <Typography.Paragraph type="secondary">
        Creates <code>init/init_config.json</code> and <code>init/steps.yaml</code> from a small template and opens the visual editor. The AI social
        scientist can also design experiments for you from a hypothesis.
      </Typography.Paragraph>
      <Form form={form} layout="vertical" initialValues={{ h: hypotheses[0]?.hypothesis_id ?? nextH }}>
        <Form.Item name="h" label="Hypothesis" rules={[{ required: true }]}>
          <Select
            options={[
              ...hypotheses.map((h) => ({ value: h.hypothesis_id, label: `${h.hypothesis_id} · ${h.title ?? "untitled"}` })),
              { value: nextH, label: `${nextH} · new hypothesis` },
            ]}
          />
        </Form.Item>
        <Form.Item noStyle shouldUpdate>
          {() =>
            !hypotheses.some((h) => h.hypothesis_id === String(form.getFieldValue("h"))) && (
              <Form.Item name="title" label="Hypothesis statement">
                <Input placeholder="Engagement-ranked feeds increase polarisation" />
              </Form.Item>
            )
          }
        </Form.Item>
        <Form.Item name="e" label="Experiment number" extra="Leave empty for the next free number.">
          <InputNumber min={1} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function ExperimentsInner() {
  const { project, canWrite } = useProjectCtx();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = React.useState<{ h: string; e: string } | null>(null);
  const [creating, setCreating] = React.useState(false);
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["experiments", project.id],
    queryFn: () => api.get<{ hypotheses: Hypothesis[] }>(sbx(project.id, "experiments")),
    refetchInterval: (q) => (q.state.data?.hypotheses.some((h) => h.experiments.some((x) => x.is_running)) ? 3000 : 15_000),
  });

  const act = async (fn: () => Promise<unknown>, done: string) => {
    try {
      await fn();
      message.success(done);
      await queryClient.invalidateQueries({ queryKey: ["experiments", project.id] });
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Action failed");
    }
  };

  const askDesign = async () => {
    try {
      const session = await api.post<ChatSession>(sbx(project.id, "chat/sessions"), { title: "Design an experiment" });
      await api.post(sbx(project.id, `chat/sessions/${session.id}/messages`), {
        content: "Design an experiment for the most promising hypothesis in this workspace (create one from TOPIC.md if none exists), validate the configuration, then run it and summarise the results.",
      });
      navigate(`/p/${project.id}/chat/${session.id}`);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not reach the AI scientist");
    }
  };

  const hypotheses = data?.hypotheses ?? [];
  return (
    <div style={{ maxWidth: 1280, margin: "0 auto" }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            Hypotheses & experiments
          </Typography.Title>
          <Typography.Text type="secondary">Each experiment simulates an agent society with AgentSociety and records a replay.</Typography.Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void refetch()} />
          {canWrite && (
            <>
              <Button icon={<RobotOutlined />} onClick={askDesign}>
                Ask AI to design one
              </Button>
              <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>
                New experiment
              </Button>
            </>
          )}
        </Space>
      </Flex>
      {!isLoading && hypotheses.length === 0 && (
        <Card>
          <Empty description="No hypotheses yet. Ask the AI social scientist to propose some, or create an experiment by hand." />
        </Card>
      )}
      <Flex vertical gap={16}>
        {hypotheses.map((h) => (
          <Card
            key={h.hypothesis_id}
            title={
              <span>
                <Tag>H{h.hypothesis_id}</Tag>
                {h.title ?? "Untitled hypothesis"}
              </span>
            }
            extra={
              <Space>
                {h.has_hypothesis_md && (
                  <Button size="small" icon={<FileTextOutlined />} onClick={() => navigate(`/p/${project.id}/files?path=${encodeURIComponent(`hypothesis_${h.hypothesis_id}/HYPOTHESIS.md`)}`)}>
                    HYPOTHESIS.md
                  </Button>
                )}
                <Tooltip title="Agent classes and environment modules available to this hypothesis">
                  <Button size="small" icon={<SettingOutlined />} onClick={() => navigate(`/p/${project.id}/files?path=${encodeURIComponent(`hypothesis_${h.hypothesis_id}/SIM_SETTINGS.json`)}`)}>
                    SIM settings
                  </Button>
                </Tooltip>
              </Space>
            }
          >
            <Table
              size="small"
              rowKey={(x) => x.experiment_id}
              dataSource={h.experiments}
              pagination={false}
              locale={{ emptyText: "No experiments for this hypothesis yet" }}
              onRow={(x) => ({ onClick: () => setOpen({ h: x.hypothesis_id, e: x.experiment_id }), style: { cursor: "pointer" } })}
              columns={[
                { title: "Experiment", dataIndex: "experiment_id", width: 110, render: (id: string) => <Typography.Text strong>E{id}</Typography.Text> },
                { title: "Status", dataIndex: "status", width: 140, render: (s: string) => <StatusTag status={s} /> },
                { title: "Steps", dataIndex: "step_count", width: 80, render: (v: number | null) => v ?? "—" },
                { title: "Started", dataIndex: "start_time", render: dateTime },
                { title: "Finished", dataIndex: "end_time", render: dateTime },
                {
                  title: "",
                  key: "actions",
                  align: "right",
                  render: (_: unknown, x: ExperimentStatus) => (
                    <Space onClick={(ev) => ev.stopPropagation()}>
                      {x.has_replay && (
                        <Button size="small" icon={<CaretRightOutlined />} onClick={() => navigate(`/p/${project.id}/replay/${x.hypothesis_id}/${x.experiment_id}`)}>
                          Replay
                        </Button>
                      )}
                      {canWrite && x.is_running && (
                        <Button size="small" danger icon={<StopOutlined />} onClick={() => act(() => api.post(sbx(project.id, `experiments/${x.hypothesis_id}/${x.experiment_id}/stop`)), "Stopping")}>
                          Stop
                        </Button>
                      )}
                      {canWrite && !x.is_running && x.has_init_config && x.has_steps && (
                        <Button
                          size="small"
                          type="primary"
                          icon={<PlayCircleOutlined />}
                          onClick={() => act(() => api.post(sbx(project.id, `experiments/${x.hypothesis_id}/${x.experiment_id}/run`), { resume: false }), "Simulation started")}
                        >
                          {x.status === "completed" || x.status === "failed" || x.status === "stopped" ? "Run again" : "Run"}
                        </Button>
                      )}
                      {canWrite && !x.is_running && ["completed", "failed", "stopped"].includes(x.status) && (
                        <Popconfirm title="Delete this run's results (logs, replay, artifacts)?" okButtonProps={{ danger: true }} onConfirm={() => act(() => api.delete(sbx(project.id, `experiments/${x.hypothesis_id}/${x.experiment_id}/run`)), "Run deleted")}>
                          <Tooltip title="Delete run results">
                            <Button size="small" icon={<ClearOutlined />} />
                          </Tooltip>
                        </Popconfirm>
                      )}
                    </Space>
                  ),
                },
              ]}
            />
          </Card>
        ))}
      </Flex>
      <ExperimentDrawer target={open} onClose={() => setOpen(null)} />
      <NewExperimentModal open={creating} onClose={() => setCreating(false)} hypotheses={hypotheses} />
    </div>
  );
}

export default function Experiments() {
  return (
    <SandboxGate what="experiments">
      <ExperimentsInner />
    </SandboxGate>
  );
}
