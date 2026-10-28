/**
 * The upstream extension's React webviews, mounted with the browser host from
 * ./host.ts. Each component renders exactly what the AI Social Scientist VS Code
 * extension shows, against this project's sandbox.
 */

import "./lang";
import "@upstream/i18n";
import "virtual:upstream-replay.css";

import { InitConfigApp } from "@upstream/initConfig/InitConfigApp";
import { PrefillParamsApp } from "@upstream/prefillParams/PrefillParamsApp";
import { ReplayApp } from "@upstream/replay/ReplayApp";
import { SimSettingsApp } from "@upstream/simSettings/SimSettingsApp";
import { App } from "antd";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { createHost, initConfigHandler, prefillParamsHandler, replayHandler, simSettingsHandler } from "./host";

function useNotify() {
  const { message } = App.useApp();
  return React.useCallback((text: string, kind: "success" | "warning" | "error" = "error") => message[kind](text), [message]);
}

export function ReplayView({ projectId, hypothesisId, experimentId }: { projectId: string; hypothesisId: string; experimentId: string }) {
  const notify = useNotify();
  const host = React.useMemo(
    () => createHost(replayHandler(projectId, hypothesisId, experimentId, notify), (e) => notify(String(e))),
    [projectId, hypothesisId, experimentId, notify],
  );
  // Rendered inside an element with class "upstream-replay" (see pages/project/Replay.tsx).
  return <ReplayApp vscode={host} />;
}

export function InitConfigView({ projectId, path, initialConfig, onSaved }: { projectId: string; path: string; initialConfig: unknown; onSaved: (text: string) => void }) {
  const notify = useNotify();
  const host = React.useMemo(() => createHost(initConfigHandler(projectId, path, onSaved), (e) => notify(String(e))), [projectId, path, onSaved, notify]);
  return (
    <div className="upstream-host">
      <InitConfigApp vscode={host} initialConfig={initialConfig as never} />
    </div>
  );
}

export function SimSettingsView({ projectId, path, initialSettings, onSaved }: { projectId: string; path: string; initialSettings: unknown; onSaved: (text: string) => void }) {
  const notify = useNotify();
  const host = React.useMemo(() => createHost(simSettingsHandler(projectId, path, onSaved), (e) => notify(String(e))), [projectId, path, onSaved, notify]);
  return (
    <div className="upstream-host">
      <SimSettingsApp vscode={host} initialSettings={(initialSettings ?? {}) as never} />
    </div>
  );
}

export function ModuleCatalogView({ projectId }: { projectId: string }) {
  const notify = useNotify();
  const navigate = useNavigate();
  const host = React.useMemo(
    () =>
      createHost(
        prefillParamsHandler(projectId, {
          openFile: (path) => navigate(`/p/${projectId}/files?path=${encodeURIComponent(path)}`),
          openSettings: () => navigate(`/p/${projectId}/settings`),
          notify,
        }),
        (e) => notify(String(e)),
      ),
    [projectId, navigate, notify],
  );
  return (
    <div className="upstream-host" style={{ minHeight: 640 }}>
      <PrefillParamsApp vscode={host} />
    </div>
  );
}
