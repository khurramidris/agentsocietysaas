import { LockOutlined, MailOutlined, UserOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Form, Input, Result, Spin, Typography } from "antd";
import * as React from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api, ApiError } from "@/api/client";
import { useAuth } from "@/auth";
import { useMeta } from "@/meta";

function AuthCard({ title, subtitle, children }: { title: string; subtitle?: React.ReactNode; children: React.ReactNode }) {
  const { data: meta } = useMeta();
  return (
    <div className="auth-page">
      <Card style={{ width: 420, maxWidth: "100%", boxShadow: "0 12px 40px rgba(15,23,42,0.12)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 20 }}>
          <img src="/favicon.svg" width={32} height={32} alt="" />
          <Typography.Title level={4} style={{ margin: 0 }}>
            {meta?.brand_name ?? "Societas"}
          </Typography.Title>
        </div>
        <Typography.Title level={3} style={{ marginTop: 0 }}>
          {title}
        </Typography.Title>
        {subtitle && <Typography.Paragraph type="secondary">{subtitle}</Typography.Paragraph>}
        {children}
        {meta && (
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 24, marginBottom: 0 }}>
            Built on{" "}
            <a href={meta.upstream.url} target="_blank" rel="noreferrer">
              {meta.upstream.project}
            </a>{" "}
            ({meta.upstream.license}).
          </Typography.Paragraph>
        )}
      </Card>
    </div>
  );
}

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong; please try again.";
}

export function LoginPage() {
  const { login, status } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const { data: meta } = useMeta();
  const next = params.get("next") || "/";
  if (status === "authenticated") return <Navigate to={next} replace />;

  return (
    <AuthCard title="Sign in" subtitle="Run LLM agent societies, from literature review to paper, without installing anything.">
      {error && <Alert type="error" title={error} showIcon style={{ marginBottom: 16 }} />}
      <Form
        layout="vertical"
        requiredMark={false}
        onFinish={async ({ email, password }) => {
          setBusy(true);
          setError(null);
          try {
            await login(email, password);
            navigate(next, { replace: true });
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <Form.Item name="email" label="Email" rules={[{ required: true, type: "email" }]}>
          <Input prefix={<MailOutlined />} autoComplete="email" autoFocus />
        </Form.Item>
        <Form.Item name="password" label="Password" rules={[{ required: true }]}>
          <Input.Password prefix={<LockOutlined />} autoComplete="current-password" />
        </Form.Item>
        <Button type="primary" htmlType="submit" block loading={busy}>
          Sign in
        </Button>
      </Form>
      {meta?.signup_enabled !== false && (
        <Typography.Paragraph style={{ marginTop: 16, marginBottom: 0 }}>
          New here? <Link to={`/signup${params.toString() ? `?${params}` : ""}`}>Create an account</Link>
        </Typography.Paragraph>
      )}
    </AuthCard>
  );
}

export function SignupPage() {
  const { signup, status } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const { data: meta } = useMeta();
  const next = params.get("next") || "/";
  if (status === "authenticated") return <Navigate to={next} replace />;
  if (meta && !meta.signup_enabled) {
    return (
      <AuthCard title="Invitation only">
        <Typography.Paragraph>Sign-up is disabled on this deployment. Ask an organization admin for an invitation.</Typography.Paragraph>
        <Link to="/login">Back to sign in</Link>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Create your account" subtitle="Includes free credit to run your first simulations.">
      {error && <Alert type="error" title={error} showIcon style={{ marginBottom: 16 }} />}
      <Form
        layout="vertical"
        requiredMark={false}
        onFinish={async (values) => {
          setBusy(true);
          setError(null);
          try {
            await signup(values);
            navigate(next, { replace: true });
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <Form.Item name="name" label="Name">
          <Input prefix={<UserOutlined />} autoComplete="name" autoFocus />
        </Form.Item>
        <Form.Item name="email" label="Email" rules={[{ required: true, type: "email" }]}>
          <Input prefix={<MailOutlined />} autoComplete="email" />
        </Form.Item>
        <Form.Item name="password" label="Password" rules={[{ required: true, min: 8, message: "At least 8 characters" }]}>
          <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
        </Form.Item>
        <Button type="primary" htmlType="submit" block loading={busy}>
          Create account
        </Button>
      </Form>
      <Typography.Paragraph style={{ marginTop: 16, marginBottom: 0 }}>
        Already have an account? <Link to={`/login${params.toString() ? `?${params}` : ""}`}>Sign in</Link>
      </Typography.Paragraph>
    </AuthCard>
  );
}

export function AcceptInvitePage() {
  const { token } = useParams();
  const { status, reload, setOrg } = useAuth();
  const navigate = useNavigate();
  const [state, setState] = React.useState<{ error?: string; done?: boolean }>({});

  React.useEffect(() => {
    if (status !== "authenticated" || !token || state.done || state.error) return;
    api
      .post<{ org_id: string }>("/api/v1/auth/invitations/accept", { token })
      .then(async (r) => {
        await reload();
        setOrg(r.org_id);
        setState({ done: true });
        navigate("/", { replace: true });
      })
      .catch((e) => setState({ error: errorText(e) }));
  }, [status, token, state, reload, setOrg, navigate]);

  if (status === "loading") return <Spin />;
  if (status === "anonymous") {
    const next = encodeURIComponent(`/invite/${token}`);
    return (
      <AuthCard title="You're invited" subtitle="Sign in or create an account with the invited email address to join the organization.">
        <div style={{ display: "flex", gap: 8 }}>
          <Button type="primary" onClick={() => navigate(`/signup?next=${next}`)}>
            Create account
          </Button>
          <Button onClick={() => navigate(`/login?next=${next}`)}>Sign in</Button>
        </div>
      </AuthCard>
    );
  }
  if (state.error) return <Result status="warning" title="Could not accept the invitation" subTitle={state.error} extra={<Link to="/">Go to projects</Link>} />;
  return <Spin />;
}
