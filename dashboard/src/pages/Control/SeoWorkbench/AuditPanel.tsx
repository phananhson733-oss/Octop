import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Popconfirm,
  Progress,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  seoApi,
  type AuditIssue,
  type AuditIssueType,
  type AuditPage,
  type AuditSeverity,
  type SeoAudit,
  type SeoAuditDetail,
} from "../../../api/modules/seo";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import { attemptKey } from "./attemptKey";
import { isServerUnreachable } from "./serverUnreachable";
import { AskExpertButton } from "./AskExpert";
import styles from "./index.module.less";

const { Text, Paragraph } = Typography;
const POLL_MS = 3000;
const SEVERITY_COLOR: Record<AuditSeverity, string> = {
  critical: "red",
  warning: "orange",
  info: "blue",
};
const isActive = (audit?: SeoAudit) =>
  audit?.status === "queued" || audit?.status === "running";

function detailText(details: AuditIssue["details"]): string {
  if (!details) return "";
  return Object.entries(details)
    .filter(([, value]) => value != null && value !== "")
    .map(
      ([key, value]) =>
        `${key}: ${
          Array.isArray(value)
            ? value.join(key === "hops" ? " → " : ", ")
            : value
        }`,
    )
    .join(" · ");
}

/**
 * Technical site audit (OpenSEO site audit). Crawling runs on the server; this
 * panel starts it, polls progress while it runs, and reads the stored results.
 */
