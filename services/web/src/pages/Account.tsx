import { LogoutOutlined } from "@ant-design/icons";
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Flex, Form, Input, Popconfirm, Table, Tag, Typography } from "antd";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { api, ApiError } from "@/api/client";
import type { OrgSummary } from "@/api/types";
import { useAuth } from "@/auth";
import { dateTime } from "@/lib/format";

function ProfileCard() {
  const { user, reload } = useAuth();
  const { message } = App.useApp();
  const [saving, setSaving] = React.useState(false);
  const save = async ({ name }: { name: string }) => {
    setSaving(true);
    try {
      await api.patch("/api/v1/auth/me", { name });
      await reload();
      message.success("Profile saved");
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not save");
    } finally {
      setSaving(false);
    }
  };
  return (
    <Card title="Profile">
      <Form layout="vertical" initialValues={{ name: user?.name }} onFinish={save} requiredMark={false} style={{ maxWidth: 480 }}>
        <Form.Item label="Email">
          <Input value={user?.email} disabled />
        </Form.Item>
        <Form.Item name="name" label="Name" rules={[{ max: 200 }]}>
          <Input />
        </Form.Item>
        <Typography.Paragraph type="secondary">Member since {dateTime(user?.created_at)}</Typography.Paragraph>
        <Button type="primary" htmlType="submit" loading={saving}>
          Save
        </Button>
      </Form>
    </Card>
  );
}

function PasswordCard() {
  const { message } = App.useApp();
  const [form] = Form.useForm<{ current_password: string; new_password: string; confirm: string }>();
  const [saving, setSaving] = React.useState(false);
  const save = async (values: { current_password: string; new_password: string }) => {
    setSaving(true);
    try {
      await api.patch("/api/v1/auth/me", { current_password: values.current_password, new_password: values.new_password });
      form.resetFields();
      message.success("Password changed");
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not change the password");
    } finally {
      setSaving(false);
    }
  };
  return (
    <Card title="Password">
      <Form form={form} layout="vertical" onFinish={save} requiredMark={false} style={{ maxWidth: 480 }}>
        <Form.Item name="current_password" label="Current password" rules={[{ required: true }]}>
          <Input.Password autoComplete="current-password" />
        </Form.Item>
        <Form.Item name="new_password" label="New password" rules={[{ required: true, min: 8, max: 200 }]}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="confirm"
          label="Repeat the new password"
          dependencies={["new_password"]}
          rules={[
            { required: true },
            ({ getFieldValue }) => ({
              validator: async (_, v) => {
                if (v && v !== getFieldValue("new_password")) throw new Error("The passwords do not match");
              },
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={saving}>
          Change password
        </Button>
      </Form>
    </Card>
  );
}

function OrganizationsCard() {
  const { user, orgs, org, setOrg, reload } = useAuth();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const leave = async (o: OrgSummary) => {
    try {
      await api.delete(`/api/v1/orgs/${o.id}/members/${user!.id}`);
      await reload();
      await queryClient.invalidateQueries();
      message.success(`You left ${o.name}`);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not leave the organization");
    }
  };
  return (
    <Card title="Organizations">
      <Table<OrgSummary>
        rowKey="id"
        dataSource={orgs}
        pagination={false}
        columns={[
          {
            title: "Name",
            key: "name",
            render: (_, o) => (
              <Flex gap={8} align="center">
                <Typography.Text strong>{o.name}</Typography.Text>
                {o.is_personal && <Tag>personal</Tag>}
                {o.id === org?.id && <Tag color="blue">current</Tag>}
              </Flex>
            ),
          },
          { title: "Role", dataIndex: "role", width: 110, render: (r: string) => <Tag>{r}</Tag> },
          { title: "Plan", dataIndex: "plan", width: 100 },
          {
            title: "",
            key: "actions",
            width: 220,
            align: "right",
            render: (_, o) => (
              <Flex gap={4} justify="end">
                {o.id !== org?.id && (
                  <Button
                    size="small"
                    onClick={() => {
                      setOrg(o.id);
                      navigate("/");
                    }}
                  >
                    Switch
                  </Button>
                )}
                {!o.is_personal && (
                  <Popconfirm title={`Leave ${o.name}?`} description="You lose access to its projects, datasets and credit." onConfirm={() => leave(o)}>
                    <Button size="small" danger icon={<LogoutOutlined />}>
                      Leave
                    </Button>
                  </Popconfirm>
                )}
              </Flex>
            ),
          },
        ]}
      />
    </Card>
  );
}

export default function AccountPage() {
  return (
    <div className="page" style={{ maxWidth: 900 }}>
      <Typography.Title level={3} style={{ marginTop: 0 }}>
        Account
      </Typography.Title>
      <Flex vertical gap={16}>
        <ProfileCard />
        <PasswordCard />
        <OrganizationsCard />
      </Flex>
    </div>
  );
}
