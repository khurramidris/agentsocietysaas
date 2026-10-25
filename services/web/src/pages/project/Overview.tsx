import { BulbOutlined, EditOutlined, FileSearchOutlined, LineChartOutlined, RobotOutlined, RocketOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Col, Descriptions, Flex, Input, Row, Statistic, Steps, Tag, Typography } from "antd";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { api, ApiError, sbx } from "@/api/client";
import type { ChatSession, Progress, WorkspaceInfo } from "@/api/types";
import { dateTime, usd } from "@/lib/format";

import { SandboxGate, useProjectCtx } from "./context";

const STAGES: { key: string; title: string; hint: string }[] = [
  { key: "literature_search", title: "Literature", hint: "Search and read related work" },
  { key: "hypothesis", title: "Hypotheses", hint: "Formulate testable hypotheses" },
  { key: "experiment_config", title: "Design", hint: "Agents, environment and steps" },
  { key: "run_experiment", title: "Simulate", hint: "Run the agent society" },
  { key: "analysis", title: "Analysis", hint: "Statistics, figures, report" },
  { key: "generate_paper", title: "Paper", hint: "Write and review the paper" },
];

const QUICK_PROMPTS = [
  { icon: <RocketOutlined />, label: "Run the full research pipeline", prompt: "Run the full research pipeline for the topic in TOPIC.md: literature review, hypotheses, experiment design, simulation, analysis and a paper draft. Report progress after each stage." },
  { icon: <FileSearchOutlined />, label: "Review the literature", prompt: "Search the literature for the research question in TOPIC.md, save the most relevant papers to the workspace and summarise the state of the art." },
  { icon: <BulbOutlined />, label: "Propose hypotheses", prompt: "Based on TOPIC.md and the saved literature, propose two testable hypotheses with experiment groups, and add them to the workspace." },
  { icon: <LineChartOutlined />, label: "Analyse the latest run", prompt: "Analyse the most recent completed experiment, produce figures and an HTML report, and summarise what the results mean for the hypothesis." },
];

function stageStatus(status: string | undefined): "wait" | "process" | "finish" | "error" {
  switch (status) {
    case "completed":
      return "finish";
    case "in_progress":
    case "running":
      return "process";
    case "failed":
    case "blocked":
      return "error";
    default:
      return "wait";
  }
}

export function Pipeline({ progress }: { progress: Progress }) {
  const current = STAGES.findIndex((s) => s.key === progress.workspace.current_stage);
  return (
    <Steps
      current={current < 0 ? 0 : current}
      size="small"
      items={STAGES.map((s) => {
        const state = progress.stages[s.key];
        return {
          title: s.title,
          status: stageStatus(state?.status),
          content: state?.status && state.status !== "not_started" ? state.status.replace(/_/g, " ") : s.hint,
        };
      })}
    />
  );
}

