import { PlayCircleOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Result, Spin, Typography } from "antd";
import * as React from "react";

import { api, ApiError } from "@/api/client";
import type { Project, SandboxStatus } from "@/api/types";
import { atLeast } from "@/auth";

export interface ProjectCtx {
  project: Project;
  status: SandboxStatus | undefined;
  running: boolean;
  canWrite: boolean;
  canAdmin: boolean;
  refresh: () => void;
}

const Ctx = React.createContext<ProjectCtx | null>(null);

export function useProjectCtx(): ProjectCtx {
  const value = React.useContext(Ctx);
  if (!value) throw new Error("useProjectCtx outside ProjectProvider");
  return value;
}

export function useProjectQueries(projectId: string) {
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.get<Project>(`/api/v1/projects/${projectId}`),
    refetchInterval: (q) => (q.state.data?.sandbox.status === "starting" || q.state.data?.sandbox.status === "stopping" ? 2000 : 20_000),
  });
  const running = project.data?.sandbox.status === "running";
  const status = useQuery({
    queryKey: ["sandbox", projectId],
    queryFn: () => api.get<SandboxStatus>(`/api/v1/projects/${projectId}/sandbox`),
    enabled: !!project.data,
    refetchInterval: running ? 15_000 : 4000,
  });
  return { project, status };
}

export function ProjectProvider({ project, status, refresh, children }: { project: Project; status?: SandboxStatus; refresh: () => void; children: React.ReactNode }) {
  const value = React.useMemo<ProjectCtx>(
    () => ({
      project,
      status,
      running: project.sandbox.status === "running" && !!status?.agent,
      canWrite: atLeast(project.role, "member"),
      canAdmin: atLeast(project.role, "admin"),
      refresh,
    }),
    [project, status, refresh],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSandboxAction(projectId: string) {
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  return useMutation({
    mutationFn: (action: "start" | "stop" | "restart") => api.post<Project>(`/api/v1/projects/${projectId}/sandbox/${action}`),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      await queryClient.invalidateQueries({ queryKey: ["sandbox", projectId] });
    },
    onError: (e) => message.error(e instanceof ApiError ? e.message : "Sandbox action failed"),
  });
}

/** Renders children only while the project's sandbox is up; offers to start it otherwise. */
export function SandboxGate({ children, what = "this page" }: { children: React.ReactNode; what?: string }) {
  const { project, running, canWrite, status } = useProjectCtx();
  const action = useSandboxAction(project.id);
  if (running) return <>{children}</>;
  const state = project.sandbox.status;
  if (state === "starting" || (state === "running" && !status?.agent)) {
    return (
      <Card>
        <Result
          icon={<Spin size="large" />}
          title="Starting your workspace"
          subTitle="Preparing the AgentSociety environment, research skills and AI social scientist. This usually takes a few seconds."
        />
      </Card>
    );
  }
  return (
    <Card>
      <Result
        status={state === "error" ? "warning" : "info"}
        title={state === "error" ? "The workspace stopped with an error" : "The workspace is not running"}
        subTitle={
          <>
            <Typography.Paragraph type="secondary">Start it to use {what}. Your files are kept while it is stopped.</Typography.Paragraph>
            {project.sandbox.error && <Typography.Text type="danger">{project.sandbox.error}</Typography.Text>}
          </>
        }
        extra={
          canWrite && (
            <Button type="primary" icon={<PlayCircleOutlined />} loading={action.isPending} onClick={() => action.mutate("start")}>
              Start workspace
            </Button>
          )
        }
      />
    </Card>
  );
}
