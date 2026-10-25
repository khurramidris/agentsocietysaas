// Shapes returned by the control plane (controlplane/api/*) and sandboxd (services/sandbox).

export type Role = "viewer" | "member" | "admin" | "owner";

export interface User {
  id: string;
  email: string;
  name: string;
  is_platform_admin: boolean;
  created_at: string;
}

export interface OrgSummary {
  id: string;
  name: string;
  slug: string;
  plan: string;
  role: Role;
  is_personal: boolean;
}

export interface Org extends OrgSummary {
  plan_label: string;
  balance_usd: number;
  project_count?: number;
  limits: {
    max_projects: number;
    max_running_sandboxes: number;
    sandbox_sizes: string[];
    max_llm_concurrency: number;
    idle_stop_minutes: number;
  };
  created_at: string;
}

export type SandboxState = "stopped" | "starting" | "running" | "stopping" | "error" | "deleted";

export interface ProjectSettings {
  sim_model?: string;
  coder_model?: string;
  embedding_model?: string;
  embedding_dims?: number;
  chat_model?: string;
  chat_small_model?: string;
  ide_enabled?: boolean;
}

export interface Project {
  id: string;
  org_id: string;
  name: string;
  slug: string;
  description: string;
  topic: string;
  settings: ProjectSettings;
  created_at: string;
  updated_at: string;
  role?: Role;
  restart_required?: boolean;
  sandbox: {
    status: SandboxState;
    size: string;
    started_at: string | null;
    last_activity_at: string | null;
    error: string | null;
  };
}

export interface AgentStatus {
  project_id: string;
  busy: boolean;
  running_experiments: string[];
  active_chat_turns: number;
  active_jobs: number;
  last_activity: number;
  backend: { running: boolean; pid: number | null; restarts: number; uptime_s: number | null } | null;
  ide: { running: boolean } | null;
  bootstrap: Record<string, unknown> | null;
  versions: Record<string, string | null>;
}

export interface SandboxStatus {
  status: SandboxState;
  size: string;
  started_at: string | null;
  error: string | null;
  driver: string;
  agent?: AgentStatus;
  agent_error?: string;
}

export interface Member {
  user_id: string;
  email: string;
  name: string;
  role: Role;
  joined_at: string;
}

export interface Invitation {
  id: string;
  email: string;
  role: Role;
  expires_at: string;
  accept_url?: string;
  token?: string;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  role: Role;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  secret?: string;
}

export interface Plan {
  id: string;
  label: string;
  price_usd_month: number | null;
  monthly_credit_usd: number;
  max_projects: number;
  max_running_sandboxes: number;
  sandbox_sizes: string[];
  max_llm_concurrency: number;
}

export interface LedgerEntry {
  id: number;
  delta_usd: number;
  kind: string;
  description: string;
  balance_after_usd: number;
  created_at: string;
}

export interface BillingOverview {
  org_id: string;
  plan: string;
  balance_usd: number;
  spent_30d_usd: number;
  stripe_enabled: boolean;
  plans: Plan[];
  ledger: LedgerEntry[];
}

export interface UsageReport {
  days: number;
  by_day: { date: string; cost_usd: number; requests: number }[];
  by_model: { kind: string; model: string; requests: number; input_tokens: number; output_tokens: number; cached_tokens: number; cost_usd: number }[];
  by_project: { project_id: string | null; project_name: string; cost_usd: number; requests: number }[];
}

export interface ModelRoute {
  id: string;
  org_id: string | null;
  alias: string;
  provider_id: string;
  upstream_model: string;
  kind: "chat" | "embedding";
  description: string;
  enabled: boolean;
  pricing_usd_per_mtok: { input: number; output: number; cache_read: number; cache_write: number; cache_write_1h: number };
  long_context_tokens: number;
  long_context_multiplier: number;
}

export interface Provider {
  id: string;
  org_id: string | null;
  name: string;
  kind: string;
  api_base: string;
  has_api_key: boolean;
  enabled: boolean;
  created_at: string;
}

export interface Meta {
  brand_name: string;
  environment: string;
  signup_enabled: boolean;
  features: { stripe: boolean; ide: boolean; mock_models: boolean; sandbox_driver: string };
  ide_base_url: string;
  mapbox_token?: string;
  defaults: { sim_model: string; chat_model: string; embedding_model: string };
  sandbox_sizes: Record<string, { cpus: number; memory_gb: number }>;
  sandbox_hourly_usd: Record<string, number>;
  plans: { id: string; label: string; price_usd_month: number | null }[];
  upstream: { project: string; license: string; url: string };
}

export interface Dataset {
  id: string;
  name: string;
  description: string;
  category: string;
  version: string;
  tags: string[];
  author: string;
  license: string;
  visibility: "org" | "public";
  status: "draft" | "uploaded" | "pending_review" | "published" | "rejected";
  package_size_bytes: number | null;
  created_at: string | null;
  updated_at: string | null;
  published_at: string | null;
}

// ------------------------------------------------------------------ sandboxd

export interface FileEntry {
  name: string;
  path: string;
  type: "file" | "directory";
  size: number | null;
  mtime: number;
  symlink: boolean;
  protected: boolean;
  children?: FileEntry[];
  truncated?: boolean;
}

export interface FileContent {
  path: string;
  content: string;
  size: number;
  mtime: number;
  binary?: boolean;
  truncated?: boolean;
}

export interface ExperimentStatus {
  hypothesis_id: string;
  experiment_id: string;
  status: string;
  is_running: boolean;
  pid: number | null;
  start_time: string | null;
  end_time: string | null;
  simulation_time: string | null;
  step_count: number | null;
  error: string | null;
  resumed: boolean;
  has_init_config: boolean;
  has_steps: boolean;
  has_replay: boolean;
}

export interface Hypothesis {
  hypothesis_id: string;
  title: string | null;
  has_hypothesis_md: boolean;
  experiments: ExperimentStatus[];
}

export interface ChatSession {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  claude_session_id: string | null;
  model: string | null;
  turns: number;
  status: "idle" | "running";
  last_error: string | null;
  last_result: Record<string, unknown> | null;
  last_seq?: number;
}

export interface ChatEvent {
  seq: number;
  ts: number;
  kind: "user" | "claude" | "log" | "error" | "turn_end";
  data: any;
}

export interface ToolJob {
  id: string;
  tool: string;
  args: string[];
  status: "running" | "succeeded" | "failed" | "cancelled" | "timeout";
  exit_code: number | null;
  started_at: number;
  finished_at: number | null;
  output?: string;
  json?: unknown;
}

export interface StageState {
  status: string;
  started_at: string | null;
  completed_at: string | null;
  attempts: number;
  error: string | null;
  verification_status?: string;
}

export interface Progress {
  version: string;
  workspace: { topic: string; current_stage: string; current_hypothesis_id: string | null; current_experiment_id: string | null; revision_round: number };
  stages: Record<string, StageState>;
  hypotheses: Record<string, unknown>;
  transitions: unknown[];
}

export interface WorkspaceInfo {
  workspace: string;
  initialized: boolean;
  topic: string | null;
  progress: Progress | null;
  counts: { papers: number; hypotheses: number; experiments: number };
  versions: Record<string, string | null>;
}
