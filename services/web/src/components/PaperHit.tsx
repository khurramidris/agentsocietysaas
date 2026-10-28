import { LinkOutlined } from "@ant-design/icons";
import { Button, Card, Flex, Space, Tag, Tooltip, Typography } from "antd";
import * as React from "react";

import type { SearchHit, SearchSources } from "@/api/types";

/** One literature search result (OpenAlex / arXiv / Crossref / the organization library). */
export function PaperHit({ hit, actions }: { hit: SearchHit; actions?: React.ReactNode }) {
  return (
    <Card size="small">
      <Flex justify="space-between" gap={12}>
        <div style={{ minWidth: 0 }}>
          <Typography.Text strong>{hit.title}</Typography.Text>
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {[hit.authors?.slice(0, 4).join(", "), hit.journal, hit.year].filter(Boolean).join(" · ")}
            </Typography.Text>
          </div>
          {hit.abstract && (
            <Typography.Paragraph type="secondary" ellipsis={{ rows: 2, expandable: true }} style={{ marginBottom: 0, fontSize: 13 }}>
              {hit.abstract}
            </Typography.Paragraph>
          )}
          <Space size={4} wrap>
            <Tag>{hit.source_name ?? hit.source}</Tag>
            {hit.doi && <Tag>doi:{hit.doi}</Tag>}
            {hit.pdf_url && <Tag color="green">open access PDF</Tag>}
          </Space>
        </div>
        <Space orientation="vertical" align="end">
          {actions}
          {hit.url && (
            <Button size="small" type="link" icon={<LinkOutlined />} href={hit.url} target="_blank" rel="noreferrer">
              Open
            </Button>
          )}
        </Space>
      </Flex>
    </Card>
  );
}

/** Which sources answered a search, with result counts or errors. */
export function SourceTags({ sources }: { sources: SearchSources }) {
  if (!Object.keys(sources).length) return null;
  return (
    <Space wrap style={{ marginTop: 12 }}>
      {Object.entries(sources).map(([name, s]) => (
        <Tooltip key={name} title={s.error}>
          <Tag color={s.ok ? "green" : "red"}>
            {name}: {s.ok ? s.count : "unavailable"}
          </Tag>
        </Tooltip>
      ))}
    </Space>
  );
}
