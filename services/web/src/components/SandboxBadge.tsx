import { Badge, Tooltip } from "antd";

import type { SandboxState } from "@/api/types";

const LOOK: Record<string, { status: "success" | "processing" | "default" | "error" | "warning"; label: string }> = {
  running: { status: "success", label: "Running" },
  starting: { status: "processing", label: "Starting" },
  stopping: { status: "warning", label: "Stopping" },
  stopped: { status: "default", label: "Stopped" },
  error: { status: "error", label: "Error" },
  deleted: { status: "default", label: "Deleted" },
};

export function SandboxBadge({ status, error }: { status: SandboxState; error?: string | null }) {
  const look = LOOK[status] ?? { status: "default", label: status };
  const badge = <Badge status={look.status} text={look.label} />;
  return error ? <Tooltip title={error}>{badge}</Tooltip> : badge;
}
