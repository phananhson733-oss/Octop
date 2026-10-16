import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  List,
  Popconfirm,
  Select,
  Table,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  KEY_PAGE_ROLES,
  SECTION_KEYS,
  seoApi,
  type ContextOp,
  type ProjectContext,
} from "../../../api/modules/seo";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import styles from "./index.module.less";

const { Text } = Typography;
const PROSE_MAX = 4000;

/**
 * Project memory the bound expert reads before reports (OpenSEO
 * ProjectContextPage). Each save is one atomic batch on the server.
 */
export function ProjectContextPanel({ projectId }: { projectId: string }) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [data, setData] = useState<ProjectContext>();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [competitorForm] = Form.useForm();
  const [pageForm] = Form.useForm();
  useEffect(() => {
    let active = true;
    seoApi
      .context(projectId)
      .then((value) => {
        if (active) setData(value);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [projectId, t]);
  async function apply(updates: ContextOp[]): Promise<boolean> {
    setBusy(true);
    setError("");
    try {
      setData(await seoApi.updateContext(projectId, updates));
      return true;
    } catch (err) {
      setError(apiErrorMessage(err, t("seo.saveError"), t));
      return false;
    } finally {
      setBusy(false);
    }
  }
  if (!data) return error ? <Alert type="error" message={error} /> : null;
  const stamp = (at: string, by: string) =>
    t("seo.contextUpdated", {
      date: formatServerIsoDateTime(at, timeZone),
      by: t(`seo.author_${by}`),
    });
  const prose = (
    draftKey: string,
    label: string,
    stored:
      | { content: string; updated_at: string; updated_by: string }
      | undefined,
    save: (content: string) => ContextOp,
    remove?: ContextOp,
  ) => {
    const value = drafts[draftKey] ?? stored?.content ?? "";
    return (
      <div key={draftKey} className={styles.stack}>
        <Text strong>{label}</Text>
        <Input.TextArea
          aria-label={label}
          value={value}
          maxLength={PROSE_MAX}
          showCount
          autoSize={{ minRows: 2, maxRows: 8 }}
          onChange={(event) =>
            setDrafts((all) => ({ ...all, [draftKey]: event.target.value }))
          }
        />
        <div className={styles.toolbar}>
          <Button
            size="small"
            disabled={busy || value === (stored?.content ?? "")}
            onClick={async () => {
              if (await apply([save(value)]))
                setDrafts(({ [draftKey]: _, ...rest }) => rest);
            }}
          >
            {t("seo.contextSave")}
          </Button>
          {remove && (
            <Popconfirm
              title={t("seo.contextDeleteConfirm")}
              okText={t("seo.contextDelete")}
              cancelText={t("seo.cancel")}
              onConfirm={() => apply([remove])}
            >
              <Button size="small" danger disabled={busy}>
                {t("seo.contextDelete")}
              </Button>
            </Popconfirm>
          )}
          {stored && (
            <Text type="secondary">
              {stamp(stored.updated_at, stored.updated_by)}
            </Text>
          )}
        </div>
      </div>
    );
  };
  return (
    <div className={styles.stack}>
      <Text type="secondary">{t("seo.contextHelp")}</Text>
      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          closable
          onClose={() => setError("")}
        />
      )}
      <Card size="small" title={t("seo.contextSections")}>
        <div className={styles.stack}>
          {SECTION_KEYS.map((key) =>
            prose(
              key,
              t(`seo.section_${key}`),
              data.sections.find((section) => section.key === key),
              (content) => ({ section: key, content }),
            ),
          )}
        </div>
      </Card>
      <Card size="small" title={t("seo.customSections")}>
        {data.custom_sections.length ? (
          <div className={styles.stack}>
            {data.custom_sections.map((section) =>
              prose(
                `custom:${section.slug}`,
                section.title ?? section.slug,
                section,
                (content) => ({
                  custom_section: section.slug,
                  ...(section.title ? { title: section.title } : {}),
                  content,
                }),
                { delete_custom_section: section.slug },
              ),
            )}
          </div>
        ) : (
          <Empty description={t("seo.customSectionsEmpty")} />
        )}
      </Card>
      <Card size="small" title={t("seo.competitors")}>
        <div className={styles.stack}>
          <Table
            rowKey="domain"
            size="small"
            pagination={false}
            dataSource={data.competitors}
            locale={{ emptyText: t("seo.competitorsEmpty") }}
            columns={[
              { title: t("seo.domain"), dataIndex: "domain" },
              { title: t("seo.competitorName"), dataIndex: "name" },
              { title: t("seo.notes"), dataIndex: "notes" },
              {
                title: "",
                key: "remove",
                width: 90,
                render: (_, row) => (
                  <Button
                    size="small"
                    disabled={busy}
                    onClick={() =>
                      void apply([{ remove_competitors: [row.domain] }])
                    }
                  >
                    {t("seo.contextRemove")}
                  </Button>
                ),
              },
            ]}
          />
          <Form
            form={competitorForm}
            layout="inline"
            onFinish={async (values) => {
              const ok = await apply([
                {
                  add_competitors: [
                    {
                      domain: values.domain,
                      ...(values.name ? { name: values.name } : {}),
                      ...(values.notes ? { notes: values.notes } : {}),
                    },
                  ],
                },
              ]);
              if (ok) competitorForm.resetFields();
            }}
          >
            <Form.Item name="domain" rules={[{ required: true }]}>
              <Input
                aria-label={t("seo.competitorDomain")}
                placeholder="competitor.com"
                maxLength={255}
              />
            </Form.Item>
            <Form.Item name="name">
              <Input
                aria-label={t("seo.competitorName")}
                placeholder={t("seo.competitorName")}
                maxLength={120}
              />
            </Form.Item>
            <Form.Item name="notes">
              <Input
                aria-label={t("seo.notes")}
                placeholder={t("seo.notes")}
                maxLength={500}
              />
            </Form.Item>
            <Button htmlType="submit" disabled={busy}>
              {t("seo.addCompetitor")}
            </Button>
          </Form>
        </div>
      </Card>
      <Card size="small" title={t("seo.keyPages")}>
        <div className={styles.stack}>
          <Table
            rowKey="url"
            size="small"
            pagination={false}
            dataSource={data.key_pages}
            locale={{ emptyText: t("seo.keyPagesEmpty") }}
            scroll={{ x: 640 }}
            columns={[
              { title: t("seo.pageUrl"), dataIndex: "url" },
              {
                title: t("seo.pageRole"),
                dataIndex: "role",
                render: (role: string) => t(`seo.role_${role}`),
              },
              { title: t("seo.pageTopic"), dataIndex: "topic" },
              { title: t("seo.notes"), dataIndex: "notes" },
              {
                title: "",
                key: "remove",
                width: 90,
                render: (_, row) => (
                  <Button
                    size="small"
                    disabled={busy}
                    onClick={() =>
                      void apply([{ remove_key_pages: [row.url] }])
                    }
                  >
                    {t("seo.contextRemove")}
                  </Button>
                ),
              },
            ]}
          />
          <Form
            form={pageForm}
            layout="inline"
            initialValues={{ role: "other" }}
            onFinish={async (values) => {
              const ok = await apply([
                {
                  add_key_pages: [
                    {
                      url: values.url,
                      role: values.role,
                      ...(values.topic ? { topic: values.topic } : {}),
                      ...(values.notes ? { notes: values.notes } : {}),
                    },
                  ],
                },
              ]);
              if (ok) pageForm.resetFields();
            }}
          >
            <Form.Item name="url" rules={[{ required: true }]}>
              <Input
                aria-label={t("seo.pageUrl")}
                placeholder="example.com/pricing"
                maxLength={2048}
              />
            </Form.Item>
            <Form.Item name="role">
              <Select
                aria-label={t("seo.pageRole")}
                style={{ minWidth: 120 }}
                options={KEY_PAGE_ROLES.map((role) => ({
                  value: role,
                  label: t(`seo.role_${role}`),
                }))}
              />
            </Form.Item>
            <Form.Item name="topic">
              <Input
                aria-label={t("seo.pageTopic")}
                placeholder={t("seo.pageTopic")}
                maxLength={200}
              />
            </Form.Item>
            <Form.Item name="notes">
              <Input
                aria-label={t("seo.pageNotes")}
                placeholder={t("seo.notes")}
                maxLength={500}
              />
            </Form.Item>
            <Button htmlType="submit" disabled={busy}>
              {t("seo.addKeyPage")}
            </Button>
          </Form>
        </div>
      </Card>
      <Card
        size="small"
        title={t("seo.researchLog", { count: data.research_log.length })}
      >
        <List
          dataSource={data.research_log}
          locale={{ emptyText: t("seo.researchLogEmpty") }}
          renderItem={(entry) => (
            <List.Item
              actions={[
                <Button
                  key="remove"
                  size="small"
                  disabled={busy}
                  onClick={() =>
                    void apply([{ remove_research_log: [entry.log_id] }])
                  }
                >
                  {t("seo.contextRemove")}
                </Button>,
              ]}
            >
              <List.Item.Meta
                title={entry.summary}
                description={stamp(entry.created_at, entry.created_by)}
              />
            </List.Item>
          )}
        />
        {data.research_log.length >= data.research_log_limit && (
          <Text type="secondary">
            {t("seo.researchLogOlder", { count: data.research_log_limit })}
          </Text>
        )}
      </Card>
    </div>
  );
}
