import { DeleteOutlined, FileTextOutlined, PlusOutlined, SaveOutlined, SearchOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Checkbox, Col, Drawer, Empty, Flex, Form, Input, InputNumber, Modal, Popconfirm, Row, Select, Table, Tabs, Typography, Upload } from "antd";
import * as React from "react";

import { api, ApiError } from "@/api/client";
import type { LiteratureDoc, SearchHit, SearchSources } from "@/api/types";
import { atLeast, useAuth } from "@/auth";
import { PaperHit, SourceTags } from "@/components/PaperHit";
import { dateTime } from "@/lib/format";

const SOURCES = [
  { value: "openalex", label: "OpenAlex" },
  { value: "arxiv", label: "arXiv" },
  { value: "crossref", label: "Crossref" },
  { value: "local", label: "Organization library" },
];

interface SearchForm {
  query: string;
  year_from?: number;
  year_to?: number;
  limit?: number;
  sources: string[];
}

function SearchTab({ canWrite }: { canWrite: boolean }) {
  const { org } = useAuth();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [hits, setHits] = React.useState<SearchHit[] | null>(null);
  const [sources, setSources] = React.useState<SearchSources>({});
  const [busy, setBusy] = React.useState(false);
  const [saved, setSaved] = React.useState<Set<string>>(new Set());

  const search = async (values: SearchForm) => {
    setBusy(true);
    try {
      const r = await api.post<{ results: SearchHit[]; sources: SearchSources }>(`/api/v1/orgs/${org!.id}/literature/search`, {
        ...values,
        limit: values.limit ?? 20,
      });
      setHits(r.results);
      setSources(r.sources);
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Search failed");
    } finally {
      setBusy(false);
    }
  };

  const save = async (hit: SearchHit, key: string) => {
    try {
      await api.post(`/api/v1/orgs/${org!.id}/literature/library`, {
        title: hit.title,
        text: [hit.title, hit.abstract].filter(Boolean).join("\n\n"),
        doi: hit.doi ?? "",
        url: hit.url ?? "",
        year: hit.year ?? null,
        authors: hit.authors ?? [],
      });
      setSaved((s) => new Set(s).add(key));
      await queryClient.invalidateQueries({ queryKey: ["org-library", org!.id] });
      message.success("Saved to the organization library");
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not save");
    }
  };

  return (
    <>
      <Form<SearchForm> name="org-literature-search" layout="vertical" onFinish={search} initialValues={{ sources: SOURCES.map((s) => s.value), limit: 20 }}>
        <Row gutter={12}>
          <Col xs={24} md={12}>
            <Form.Item name="query" label="Query" rules={[{ required: true }]}>
              <Input prefix={<SearchOutlined />} placeholder="agent-based models of opinion dynamics on social networks" />
            </Form.Item>
          </Col>
          <Col xs={8} md={4}>
            <Form.Item name="year_from" label="From year">
              <InputNumber min={1900} max={2100} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
          <Col xs={8} md={4}>
            <Form.Item name="year_to" label="To year">
              <InputNumber min={1900} max={2100} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
          <Col xs={8} md={4}>
            <Form.Item name="limit" label="Results">
              <InputNumber min={1} max={50} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
        </Row>
        <Flex justify="space-between" align="center" wrap gap={8}>
          <Form.Item name="sources" noStyle>
            <Checkbox.Group options={SOURCES} />
          </Form.Item>
          <Button type="primary" htmlType="submit" icon={<SearchOutlined />} loading={busy}>
            Search
          </Button>
        </Flex>
      </Form>
      <SourceTags sources={sources} />
      {hits && (
        <Flex vertical gap={8} style={{ marginTop: 12 }}>
          {hits.length === 0 && <Empty description="No results" />}
          {hits.map((hit, i) => {
            const key = hit.doi || hit.url || `${hit.title}-${i}`;
            return (
              <PaperHit
                key={key}
                hit={hit}
                actions={
                  canWrite &&
                  hit.source !== "local" && (
                    <Button size="small" icon={<SaveOutlined />} disabled={saved.has(key)} onClick={() => save(hit, key)}>
                      {saved.has(key) ? "Saved" : "Save"}
                    </Button>
                  )
                }
              />
            );
          })}
        </Flex>
      )}
    </>
  );
}

interface DocForm {
  title: string;
  authors?: string[];
  year?: number;
  doi?: string;
  url?: string;
  text: string;
}

function AddDocumentModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { org } = useAuth();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<DocForm>();
  const [saving, setSaving] = React.useState(false);

  const submit = async (values: DocForm) => {
    setSaving(true);
    try {
      await api.post(`/api/v1/orgs/${org!.id}/literature/library`, { ...values, authors: values.authors ?? [], year: values.year ?? null });
      await queryClient.invalidateQueries({ queryKey: ["org-library", org!.id] });
      message.success("Document added");
      form.resetFields();
      onClose();
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not add the document");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onCancel={onClose} title="Add a document" okText="Add" onOk={() => form.submit()} confirmLoading={saving} width={720} destroyOnHidden>
      <Typography.Paragraph type="secondary">
        Papers, reports or notes your organization wants every project to find. The AI scientist searches them with the <code>local</code> source.
      </Typography.Paragraph>
      <Form form={form} name="org-literature-doc" layout="vertical" onFinish={submit} requiredMark={false}>
        <Form.Item name="title" label="Title" rules={[{ required: true, max: 1000 }]}>
          <Input />
        </Form.Item>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="authors" label="Authors">
              <Select mode="tags" tokenSeparators={[";"]} placeholder="Add authors" />
            </Form.Item>
          </Col>
          <Col span={4}>
            <Form.Item name="year" label="Year">
              <InputNumber min={1500} max={2100} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="doi" label="DOI">
              <Input placeholder="10.1000/xyz123" />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="url" label="URL" rules={[{ type: "url" }]}>
          <Input placeholder="https://…" />
        </Form.Item>
        <Form.Item
          name="text"
          label={
            <Flex gap={8} align="center">
              Text
              <Upload
                accept=".txt,.md,.markdown,text/plain,text/markdown"
                showUploadList={false}
                beforeUpload={async (file) => {
                  const text = await file.text();
                  form.setFieldsValue({ text, title: form.getFieldValue("title") || file.name.replace(/\.[^.]+$/, "") });
                  return Upload.LIST_IGNORE;
                }}
              >
                <Button size="small" type="link" icon={<FileTextOutlined />}>
                  Load a .txt or .md file
                </Button>
              </Upload>
            </Flex>
          }
          rules={[{ required: true }]}
        >
          <Input.TextArea rows={10} placeholder="Full text or abstract" />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function LibraryTab({ canWrite }: { canWrite: boolean }) {
  const { org } = useAuth();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [filter, setFilter] = React.useState("");
  const [adding, setAdding] = React.useState(false);
  const [viewing, setViewing] = React.useState<number | null>(null);
  const library = useQuery({
    queryKey: ["org-library", org?.id],
    queryFn: () => api.get<{ documents: LiteratureDoc[] }>(`/api/v1/orgs/${org!.id}/literature/library`),
    enabled: !!org,
  });
  const doc = useQuery({
    queryKey: ["org-library", org?.id, viewing],
    queryFn: () => api.get<LiteratureDoc>(`/api/v1/orgs/${org!.id}/literature/library/${viewing}`),
    enabled: viewing !== null,
  });
  const rows = (library.data?.documents ?? []).filter((d) => !filter || `${d.title} ${d.authors.join(" ")} ${d.doi}`.toLowerCase().includes(filter.toLowerCase()));

  const remove = async (id: number) => {
    try {
      await api.delete(`/api/v1/orgs/${org!.id}/literature/library/${id}`);
      await queryClient.invalidateQueries({ queryKey: ["org-library", org!.id] });
      message.success("Removed");
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : "Could not remove");
    }
  };

  return (
    <>
      <Flex justify="space-between" gap={8} wrap style={{ marginBottom: 12 }}>
        <Input.Search allowClear placeholder="Filter the library" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ maxWidth: 360 }} />
        {canWrite && (
          <Button icon={<PlusOutlined />} onClick={() => setAdding(true)}>
            Add document
          </Button>
        )}
      </Flex>
      <Table<LiteratureDoc>
        rowKey="id"
        loading={library.isLoading}
        dataSource={rows}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No documents yet. Save search results or add your own." /> }}
        onRow={(d) => ({ onClick: () => setViewing(d.id), style: { cursor: "pointer" } })}
        columns={[
          {
            title: "Title",
            key: "title",
            render: (_, d) => (
              <div>
                <Typography.Text strong>{d.title}</Typography.Text>
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {[d.authors.slice(0, 3).join(", ") + (d.authors.length > 3 ? " et al." : ""), d.doi && `doi:${d.doi}`].filter(Boolean).join(" · ")}
                  </Typography.Text>
                </div>
              </div>
            ),
          },
          { title: "Year", dataIndex: "year", width: 80, render: (y: number | null) => y ?? "—" },
          { title: "Text", dataIndex: "characters", width: 110, render: (n: number) => `${n.toLocaleString()} chars` },
          { title: "Added", dataIndex: "created_at", width: 160, render: dateTime },
          {
            title: "",
            key: "actions",
            width: 56,
            render: (_, d) =>
              canWrite && (
                <span onClick={(e) => e.stopPropagation()}>
                  <Popconfirm title="Remove from the library?" onConfirm={() => remove(d.id)}>
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </span>
              ),
          },
        ]}
      />
      <AddDocumentModal open={adding} onClose={() => setAdding(false)} />
      <Drawer open={viewing !== null} onClose={() => setViewing(null)} size={760} title={doc.data?.title ?? "Document"} loading={doc.isLoading}>
        {doc.data && (
          <>
            <Typography.Paragraph type="secondary">
              {[doc.data.authors.join(", "), doc.data.year, doc.data.doi && `doi:${doc.data.doi}`].filter(Boolean).join(" · ")}
              {doc.data.url && (
                <>
                  {" · "}
                  <a href={doc.data.url} target="_blank" rel="noreferrer">
                    source
                  </a>
                </>
              )}
            </Typography.Paragraph>
            <pre className="log-view" style={{ whiteSpace: "pre-wrap" }}>
              {doc.data.text}
            </pre>
          </>
        )}
      </Drawer>
    </>
  );
}

export default function LiteratureLibraryPage() {
  const { org } = useAuth();
  const canWrite = atLeast(org?.role, "member");
  const library = useQuery({
    queryKey: ["org-library", org?.id],
    queryFn: () => api.get<{ documents: LiteratureDoc[] }>(`/api/v1/orgs/${org!.id}/literature/library`),
    enabled: !!org,
  });
  return (
    <div className="page">
      <Typography.Title level={3} style={{ margin: 0 }}>
        Literature
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        Search OpenAlex, arXiv and Crossref, and keep an organization library that every project's AI scientist can search. To collect papers into a project
        workspace, use the project's Literature page.
      </Typography.Paragraph>
      <Card styles={{ body: { paddingTop: 4 } }}>
        <Tabs
          items={[
            { key: "search", label: "Search", children: <SearchTab canWrite={canWrite} /> },
            { key: "library", label: `Organization library (${library.data?.documents.length ?? 0})`, children: <LibraryTab canWrite={canWrite} /> },
          ]}
        />
      </Card>
    </div>
  );
}
