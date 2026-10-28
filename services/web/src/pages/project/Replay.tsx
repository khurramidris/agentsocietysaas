import { ArrowLeftOutlined } from "@ant-design/icons";
import { Button, Typography } from "antd";
import * as React from "react";
import { useNavigate, useParams } from "react-router-dom";

import { ReplayView } from "@/upstream/embeds";

import { SandboxGate, useProjectCtx } from "./context";

const BAR = 44;

/**
 * The upstream replay viewer is laid out for a full VS Code panel (its CSS sizes
 * panels against the viewport), so it gets the whole window; our bar takes the
 * space the upstream layout reserves through --replay-header.
 */
export default function Replay() {
  const { project, running } = useProjectCtx();
  const { h = "", e = "" } = useParams();
  const navigate = useNavigate();
  const back = () => navigate(`/p/${project.id}/experiments`);
  React.useEffect(() => {
    const onKey = (ev: KeyboardEvent) => ev.key === "Escape" && back();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  const bar = (
    <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: BAR, zIndex: 5, display: "flex", alignItems: "center", gap: 12, padding: "0 12px", background: "#0f172a", color: "#fff" }}>
      <Button type="text" size="small" icon={<ArrowLeftOutlined />} onClick={back} style={{ color: "#fff" }}>
        Back
      </Button>
      <Typography.Text style={{ color: "#fff" }} strong>
        {project.name}
      </Typography.Text>
      <Typography.Text style={{ color: "#cbd5e1" }}>
        Replay · hypothesis {h} · experiment {e}
      </Typography.Text>
    </div>
  );
  if (!running) {
    return (
      <div style={{ padding: 24 }}>
        <SandboxGate what="the replay viewer">{null}</SandboxGate>
      </div>
    );
  }
  return (
    <div
      className="upstream-replay"
      style={{ position: "fixed", inset: 0, zIndex: 900, background: "var(--vscode-editor-background)", ["--replay-header" as string]: `${BAR}px` } as React.CSSProperties}
    >
      {bar}
      <ReplayView key={`${h}/${e}`} projectId={project.id} hypothesisId={h} experimentId={e} />
    </div>
  );
}