function TopicCard() {
  const { project, canWrite } = useProjectCtx();
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const info = useQuery({ queryKey: ["ws-info", project.id], queryFn: () => api.get<WorkspaceInfo>(sbx(project.id, "workspace/info")) });
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const topic = info.data?.topic ?? project.topic;

  const save = async () => {
    try {
      await api.put(sbx(project.id, "workspace/topic"), { content: draft });
      await api.patch(`/api/v1/projects/${project.id}`, { topic: draft.replace(/^#.*\n/, "").trim().slice(0, 20000) || project.topic });
      await queryClient.invalidateQueries({ queryKey: ["ws-info", project.id] });
      await queryClient.invalidateQueries({ queryKey: ["project", project.id] });
      setEditing(false);
      message.success("TOPIC.md saved");
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not save");
    }
  };

  return (
    <Card
      title="Research question"
      extra={
        canWrite &&
        !editing && (
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => {
              setDraft(topic ?? "");
              setEditing(true);
            }}
          >
            Edit TOPIC.md
          </Button>
        )
      }
    >
      {editing ? (
        <>
          <Input.TextArea rows={8} value={draft} onChange={(e) => setDraft(e.target.value)} className="mono" />
          <Flex gap={8} justify="end" style={{ marginTop: 12 }}>
            <Button onClick={() => setEditing(false)}>Cancel</Button>
            <Button type="primary" onClick={save}>
              Save
            </Button>
          </Flex>
        </>
      ) : (
        <Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{topic || "No topic yet."}</Typography.Paragraph>
      )}
      {info.data && (
        <Row gutter={16} style={{ marginTop: 16 }}>
          <Col span={8}>
            <Statistic title="Papers" value={info.data.counts.papers} />
          </Col>
          <Col span={8}>
            <Statistic title="Hypotheses" value={info.data.counts.hypotheses} />
          </Col>
          <Col span={8}>
            <Statistic title="Experiments" value={info.data.counts.experiments} />
          </Col>
        </Row>
      )}
    </Card>
  );
}

export default function Overview() {
  const { project, status, canWrite } = useProjectCtx();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const pipeline = useQuery({
    queryKey: ["pipeline", project.id],
    queryFn: () => api.get<Progress>(sbx(project.id, "workspace/pipeline")),
    enabled: project.sandbox.status === "running",
    refetchInterval: 15_000,
  });
  const usage = useQuery({ queryKey: ["project-usage", project.id], queryFn: () => api.get<{ total_cost_usd: number }>(`/api/v1/projects/${project.id}/usage`) });

  const ask = async (prompt: string, title: string) => {
    try {
      const session = await api.post<ChatSession>(sbx(project.id, "chat/sessions"), { title });
      await api.post(sbx(project.id, `chat/sessions/${session.id}/messages`), { content: prompt });
      navigate(`/p/${project.id}/chat/${session.id}`);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not reach the AI scientist");
    }
  };

  return (
    <div style={{ maxWidth: 1280, margin: "0 auto", display: "flex", flexDirection: "column", gap: 16 }}>
      <SandboxGate what="the research workspace">
        <Card title="Research pipeline" extra={pipeline.data && <Tag>stage: {pipeline.data.workspace.current_stage.replace(/_/g, " ")}</Tag>}>
          {pipeline.data ? <Pipeline progress={pipeline.data} /> : <Typography.Text type="secondary">Loading pipeline…</Typography.Text>}
        </Card>
        <Row gutter={16}>
          <Col xs={24} lg={14}>
            <TopicCard />
          </Col>
          <Col xs={24} lg={10}>
            <Card title={<span><RobotOutlined /> Ask the AI social scientist</span>}>
              <Flex vertical gap={8}>
                {QUICK_PROMPTS.map((q) => (
                  <Button key={q.label} icon={q.icon} block style={{ justifyContent: "flex-start" }} disabled={!canWrite} onClick={() => ask(q.prompt, q.label)}>
                    {q.label}
                  </Button>
                ))}
                <Button type="link" onClick={() => navigate(`/p/${project.id}/chat`)}>
                  Open the chat →
                </Button>
              </Flex>
            </Card>
          </Col>
        </Row>
      </SandboxGate>
      <Card title="Workspace">
        <Descriptions
          column={{ xs: 1, md: 2, xl: 3 }}
          size="small"
          items={[
            { key: "size", label: "Size", children: project.sandbox.size },
            { key: "started", label: "Started", children: dateTime(project.sandbox.started_at) },
            { key: "spend", label: "Spend to date", children: usage.data ? usd(usage.data.total_cost_usd) : "—" },
            { key: "sim", label: "Simulation model", children: project.settings.sim_model ?? "platform default" },
            { key: "chat", label: "AI scientist model", children: project.settings.chat_model ?? "platform default" },
            ...Object.entries(status?.agent?.versions ?? {}).map(([k, v]) => ({ key: `v-${k}`, label: k, children: v ?? "—" })),
          ]}
        />
      </Card>
    </div>
  );
}
