import {
  AppstoreOutlined,
  ArrowLeftOutlined,
  BookOutlined,
  CodeOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  FolderOpenOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  RobotOutlined,
  SettingOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { App, Button, Flex, Menu, Result, Space, Spin, Tooltip, Typography } from "antd";
import * as React from "react";
import { Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";

import { api, ApiError } from "@/api/client";
import { SandboxBadge } from "@/components/SandboxBadge";
import { Loading } from "@/components/Loading";
import { useMeta } from "@/meta";

import { ProjectProvider, useProjectQueries, useSandboxAction } from "./context";

const Overview = React.lazy(() => import("./Overview"));
const Chat = React.lazy(() => import("./Chat"));
const Files = React.lazy(() => import("./Files"));
const Experiments = React.lazy(() => import("./Experiments"));
const Replay = React.lazy(() => import("./Replay"));
const Literature = React.lazy(() => import("./Literature"));
const Modules = React.lazy(() => import("./Modules"));
const Tools = React.lazy(() => import("./Tools"));
const Settings = React.lazy(() => import("./Settings"));

const SECTIONS = [
  { key: "", icon: <DashboardOutlined />, label: "Overview" },
  { key: "chat", icon: <RobotOutlined />, label: "AI Scientist" },
  { key: "files", icon: <FolderOpenOutlined />, label: "Files" },
  { key: "experiments", icon: <ExperimentOutlined />, label: "Experiments" },
  { key: "literature", icon: <BookOutlined />, label: "Literature" },
  { key: "modules", icon: <AppstoreOutlined />, label: "Modules" },
  { key: "tools", icon: <ToolOutlined />, label: "Tools" },
  { key: "settings", icon: <SettingOutlined />, label: "Settings" },
];

export default function ProjectLayout() {
  const { projectId = "" } = useParams();
  const { project, status } = useProjectQueries(projectId);
  const action = useSandboxAction(projectId);
  const navigate = useNavigate();
  const location = useLocation();
  const { data: meta } = useMeta();
  const { message } = App.useApp();
  const refresh = React.useCallback(() => {
    void project.refetch();
    void status.refetch();
  }, [project, status]);

  if (project.isLoading) return <Loading />;
  if (project.error || !project.data) {
    return <Result status="404" title="Project not found" subTitle="It may have been deleted, or you no longer have access." extra={<Link to="/">Back to projects</Link>} />;
  }
  const p = project.data;
  const base = `/p/${p.id}`;
  const section = location.pathname.slice(base.length + 1).split("/")[0] ?? "";
  const canWrite = p.role === "member" || p.role === "admin" || p.role === "owner";
  const state = p.sandbox.status;
  const ideEnabled = p.settings.ide_enabled ?? meta?.features.ide ?? false;

  const openIde = async () => {
    try {
      const { url } = await api.post<{ url: string }>(`/api/v1/projects/${p.id}/ide-ticket`);
      window.open(url, "_blank", "noopener");
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not open the IDE");
    }
  };

  return (
    <ProjectProvider project={p} status={status.data} refresh={refresh}>
      <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
        <div style={{ background: "#fff", borderBottom: "1px solid var(--app-border)", padding: "12px 24px 0" }}>
          <Flex justify="space-between" align="center" wrap gap={12}>
            <Space size={12} align="center">
              <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate("/")} aria-label="Back to projects" />
              <div>
                <Typography.Title level={4} style={{ margin: 0 }}>
                  {p.name}
                </Typography.Title>
                <Typography.Text type="secondary" ellipsis style={{ maxWidth: 560, display: "block" }}>
                  {p.topic}
                </Typography.Text>
              </div>
            </Space>
            <Space wrap>
              <SandboxBadge status={state} error={p.sandbox.error} />
              {status.data?.agent?.busy && <Spin size="small" />}
              {canWrite && (state === "stopped" || state === "error") && (
                <Button type="primary" icon={<PlayCircleOutlined />} loading={action.isPending} onClick={() => action.mutate("start")}>
                  Start
                </Button>
              )}
              {canWrite && state === "running" && (
                <>
                  <Tooltip title="Restart the workspace (applies model and size changes)">
                    <Button icon={<ReloadOutlined />} loading={action.isPending} onClick={() => action.mutate("restart")} />
                  </Tooltip>
                  <Button icon={<PauseCircleOutlined />} loading={action.isPending} onClick={() => action.mutate("stop")}>
                    Stop
                  </Button>
                </>
              )}
              {canWrite && ideEnabled && (
                <Button icon={<CodeOutlined />} disabled={state !== "running"} onClick={openIde}>
                  Open IDE
                </Button>
              )}
            </Space>
          </Flex>
          <Menu
            mode="horizontal"
            selectedKeys={[section === "replay" ? "experiments" : section]}
            items={SECTIONS.map((s) => ({ key: s.key, icon: s.icon, label: s.label }))}
            onClick={({ key }) => navigate(key ? `${base}/${key}` : base)}
            style={{ borderBottom: 0, marginTop: 4 }}
          />
        </div>
        <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: section === "chat" || section === "files" || section === "replay" ? 0 : 24 }}>
          <React.Suspense fallback={<Loading />}>
            <Routes>
              <Route index element={<Overview />} />
              <Route path="chat/:sessionId?" element={<Chat />} />
              <Route path="files/*" element={<Files />} />
              <Route path="experiments" element={<Experiments />} />
              <Route path="replay/:h/:e" element={<Replay />} />
              <Route path="literature" element={<Literature />} />
              <Route path="modules" element={<Modules />} />
              <Route path="tools" element={<Tools />} />
              <Route path="settings" element={<Settings />} />
              <Route path="*" element={<Navigate to={base} replace />} />
            </Routes>
          </React.Suspense>
        </div>
      </div>
    </ProjectProvider>
  );
}
