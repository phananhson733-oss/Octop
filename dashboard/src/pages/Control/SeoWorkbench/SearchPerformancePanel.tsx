import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import { useLocation, useSearchParams } from "react-router-dom";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  seoApi,
  type GoogleStatus,
  type GscAccountSites,
  type GscConnection,
  type GscDateRange,
  type GscDimensionRow,
  type GscInspection,
  type GscReport,
  type GscReportQuery,
  type GscTable,
  type GscTotals,
} from "../../../api/modules/seo";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import styles from "./index.module.less";

const { Text, Paragraph } = Typography;
const DATE_RANGES: GscDateRange[] = [
  "last_7_days",
  "last_28_days",
  "last_3_months",
  "last_6_months",
  "last_12_months",
  "last_16_months",
];
const DEVICES = ["DESKTOP", "MOBILE", "TABLET"] as const;
const PAGE_SIZE = 25;
// Reasons the server's Google callback sends back in ?seo_google_error=.
const CALLBACK_ERRORS = [
  "invalid_state",
  "forbidden",
  "access_denied",
  "google_error",
  "missing_code",
  "not_configured",
  "code_rejected",
  "scope_not_granted",
];
const UNVERIFIED = "siteUnverifiedUser";
type TableView = "query" | "page" | "striking" | "countries";
const METRIC_LABELS = {
  clicks: "seo.gscClicks",
  impressions: "seo.gscImpressions",
  position: "seo.gscPosition",
} as const;

const count = (value: number) => Math.round(value).toLocaleString();
const percent = (value: number) => `${(value * 100).toFixed(1)}%`;
const rank = (value: number | null | undefined) =>
  value == null ? "—" : value.toFixed(1);

/** Change against the previous period; position improves as it falls. */
function delta(
  metric: keyof GscTotals,
  current: number,
  previous: number,
): { text: string; good: boolean } | null {
  if (metric === "position") {
    if (!current || !previous) return null;
    const diff = current - previous;
    return {
      text: `${diff > 0 ? "+" : ""}${diff.toFixed(1)}`,
      good: diff <= 0,
    };
  }
  if (metric === "ctr") {
    const diff = (current - previous) * 100;
    return {
      text: `${diff > 0 ? "+" : ""}${diff.toFixed(1)} pp`,
      good: diff >= 0,
    };
  }
  if (!previous) return null;
  const ratio = (current - previous) / previous;
  return {
    text: `${ratio > 0 ? "+" : ""}${(ratio * 100).toFixed(0)}%`,
    good: ratio >= 0,
  };
}

/**
 * Search performance from the project's Search Console property (OpenSEO's
 * search performance page). Data is read live from Google on every view and is
 * free; nothing is stored apart from the admin's encrypted Google grant.
 */