export function AuditPanel({
  projectId,
  domain,
  isVisible,
}: {
  projectId: string;
  domain: string;
  isVisible: boolean;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [audits, setAudits] = useState<SeoAudit[]>([]);
  const [auditId, setAuditId] = useState<string>();
  const [detail, setDetail] = useState<SeoAuditDetail>();
  const [summary, setSummary] = useState<AuditIssueType[]>([]);
  const [issues, setIssues] = useState<AuditIssue[]>([]);
  const [pages, setPages] = useState<AuditPage[]>([]);
  const [startUrl, setStartUrl] = useState(`https://${domain}/`);
  const [maxPages, setMaxPages] = useState(50);
  const [severity, setSeverity] = useState<AuditSeverity | "all">("all");
  const [urlFilter, setUrlFilter] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  // Background reads (progress poll, results reread) keep their own failure
  // so the next successful read clears it instead of leaving a stale banner.
  const [pollError, setPollError] = useState<unknown>();
  const [resultsError, setResultsError] = useState<unknown>();
  const attempt = useRef<{ input: string; key: string }>();
  const date = (value: string) => formatServerIsoDateTime(value, timeZone);
  const fail = useCallback(
    (err: unknown) =>
      setError(apiErrorMessage(err, t("seo.operationError"), t)),
    [t],
  );

  useEffect(() => {
    let live = true;
    seoApi
      .audits(projectId)
      .then((list) => {
        if (!live) return;
        setAudits(list);
        setAuditId((selected) => selected ?? list[0]?.audit_id);
      })
      .catch((err) => {
        if (live) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      live = false;
    };
  }, [projectId, t]);

  // Progress is polled only while the audit runs and the page is on screen.
  const running = isActive(detail);
  useEffect(() => {
    if (!auditId || !isVisible) return;
    let live = true;
    const load = () =>
      seoApi
        .audit(projectId, auditId)
        .then((value) => {
          if (!live) return;
          setPollError(undefined);
          setDetail(value);
          setAudits((list) =>
            list.map((item) =>
              item.audit_id === value.audit_id ? value : item,
            ),
          );
        })
        .catch((err) => {
          if (live) setPollError(err);
        });
    void load();
    const timer = running ? window.setInterval(load, POLL_MS) : undefined;
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [projectId, auditId, isVisible, running]);

  // Results are reread when the crawl moves on or ends, not on every poll.
  const progressKey =
    detail && detail.audit_id === auditId
      ? `${detail.audit_id}:${detail.status}:${detail.pages_crawled}`
      : "";
  useEffect(() => {
    if (!progressKey || !auditId) return;
    let live = true;
    Promise.all([
      seoApi.auditIssues(projectId, auditId),
      seoApi.auditPages(projectId, auditId),
    ])
      .then(([issueResult, pageResult]) => {
        if (!live) return;
        setResultsError(undefined);
        setSummary(issueResult.summary);
        setIssues(issueResult.issues);
        setPages(pageResult.pages);
      })
      .catch((err) => {
        if (live) setResultsError(err);
      });
    return () => {
      live = false;
    };
  }, [projectId, auditId, progressKey]);

  const busyAudit = audits.find((audit) => isActive(audit));
  async function start() {
    const input = JSON.stringify({ startUrl, maxPages });
    if (attempt.current?.input !== input)
      attempt.current = { input, key: attemptKey() };
    setBusy("start");
    setError("");
    try {
      const created = await seoApi.startAudit(projectId, {
        start_url: startUrl.trim(),
        max_pages: maxPages,
        idempotency_key: attempt.current.key,
      });
      attempt.current = undefined;
      setAudits((list) => [
        created,
        ...list.filter((item) => item.audit_id !== created.audit_id),
      ]);
      select(created.audit_id);
    } catch (err) {
      fail(err);
    } finally {
      setBusy("");
    }
  }
  function select(next: string) {
    setAuditId(next);
    setPollError(undefined);
    setResultsError(undefined);
    setDetail(undefined);
    setSummary([]);
    setIssues([]);
    setPages([]);
  }
  async function cancel() {
    if (!auditId) return;
    setBusy("cancel");
    try {
      const value = await seoApi.cancelAudit(projectId, auditId);
      setDetail((current) => (current ? { ...current, ...value } : current));
    } catch (err) {
      fail(err);
    } finally {
      setBusy("");
    }
  }
  async function remove() {
    if (!auditId) return;
    setBusy("delete");
    try {
      await seoApi.deleteAudit(projectId, auditId);
      const rest = audits.filter((item) => item.audit_id !== auditId);
      setAudits(rest);
      if (rest[0]) select(rest[0].audit_id);
      else select("");
    } catch (err) {
      fail(err);
    } finally {
      setBusy("");
    }
  }

  const byType = useMemo(() => {
    const grouped = new Map<string, AuditIssue[]>();
    for (const issue of issues)
      grouped.set(issue.issue_type, [
        ...(grouped.get(issue.issue_type) ?? []),
        issue,
      ]);
    return grouped;
  }, [issues]);
  const shownSummary = summary.filter(
    (row) => severity === "all" || row.severity === severity,
  );
  const shownPages = pages.filter((page) => page.url.includes(urlFilter));
  const notices = detail ? auditNotices(detail, pages, t) : [];
  const readError = pollError ?? resultsError;
  const serverAway = readError !== undefined && isServerUnreachable(readError);

  return (
    <div className={styles.stack}>
      <Card size="small">
        <div className={styles.stack}>
          <Paragraph type="secondary">{t("seo.auditHelp")}</Paragraph>
          <div className={styles.toolbar}>
            <Input
              aria-label={t("seo.auditStartUrl")}
              className={styles.projectSelect}
              value={startUrl}
              onChange={(event) => setStartUrl(event.target.value)}
              maxLength={2048}
            />
            <Select
              aria-label={t("seo.auditMaxPages")}
              value={maxPages}
              onChange={setMaxPages}
              options={[50, 100, 200].map((value) => ({
                value,
                label: t("seo.auditPagesOption", { count: value }),
              }))}
            />
            <Button
              type="primary"
              loading={busy === "start"}
              disabled={!!busy || !startUrl.trim() || !!busyAudit}
              onClick={start}
            >
              {t("seo.auditStart")}
            </Button>
          </div>
          {busyAudit && <Text type="secondary">{t("seo.auditBusy")}</Text>}
        </div>
      </Card>
      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          closable
          onClose={() => setError("")}
        />
      )}
      {readError !== undefined && (
        <Alert
          type={serverAway ? "warning" : "error"}
          showIcon
          message={
            serverAway
              ? t(running ? "seo.auditServerRestarting" : "seo.serverAway")
              : apiErrorMessage(readError, t("seo.loadError"), t)
          }
          closable
          onClose={() => {
            setPollError(undefined);
            setResultsError(undefined);
          }}
        />
      )}
      {audits.length ? (
        <div className={styles.toolbar}>
          <Select
            aria-label={t("seo.auditHistory")}
            className={styles.projectSelect}
            value={auditId || undefined}
            onChange={select}
            options={audits.map((audit) => ({
              value: audit.audit_id,
              label: `${date(audit.created_at)} · ${t(
                `seo.auditStatus_${audit.status}`,
              )} · ${audit.requested_url}`,
            }))}
          />
          {isActive(detail) && (
            <Button
              loading={busy === "cancel"}
              disabled={!!busy || detail?.cancel_requested}
              onClick={cancel}
            >
              {t("seo.auditCancel")}
            </Button>
          )}
          <Popconfirm
            title={t("seo.auditDeleteConfirm")}
            onConfirm={remove}
            disabled={!auditId}
          >
            <Button danger disabled={!!busy || !auditId}>
              {t("seo.auditDelete")}
            </Button>
          </Popconfirm>
          {detail && !isActive(detail) && detail.pages_crawled > 0 && (
            <AskExpertButton
              disabled={!!busy}
              what={t("seo.askExpertWhat.audit", { id: detail.audit_id })}
            />
          )}
        </div>
      ) : (
        <Empty description={t("seo.auditEmpty")} />
      )}
      {detail && (
        <Card size="small">
          <div className={styles.stack}>
            <Space wrap>
              <Tag>{t(`seo.auditStatus_${detail.status}`)}</Tag>
              {isActive(detail) && (
                <Text>{t(`seo.auditPhase_${detail.phase}`)}</Text>
              )}
              <Text>{detail.start_url ?? detail.requested_url}</Text>
              {(["critical", "warning", "info"] as const).map((level) => (
                <Tag key={level} color={SEVERITY_COLOR[level]}>
                  {t(`seo.auditSeverity_${level}`)} {detail.issue_counts[level]}
                </Tag>
              ))}
            </Space>
            <Progress
              percent={Math.round(
                (100 * detail.pages_crawled) /
                  Math.max(detail.pages_total || detail.max_pages, 1),
              )}
              status={
                detail.status === "failed"
                  ? "exception"
                  : isActive(detail)
                  ? "active"
                  : "normal"
              }
            />
            <Text type="secondary">
              {t("seo.auditProgress", {
                crawled: detail.pages_crawled,
                total: detail.pages_total || detail.max_pages,
                discovered: detail.discovered_count,
                sitemap: detail.sitemap_count,
              })}
            </Text>
            {notices.map((notice) => (
              <Alert
                key={notice.key}
                type={notice.type}
                showIcon
                message={notice.message}
              />
            ))}
          </div>
        </Card>
      )}
      {detail && (
        <Tabs
          size="small"
          items={[
            {
              key: "issues",
              label: t("seo.auditIssues"),
              children: (
                <div className={styles.stack}>
                  <Select
                    aria-label={t("seo.auditSeverity")}
                    value={severity}
                    onChange={setSeverity}
                    style={{ width: 160 }}
                    options={(
                      ["all", "critical", "warning", "info"] as const
                    ).map((value) => ({
                      value,
                      label: t(`seo.auditSeverity_${value}`),
                    }))}
                  />
                  <Table<AuditIssueType>
                    rowKey="issue_type"
                    dataSource={shownSummary}
                    pagination={false}
                    locale={{ emptyText: t("seo.auditNoIssues") }}
                    columns={[
                      {
                        title: t("seo.auditSeverity"),
                        dataIndex: "severity",
                        width: 110,
                        render: (value: AuditSeverity) => (
                          <Tag color={SEVERITY_COLOR[value]}>
                            {t(`seo.auditSeverity_${value}`)}
                          </Tag>
                        ),
                      },
                      { title: t("seo.auditIssue"), dataIndex: "title" },
                      {
                        title: t("seo.auditCount"),
                        dataIndex: "count",
                        width: 90,
                      },
                    ]}
                    expandable={{
                      expandedRowRender: (row) => (
                        <div className={styles.stack}>
                          <Paragraph>{row.explanation}</Paragraph>
                          <Paragraph>
                            <Text strong>{t("seo.auditHowToFix")}</Text>{" "}
                            {row.how_to_fix}
                          </Paragraph>
                          <Table<AuditIssue>
                            size="small"
                            rowKey={(issue) =>
                              `${issue.page_url}|${detailText(issue.details)}`
                            }
                            dataSource={byType.get(row.issue_type) ?? []}
                            pagination={{
                              pageSize: 20,
                              hideOnSinglePage: true,
                            }}
                            columns={[
                              {
                                title: t("seo.auditUrl"),
                                dataIndex: "page_url",
                              },
                              {
                                title: t("seo.auditDetails"),
                                dataIndex: "details",
                                render: detailText,
                              },
                            ]}
                          />
                        </div>
                      ),
                    }}
                  />
                </div>
              ),
            },
            {
              key: "pages",
              label: t("seo.auditPages", { count: pages.length }),
              children: (
                <div className={styles.stack}>
                  <Input.Search
                    aria-label={t("seo.auditFilterUrl")}
                    placeholder={t("seo.auditFilterUrl")}
                    allowClear
                    onSearch={setUrlFilter}
                  />
                  <Table<AuditPage>
                    rowKey="url"
                    size="small"
                    dataSource={shownPages}
                    scroll={{ x: 960 }}
                    pagination={{ pageSize: 50, hideOnSinglePage: true }}
                    columns={[
                      { title: t("seo.auditUrl"), dataIndex: "url" },
                      {
                        title: t("seo.auditStatusCode"),
                        dataIndex: "status_code",
                        width: 90,
                        render: (code: number, page) =>
                          page.fetch_class === "ok"
                            ? code
                            : `${code || "—"} · ${t(
                                `seo.auditFetch_${page.fetch_class}`,
                              )}`,
                      },
                      { title: t("seo.title"), dataIndex: "title" },
                      {
                        title: t("seo.auditWords"),
                        dataIndex: "word_count",
                        width: 90,
                      },
                      {
                        title: t("seo.auditIndexable"),
                        dataIndex: "is_indexable",
                        width: 100,
                        render: (value: boolean, page) =>
                          page.is_html
                            ? t(value ? "seo.auditYes" : "seo.auditNo")
                            : "—",
                      },
                      {
                        title: t("seo.auditResponse"),
                        dataIndex: "response_time_ms",
                        width: 110,
                      },
                      {
                        title: t("seo.auditDepth"),
                        dataIndex: "crawl_depth",
                        width: 80,
                        render: (value: number | null) => value ?? "—",
                      },
                    ]}
                  />
                </div>
              ),
            },
          ]}
        />
      )}
    </div>
  );
}

/** Why an audit's results are partial, so nobody reads a cut-off crawl as clean. */
function auditNotices(
  audit: SeoAuditDetail,
  pages: AuditPage[],
  t: (key: string, values?: Record<string, unknown>) => string,
): { key: string; type: "warning" | "error" | "info"; message: string }[] {
  const notices: {
    key: string;
    type: "warning" | "error" | "info";
    message: string;
  }[] = [];
  if (audit.status === "failed")
    notices.push({
      key: "failed",
      type: "error",
      message: t(`seo.auditFailed_${audit.error_code ?? "unknown"}`),
    });
  if (audit.status === "cancelled")
    notices.push({
      key: "cancelled",
      type: "warning",
      message: t("seo.auditCancelledPartial"),
    });
  if (audit.cancel_requested && isActive(audit))
    notices.push({
      key: "cancelling",
      type: "info",
      message: t("seo.auditCancelling"),
    });
  if (audit.rate_limited)
    notices.push({
      key: "rate",
      type: "warning",
      message: t("seo.auditRateLimited"),
    });
  const blocked = pages.filter((page) => page.fetch_class === "blocked").length;
  if (blocked)
    notices.push({
      key: "blocked",
      type: "warning",
      message: t("seo.auditBlocked", { count: blocked }),
    });
  if (audit.status === "completed" && audit.pages_crawled === 0)
    notices.push({
      key: "nothing",
      type: "warning",
      message: t("seo.auditNothingCrawled"),
    });
  else if (
    audit.status === "completed" &&
    audit.crawl_complete === false &&
    !audit.rate_limited
  )
    notices.push({
      key: "limit",
      type: "warning",
      message: t("seo.auditPageLimit", {
        max: audit.max_pages,
        discovered: audit.discovered_count,
      }),
    });
  if (pages.some((page) => page.javascript_shell))
    notices.push({
      key: "js",
      type: "info",
      message: t("seo.auditJsShells"),
    });
  return notices;
}
