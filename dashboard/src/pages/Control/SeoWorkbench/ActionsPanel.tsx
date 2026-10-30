import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  seoApi,
  type SeoAction,
  type SeoAudit,
} from "../../../api/modules/seo";
import { apiErrorMessage } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import styles from "./index.module.less";

export function ActionsPanel({
  projectId,
  isVisible,
}: {
  projectId: string;
  isVisible: boolean;
}) {
  const { t, i18n } = useTranslation();
  const zone = useServerTimezone();
  const [rows, setRows] = useState<SeoAction[]>();
  const [audits, setAudits] = useState<SeoAudit[]>([]);
  const [auditId, setAuditId] = useState<string>();
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!isVisible) return;
    let live = true;
    Promise.all([seoApi.actions(projectId), seoApi.audits(projectId)])
      .then(([actions, list]) => {
        if (live) {
          setRows(actions);
          setAudits(list);
          setError("");
        }
      })
      .catch((err) => {
        if (live) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      live = false;
    };
  }, [projectId, isVisible, revision, t]);
  async function update(
    row: SeoAction,
    status: "open" | "in_progress" | "implemented" | "verify",
  ) {
    const note = (notes[row.action_id] ?? "").trim();
    if (status === "implemented" && !note) {
      setError(t("seo.actions.noteRequired"));
      return;
    }
    if (status === "verify" && !auditId) {
      setError(t("seo.actions.chooseAudit"));
      return;
    }
    setBusy(row.action_id);
    setError("");
    try {
      const value =
        status === "verify"
          ? await seoApi.verifyAction(projectId, row.action_id, auditId!)
          : await seoApi.updateAction(projectId, row.action_id, {
              status,
              note,
            });
      setRows(
        (current) =>
          current?.map((item) =>
            item.action_id === value.action_id ? value : item,
          ),
      );
    } catch (err) {
      setError(apiErrorMessage(err, t("seo.operationError"), t));
    } finally {
      setBusy("");
    }
  }
  return (
    <Card
      size="small"
      title={
        rows
          ? t("seo.actions.list", { count: rows.length })
          : t("seo.actions.listTitle")
      }
    >
      <div className={styles.stack}>
        {error && <Alert type="error" showIcon message={error} />}
        <div className={styles.toolbar}>
          <Select
            className={styles.projectSelect}
            aria-label={t("seo.actions.chooseAudit")}
            placeholder={t("seo.actions.chooseAudit")}
            value={auditId}
            onChange={setAuditId}
            options={audits
              .filter((a) => a.status === "completed")
              .map((a) => ({
                value: a.audit_id,
                label: `${formatServerIsoDateTime(
                  a.completed_at ?? a.created_at,
                  zone,
                )} · ${a.requested_url}`,
              }))}
          />
          <Button onClick={() => setRevision((n) => n + 1)}>
            {t("seo.actions.refresh")}
          </Button>
        </div>
        <Table<SeoAction>
          size="small"
          rowKey="action_id"
          loading={!rows && !error}
          dataSource={rows}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={t("seo.actions.empty")}
              />
            ),
          }}
          expandable={{
            expandedRowRender: (row) => (
              <Space direction="vertical">
                {row.history.map((event, index) => (
                  <Typography.Text key={index}>
                    {formatServerIsoDateTime(event.at, zone)} ·{" "}
                    {t(`seo.actions.status.${event.status}`)} · {event.note}
                  </Typography.Text>
                ))}
                {row.verification && (
                  <Typography.Text>
                    {t(`seo.actions.reason.${row.verification.reason}`)} ·{" "}
                    {row.verification.audit_id}
                  </Typography.Text>
                )}
              </Space>
            ),
          }}
          columns={[
            {
              title: t("seo.auditUrl"),
              render: (_, row) => (
                <Space direction="vertical">
                  <Typography.Text>{row.source.page_url}</Typography.Text>
                  <Typography.Text>
                    {row.source.labels?.[
                      i18n?.language?.startsWith("zh") ? "zh" : "en"
                    ] ?? row.source.issue_type}
                  </Typography.Text>
                </Space>
              ),
            },
            {
              title: t("seo.actions.state"),
              render: (_, row) => (
                <Tag color={row.status === "verified" ? "green" : "default"}>
                  {t(`seo.actions.status.${row.status}`)}
                </Tag>
              ),
            },
            {
              title: t("seo.actions.progress"),
              render: (_, row) => (
                <Space direction="vertical">
                  <Input.TextArea
                    aria-label={t("seo.actions.note")}
                    placeholder={t("seo.actions.note")}
                    maxLength={4000}
                    value={notes[row.action_id] ?? ""}
                    onChange={(e) =>
                      setNotes((n) => ({
                        ...n,
                        [row.action_id]: e.target.value,
                      }))
                    }
                  />
                  {row.implementation && (
                    <Typography.Text>{row.implementation.note}</Typography.Text>
                  )}
                  <Space wrap>
                    <Button
                      disabled={!!busy}
                      onClick={() => void update(row, "in_progress")}
                    >
                      {t("seo.actions.start")}
                    </Button>
                    <Button
                      disabled={!!busy}
                      onClick={() => void update(row, "implemented")}
                    >
                      {t("seo.actions.implemented")}
                    </Button>
                    <Button
                      disabled={!!busy || !row.implementation}
                      onClick={() => void update(row, "verify")}
                    >
                      {t("seo.actions.verify")}
                    </Button>
                    <Button
                      disabled={!!busy}
                      onClick={() => void update(row, "open")}
                    >
                      {t("seo.actions.reopen")}
                    </Button>
                  </Space>
                </Space>
              ),
            },
          ]}
        />
      </div>
    </Card>
  );
}
