import {
  ApiOutlined,
  AppstoreOutlined,
  BookOutlined,
  CrownOutlined,
  DatabaseOutlined,
  KeyOutlined,
  LogoutOutlined,
  PieChartOutlined,
  PlusOutlined,
  SafetyOutlined,
  TeamOutlined,
  UserOutlined,
  WalletOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Avatar, Button, Dropdown, Form, Input, Layout, Menu, Modal, Select, Tag, Typography } from "antd";
import * as React from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { api } from "@/api/client";
import type { Org } from "@/api/types";
import { useAuth } from "@/auth";
import { useMeta } from "@/meta";

const { Sider, Content } = Layout;

export function useCurrentOrg() {
  const { org } = useAuth();
  return useQuery({
    queryKey: ["org", org?.id],
    queryFn: () => api.get<Org>(`/api/v1/orgs/${org!.id}`),
    enabled: !!org,
    refetchInterval: 60_000,
  });
}

function OrgSwitcher() {
  const { orgs, org, setOrg, reload } = useAuth();
  const [open, setOpen] = React.useState(false);
  const [form] = Form.useForm<{ name: string }>();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const create = async () => {
    const { name } = await form.validateFields();
    const created = await api.post<Org>("/api/v1/orgs", { name });
    await reload();
    setOrg(created.id);
    setOpen(false);
    form.resetFields();
    message.success(`Created ${created.name}`);
    await queryClient.invalidateQueries();
    navigate("/");
  };

  return (
    <>
      <Select
        value={org?.id}
        style={{ width: "100%" }}
        onChange={(id) => {
          if (id === "__new") return setOpen(true);
          setOrg(id);
          void queryClient.invalidateQueries();
          navigate("/");
        }}
        options={[
          ...orgs.map((o) => ({ value: o.id, label: o.name })),
          { value: "__new", label: <span><PlusOutlined /> New organization</span> },
        ]}
        aria-label="Organization"
      />
      <Modal title="New organization" open={open} onOk={create} onCancel={() => setOpen(false)} okText="Create" destroyOnHidden>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="Name" rules={[{ required: true, max: 200 }]}>
            <Input placeholder="e.g. Computational Social Science Lab" autoFocus />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

export function AppLayout() {
  const { user, logout, org } = useAuth();
  const { data: meta } = useMeta();
  const { data: orgDetail } = useCurrentOrg();
  const navigate = useNavigate();
  const location = useLocation();

  const items = [
    { key: "/", icon: <AppstoreOutlined />, label: "Projects" },
    { key: "/datasets", icon: <DatabaseOutlined />, label: "Datasets" },
    { key: "/literature", icon: <BookOutlined />, label: "Literature" },
    {
      key: "org",
      icon: <TeamOutlined />,
      label: "Organization",
      children: [
        { key: "/org/members", icon: <TeamOutlined />, label: "Members" },
        { key: "/org/billing", icon: <WalletOutlined />, label: "Billing" },
        { key: "/org/usage", icon: <PieChartOutlined />, label: "Usage" },
        { key: "/org/models", icon: <ApiOutlined />, label: "Models" },
        { key: "/org/api-keys", icon: <KeyOutlined />, label: "API keys" },
      ],
    },
    ...(user?.is_platform_admin ? [{ key: "/admin", icon: <SafetyOutlined />, label: "Admin" }] : []),
  ];
  const selected =
    location.pathname === "/" || location.pathname.startsWith("/p/")
      ? "/"
      : (items.flatMap((i) => ("children" in i && i.children ? i.children : [i])).find((i) => location.pathname.startsWith(i.key) && i.key !== "/")?.key ?? "/");

  return (
    <Layout style={{ height: "100%" }}>
      <Sider width={232} breakpoint="lg" collapsedWidth={0} style={{ display: "flex", flexDirection: "column" }}>
        <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
          <div style={{ padding: "18px 16px 12px", display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }} onClick={() => navigate("/")}>
            <img src="/favicon.svg" width={28} height={28} alt="" />
            <Typography.Text strong style={{ color: "#fff", fontSize: 17 }}>
              {meta?.brand_name ?? "Societas"}
            </Typography.Text>
          </div>
          <div style={{ padding: "0 12px 12px" }}>
            <OrgSwitcher />
            {orgDetail && (
              <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8, color: "#cbd5e1", fontSize: 12 }}>
                <Tag color={orgDetail.plan === "free" ? "default" : "gold"} icon={orgDetail.plan !== "free" ? <CrownOutlined /> : undefined}>
                  {orgDetail.plan_label}
                </Tag>
                <span title="Prepaid credit" style={{ cursor: "pointer" }} onClick={() => navigate("/org/billing")}>
                  ${orgDetail.balance_usd.toFixed(2)} credit
                </span>
              </div>
            )}
          </div>
          <Menu
            theme="dark"
            mode="inline"
            selectedKeys={[selected]}
            defaultOpenKeys={location.pathname.startsWith("/org") ? ["org"] : []}
            items={items}
            onClick={({ key }) => navigate(key)}
            style={{ flex: 1, borderInlineEnd: 0 }}
          />
          <div style={{ padding: 12, borderTop: "1px solid rgba(255,255,255,0.08)" }}>
            <Dropdown
              placement="topLeft"
              menu={{
                items: [
                  { key: "account", icon: <UserOutlined />, label: "Account" },
                  { key: "docs", icon: <ApiOutlined />, label: "API reference" },
                  { type: "divider" },
                  { key: "logout", icon: <LogoutOutlined />, label: "Sign out" },
                ],
                onClick: async ({ key }) => {
                  if (key === "account") navigate("/account");
                  if (key === "docs") window.open("/api/docs", "_blank", "noopener");
                  if (key === "logout") {
                    await logout();
                    navigate("/login");
                  }
                },
              }}
            >
              <Button type="text" block style={{ color: "#e2e8f0", justifyContent: "flex-start", height: 44 }}>
                <Avatar size="small" style={{ background: "#1f4fd8" }}>
                  {(user?.name || user?.email || "?").slice(0, 1).toUpperCase()}
                </Avatar>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{user?.name || user?.email}</span>
              </Button>
            </Dropdown>
          </div>
        </div>
      </Sider>
      <Content style={{ overflow: "auto", height: "100%" }} key={org?.id}>
        <Outlet />
      </Content>
    </Layout>
  );
}