export function SearchPerformancePanel({ projectId }: { projectId: string }) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [query, setQuery] = useSearchParams();
  const location = useLocation();
  const [google, setGoogle] = useState<GoogleStatus>();
  const [connection, setConnection] = useState<GscConnection | null>();
  const [sites, setSites] = useState<GscAccountSites[]>();
  const [choice, setChoice] = useState<string>();
  const [filters, setFilters] = useState<GscReportQuery>({
    date_range: "last_28_days",
  });
  const [queryText, setQueryText] = useState("");
  const [pageText, setPageText] = useState("");
  const [report, setReport] = useState<GscReport>();
  const [view, setView] = useState<TableView>("query");
  const [page, setPage] = useState(1);
  const [table, setTable] = useState<GscTable>();
  const [inspectText, setInspectText] = useState("");
  const [inspections, setInspections] = useState<GscInspection[]>([]);
  const [version, setVersion] = useState(0);
  const [loading, setLoading] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const fail = (err: unknown) =>
    setError(apiErrorMessage(err, t("seo.operationError"), t));

  // Google sends the admin back here with the outcome in the query string.
  useEffect(() => {
    const result = query.get("seo_google");
    const reason = query.get("seo_google_error");
    if (!result && !reason) return;
    if (result === "connected") setNotice(t("seo.gscConnected"));
    if (reason)
      setError(
        t(
          `seo.gscCallback_${
            CALLBACK_ERRORS.includes(reason) ? reason : "google_error"
          }`,
        ),
      );
    const next = new URLSearchParams(query);
    next.delete("seo_google");
    next.delete("seo_google_error");
    setQuery(next, { replace: true });
  }, [query, setQuery, t]);

  useEffect(() => {
    let active = true;
    Promise.all([seoApi.google(), seoApi.gsc(projectId)])
      .then(([status, gsc]) => {
        if (!active) return;
        setGoogle(status);
        setConnection(gsc.connection);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [projectId, t, version]);

  const accountCount = google?.accounts.length ?? 0;
  useEffect(() => {
    if (connection !== null || !accountCount) return;
    let active = true;
    setLoading("sites");
    seoApi
      .gscSites(projectId)
      .then((result) => {
        if (active) setSites(result.accounts);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      })
      .finally(() => {
        if (active) setLoading("");
      });
    return () => {
      active = false;
    };
  }, [accountCount, connection, projectId, t]);

  useEffect(() => {
    if (!connection) return;
    let active = true;
    setLoading("report");
    seoApi
      .gscReport(projectId, filters)
      .then((result) => {
        if (active) setReport(result);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      })
      .finally(() => {
        if (active) setLoading("");
      });
    return () => {
      active = false;
    };
  }, [connection, filters, projectId, t]);

  useEffect(() => {
    if (!connection || (view !== "query" && view !== "page")) return;
    let active = true;
    seoApi
      .gscTable(projectId, {
        ...filters,
        dimension: view,
        page,
        page_size: PAGE_SIZE,
      })
      .then((result) => {
        if (active) setTable(result);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [connection, filters, page, projectId, t, view]);

  const changeFilters = (next: Partial<GscReportQuery>) => {
    setError("");
    setPage(1);
    setFilters((current) => ({ ...current, ...next }));
  };
  const applyText = () =>
    changeFilters({
      query_filter: queryText.trim()
        ? { operator: "contains", expression: queryText.trim() }
        : undefined,
      page_filter: pageText.trim()
        ? { operator: "contains", expression: pageText.trim() }
        : undefined,
    });

  async function act(name: string, run: () => Promise<unknown>) {
    setBusy(name);
    setError("");
    setNotice("");
    try {
      await run();
    } catch (err) {
      fail(err);
    } finally {
      setBusy("");
    }
  }
  async function connect() {
    setBusy("connect");
    setError("");
    try {
      const { authorize_url } = await seoApi.connectGoogle(
        location.pathname + location.search,
      );
      window.location.assign(authorize_url);
    } catch (err) {
      fail(err);
      setBusy("");
    }
  }
  const bind = () =>
    act("bind", async () => {
      const [grantId, siteUrl] = JSON.parse(choice ?? "[]") as string[];
      const bound = await seoApi.bindGsc(projectId, {
        grant_id: grantId,
        site_url: siteUrl,
      });
      setConnection(bound);
    });
  const unbind = () =>
    act("unbind", async () => {
      await seoApi.unbindGsc(projectId);
      setConnection(null);
      setReport(undefined);
      setTable(undefined);
      setSites(undefined);
    });
  const disconnect = (grantId: string) =>
    act(`disconnect:${grantId}`, async () => {
      await seoApi.disconnectGoogle(grantId);
      setSites(undefined);
      setReport(undefined);
      setVersion((v) => v + 1);
    });
  const inspect = () =>
    act("inspect", async () => {
      const urls = [...new Set(inspectText.split(/\s+/).filter(Boolean))].slice(
        0,
        10,
      );
      const result = await seoApi.gscInspect(projectId, urls);
      setInspections(result.results);
    });

  const rowColumns = (keyTitle: string) => [
    {
      title: keyTitle,
      dataIndex: "key",
      render: (value: string) => (
        <Text copyable ellipsis={{ tooltip: value }}>
          {value}
        </Text>
      ),
    },
    {
      title: t("seo.gscClicks"),
      dataIndex: "clicks",
      width: 100,
      render: count,
    },
    {
      title: t("seo.gscImpressions"),
      dataIndex: "impressions",
      width: 110,
      render: count,
    },
    { title: "CTR", dataIndex: "ctr", width: 90, render: percent },
    {
      title: t("seo.gscPosition"),
      dataIndex: "position",
      width: 100,
      render: rank,
    },
  ];

  if (!google) return <Card loading size="small" />;

  const accounts = (
    <Card size="small" title={t("seo.gscAccounts")}>
      <div className={styles.stack}>
        {!google.configured && (
          <Alert
            type="info"
            showIcon
            message={t("seo.gscNotConfigured")}
            description={
              <Paragraph copyable={{ text: google.redirect_uri }}>
                {t("seo.gscRedirectHelp", { uri: google.redirect_uri })}
              </Paragraph>
            }
          />
        )}
        {google.accounts.map((account) => (
          <div key={account.grant_id} className={styles.toolbar}>
            <Text strong>{account.email ?? account.grant_id}</Text>
            {account.search_console && <Tag color="blue">Search Console</Tag>}
            {account.analytics && <Tag color="green">Analytics</Tag>}
            <Popconfirm
              title={t("seo.gscDisconnectConfirm")}
              onConfirm={() => disconnect(account.grant_id)}
            >
              <Button
                size="small"
                danger
                loading={busy === `disconnect:${account.grant_id}`}
              >
                {t("seo.gscDisconnect")}
              </Button>
            </Popconfirm>
          </div>
        ))}
        {google.configured && (
          <div className={styles.toolbar}>
            <Button
              type={accountCount ? "default" : "primary"}
              loading={busy === "connect"}
              onClick={connect}
            >
              {t(accountCount ? "seo.gscConnectAnother" : "seo.gscConnect")}
            </Button>
            <Text type="secondary">{t("seo.gscConnectHelp")}</Text>
          </div>
        )}
      </div>
    </Card>
  );

  const binder = connection === null && accountCount > 0 && (
    <Card size="small" title={t("seo.gscProperty")}>
      <div className={styles.stack}>
        {sites?.map(
          (account) =>
            (account.requires_reconnect || account.api_disabled) && (
              <Alert
                key={account.grant_id}
                type="warning"
                showIcon
                message={t(
                  account.api_disabled
                    ? "seo.gscApiDisabled"
                    : "seo.gscReconnect",
                  { email: account.email ?? account.grant_id },
                )}
              />
            ),
        )}
        <div className={styles.toolbar}>
          <Select
            aria-label={t("seo.gscProperty")}
            className={styles.projectSelect}
            loading={loading === "sites"}
            value={choice}
            onChange={setChoice}
            placeholder={t("seo.gscPickProperty")}
            options={(sites ?? []).map((account) => ({
              label: account.email ?? account.grant_id,
              options: account.sites.map((site) => ({
                value: JSON.stringify([account.grant_id, site.site_url]),
                label:
                  site.permission_level === UNVERIFIED
                    ? `${site.site_url} (${t("seo.gscUnverified")})`
                    : site.site_url,
                disabled: site.permission_level === UNVERIFIED,
              })),
            }))}
          />
          <Button
            type="primary"
            disabled={!choice}
            loading={busy === "bind"}
            onClick={bind}
          >
            {t("seo.gscBind")}
          </Button>
        </div>
        {sites && !sites.some((account) => account.sites.length) && (
          <Text type="secondary">{t("seo.gscNoProperties")}</Text>
        )}
      </div>
    </Card>
  );

  const totals = report && (
    <div className={styles.metrics}>
      {(["clicks", "impressions", "ctr", "position"] as const).map((metric) => {
        const current = report.totals[metric];
        const change = delta(metric, current, report.prev_totals[metric]);
        return (
          <Card key={metric} size="small">
            <Text type="secondary">
              {metric === "ctr" ? "CTR" : t(METRIC_LABELS[metric])}
            </Text>
            <div className={styles.metricValue}>
              {metric === "ctr"
                ? percent(current)
                : metric === "position"
                ? rank(current || null)
                : count(current)}
            </div>
            {change && (
              <Text type={change.good ? "success" : "danger"}>
                {change.text}
              </Text>
            )}
          </Card>
        );
      })}
    </div>
  );

  const performance = connection && (
    <div className={styles.stack}>
      <Card size="small">
        <div className={styles.stack}>
          <div className={styles.toolbar}>
            <Text>
              {t("seo.gscBoundTo", {
                site: connection.site_url,
                email: connection.email ?? connection.grant_id,
              })}
            </Text>
            <Popconfirm title={t("seo.gscUnbindConfirm")} onConfirm={unbind}>
              <Button size="small" loading={busy === "unbind"}>
                {t("seo.gscUnbind")}
              </Button>
            </Popconfirm>
          </div>
          <div className={styles.toolbar}>
            <Select
              aria-label={t("seo.gscRange")}
              value={filters.date_range}
              onChange={(date_range) => changeFilters({ date_range })}
              options={DATE_RANGES.map((value) => ({
                value,
                label: t(`seo.gscRange_${value}`),
              }))}
            />
            <Select
              aria-label={t("seo.gscDevice")}
              value={filters.device ?? ""}
              onChange={(value) =>
                changeFilters({
                  device: (value || undefined) as GscReportQuery["device"],
                })
              }
              options={[
                { value: "", label: t("seo.gscAllDevices") },
                ...DEVICES.map((value) => ({
                  value,
                  label: t(`seo.gscDevice_${value}`),
                })),
              ]}
            />
            <Select
              aria-label={t("seo.gscCountry")}
              className={styles.countrySelect}
              value={filters.country ?? ""}
              onChange={(value) =>
                changeFilters({ country: value || undefined })
              }
              options={[
                { value: "", label: t("seo.gscAllCountries") },
                ...(report?.countries ?? []).map((row) => ({
                  value: row.key,
                  label: row.key.toUpperCase(),
                })),
              ]}
            />
            <Input
              aria-label={t("seo.gscQueryContains")}
              className={styles.filterInput}
              placeholder={t("seo.gscQueryContains")}
              value={queryText}
              onChange={(event) => setQueryText(event.target.value)}
              onPressEnter={applyText}
              allowClear
            />
            <Input
              aria-label={t("seo.gscPageContains")}
              className={styles.filterInput}
              placeholder={t("seo.gscPageContains")}
              value={pageText}
              onChange={(event) => setPageText(event.target.value)}
              onPressEnter={applyText}
              allowClear
            />
            <Button onClick={applyText}>{t("seo.gscApply")}</Button>
          </div>
          {report && (
            <Text type="secondary">
              {t("seo.gscRangeText", {
                start: report.range.start_date,
                end: report.range.end_date,
                prevStart: report.range.prev_start_date,
                prevEnd: report.range.prev_end_date,
              })}
            </Text>
          )}
        </div>
      </Card>
      {loading === "report" && !report && <Card loading size="small" />}
      {totals}
      {report && report.daily.length > 0 && (
        <Card size="small" title={t("seo.gscDaily")}>
          <div className={styles.chart}>
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <LineChart data={report.daily}>
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="var(--fn-border-secondary)"
                />
                <XAxis dataKey="date" />
                <YAxis yAxisId="clicks" />
                <YAxis yAxisId="impressions" orientation="right" />
                <RechartsTooltip />
                <Line
                  yAxisId="clicks"
                  dataKey="clicks"
                  name={t("seo.gscClicks")}
                  stroke="#1677ff"
                  dot={false}
                />
                <Line
                  yAxisId="impressions"
                  dataKey="impressions"
                  name={t("seo.gscImpressions")}
                  stroke="#8c8c8c"
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>
      )}
      <Tabs
        activeKey={view}
        onChange={(next) => {
          setView(next as TableView);
          setPage(1);
          setTable(undefined);
        }}
        items={[
          { key: "query", label: t("seo.gscTopQueries") },
          { key: "page", label: t("seo.gscTopPages") },
          {
            key: "striking",
            label: t("seo.gscStriking", {
              count: report?.striking_distance.length ?? 0,
            }),
          },
          { key: "countries", label: t("seo.gscCountries") },
        ].map((item) => ({
          ...item,
          children:
            item.key === "striking" ? (
              <div className={styles.stack}>
                <Text type="secondary">{t("seo.gscStrikingHelp")}</Text>
                <Table
                  size="small"
                  rowKey="query"
                  dataSource={report?.striking_distance ?? []}
                  pagination={{ pageSize: PAGE_SIZE, hideOnSinglePage: true }}
                  columns={[
                    {
                      title: t("seo.keyword"),
                      dataIndex: "query",
                    },
                    {
                      title: t("seo.gscBestPage"),
                      dataIndex: "page",
                      render: (value: string) => (
                        <Text copyable ellipsis={{ tooltip: value }}>
                          {value}
                        </Text>
                      ),
                    },
                    {
                      title: t("seo.gscImpressions"),
                      dataIndex: "impressions",
                      width: 110,
                      render: count,
                    },
                    {
                      title: t("seo.gscClicks"),
                      dataIndex: "clicks",
                      width: 100,
                      render: count,
                    },
                    {
                      title: t("seo.gscPosition"),
                      dataIndex: "position",
                      width: 100,
                      render: rank,
                    },
                  ]}
                />
              </div>
            ) : item.key === "countries" ? (
              <Table<GscDimensionRow>
                size="small"
                rowKey="key"
                dataSource={report?.countries ?? []}
                pagination={false}
                columns={rowColumns(t("seo.gscCountry"))}
              />
            ) : (
              <div className={styles.stack}>
                <Table<GscDimensionRow>
                  size="small"
                  rowKey="key"
                  loading={!table}
                  dataSource={table?.rows ?? []}
                  pagination={false}
                  columns={rowColumns(
                    t(item.key === "page" ? "seo.gscPage" : "seo.keyword"),
                  )}
                />
                <Space>
                  <Button
                    size="small"
                    disabled={page <= 1}
                    onClick={() => setPage((p) => p - 1)}
                  >
                    {t("seo.gscPrev")}
                  </Button>
                  <Text>{t("seo.gscPageNumber", { page })}</Text>
                  <Button
                    size="small"
                    disabled={!table?.has_next_page}
                    onClick={() => setPage((p) => p + 1)}
                  >
                    {t("seo.gscNext")}
                  </Button>
                </Space>
              </div>
            ),
        }))}
      />
      <Card size="small" title={t("seo.gscInspect")}>
        <div className={styles.stack}>
          <Text type="secondary">{t("seo.gscInspectHelp")}</Text>
          <Input.TextArea
            aria-label={t("seo.gscInspect")}
            rows={3}
            value={inspectText}
            onChange={(event) => setInspectText(event.target.value)}
            placeholder={`https://${connection.site_url.replace(
              /^sc-domain:|^https?:\/\/|\/$/g,
              "",
            )}/`}
          />
          <div>
            <Button
              disabled={!inspectText.trim()}
              loading={busy === "inspect"}
              onClick={inspect}
            >
              {t("seo.gscInspectRun")}
            </Button>
          </div>
          {inspections.length > 0 && (
            <Table<GscInspection>
              size="small"
              rowKey="url"
              dataSource={inspections}
              pagination={false}
              columns={[
                {
                  title: t("seo.auditUrl"),
                  dataIndex: "url",
                  render: (value: string) => (
                    <Text copyable ellipsis={{ tooltip: value }}>
                      {value}
                    </Text>
                  ),
                },
                {
                  title: t("seo.gscVerdict"),
                  render: (_: unknown, row) => {
                    if (row.error)
                      return <Text type="danger">{row.error}</Text>;
                    const index = indexStatus(row);
                    return (
                      <Space direction="vertical" size={0}>
                        <Tag
                          color={index.verdict === "PASS" ? "green" : "orange"}
                        >
                          {index.verdict ?? "—"}
                        </Tag>
                        <Text type="secondary">{index.coverageState}</Text>
                      </Space>
                    );
                  },
                },
                {
                  title: t("seo.gscLastCrawl"),
                  width: 180,
                  render: (_: unknown, row) => {
                    const crawled = indexStatus(row).lastCrawlTime;
                    return crawled
                      ? formatServerIsoDateTime(crawled, timeZone)
                      : "—";
                  },
                },
                {
                  title: t("seo.gscCanonical"),
                  render: (_: unknown, row) => {
                    const index = indexStatus(row);
                    if (!index.googleCanonical) return "—";
                    const mismatch =
                      !!index.userCanonical &&
                      index.userCanonical !== index.googleCanonical;
                    return (
                      <Space direction="vertical" size={0}>
                        <Text ellipsis={{ tooltip: index.googleCanonical }}>
                          {index.googleCanonical}
                        </Text>
                        {mismatch && (
                          <Text type="warning">
                            {t("seo.gscCanonicalMismatch", {
                              declared: index.userCanonical,
                            })}
                          </Text>
                        )}
                      </Space>
                    );
                  },
                },
              ]}
            />
          )}
        </div>
      </Card>
    </div>
  );

  return (
    <div className={styles.stack}>
      {error && <Alert type="error" showIcon message={error} />}
      {notice && <Alert type="success" showIcon message={notice} />}
      {accounts}
      {binder}
      {performance}
      {connection === null && google.configured && !accountCount && (
        <Empty description={t("seo.gscConnectFirst")} />
      )}
    </div>
  );
}

interface IndexStatus {
  verdict?: string;
  coverageState?: string;
  lastCrawlTime?: string;
  googleCanonical?: string;
  userCanonical?: string;
}

function indexStatus(row: GscInspection): IndexStatus {
  const index = (row.result as { indexStatusResult?: IndexStatus } | null)
    ?.indexStatusResult;
  return index ?? {};
}
