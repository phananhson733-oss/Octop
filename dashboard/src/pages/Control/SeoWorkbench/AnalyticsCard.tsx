import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Popconfirm,
  Segmented,
  Select,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
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
  type Ga4AccountProperties,
  type Ga4Connection,
  type Ga4Overview,
} from "../../../api/modules/seo";
import { apiErrorMessage } from "../../../utils/apiError";
import styles from "./index.module.less";

const { Text } = Typography;
type Trend = "daily" | "weekly";
const METRICS = [
  ["sessions", "seo.ga4Sessions"],
  ["activeUsers", "seo.ga4ActiveUsers"],
  ["engagementRate", "seo.ga4EngagementRate"],
  ["keyEvents", "seo.ga4KeyEvents"],
] as const;

const show = (metric: string, value: number | null | undefined) => {
  if (value == null) return "—";
  return metric === "engagementRate"
    ? `${(value * 100).toFixed(1)}%`
    : Math.round(value).toLocaleString();
};

/** Change against the previous period; the rate moves in points. */
function change(overview: Ga4Overview, metric: string) {
  const row = overview.comparison[metric];
  if (!row) return null;
  if (metric === "engagementRate") {
    if (row.absolute_change == null) return null;
    const points = row.absolute_change * 100;
    return { text: `${points > 0 ? "+" : ""}${points.toFixed(1)} pp`, points };
  }
  if (row.percent_change == null) return null;
  const points = row.percent_change * 100;
  return { text: `${points > 0 ? "+" : ""}${points.toFixed(0)}%`, points };
}

/**
 * Organic-search traffic from the project's GA4 property (OpenSEO's GA4
 * card). Read live from Google on every view; nothing is stored apart from the
 * chosen property.
 */
export function AnalyticsCard({
  projectId,
  accountCount,
  version,
}: {
  projectId: string;
  accountCount: number;
  version: number;
}) {
  const { t } = useTranslation();
  const [connection, setConnection] = useState<Ga4Connection | null>();
  const [accounts, setAccounts] = useState<Ga4AccountProperties[]>();
  const [choice, setChoice] = useState<string>();
  const [trend, setTrend] = useState<Trend>("daily");
  const [overview, setOverview] = useState<Ga4Overview>();
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setAccounts(undefined);
    setOverview(undefined);
    seoApi
      .ga4(projectId)
      .then((result) => {
        if (active) setConnection(result.connection);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [projectId, t, version]);

  useEffect(() => {
    if (connection !== null || !accountCount) return;
    let active = true;
    setBusy("properties");
    seoApi
      .ga4Properties(projectId)
      .then((result) => {
        if (active) setAccounts(result.accounts);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      })
      .finally(() => {
        if (active) setBusy("");
      });
    return () => {
      active = false;
    };
  }, [accountCount, connection, projectId, t]);

  useEffect(() => {
    if (!connection) return;
    let active = true;
    setBusy("overview");
    seoApi
      .ga4Overview(projectId, { trend })
      .then((result) => {
        if (active) setOverview(result);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      })
      .finally(() => {
        if (active) setBusy("");
      });
    return () => {
      active = false;
    };
  }, [connection, projectId, t, trend]);

  async function act(name: string, run: () => Promise<unknown>) {
    setBusy(name);
    setError("");
    try {
      await run();
    } catch (err) {
      setError(apiErrorMessage(err, t("seo.operationError"), t));
    } finally {
      setBusy("");
    }
  }
  const bind = () =>
    act("bind", async () => {
      const [grantId, propertyId] = JSON.parse(choice ?? "[]") as string[];
      setConnection(
        await seoApi.bindGa4(projectId, {
          grant_id: grantId,
          property_id: propertyId,
        }),
      );
    });
  const unbind = () =>
    act("unbind", async () => {
      await seoApi.unbindGa4(projectId);
      setOverview(undefined);
      setAccounts(undefined);
      setConnection(null);
    });

  if (!accountCount || connection === undefined) return null;

  const picker = connection === null && (
    <div className={styles.stack}>
      {accounts?.map(
        (account) =>
          (account.requires_reconnect || account.api_disabled) && (
            <Alert
              key={account.grant_id}
              type="warning"
              showIcon
              message={t(
                account.api_disabled
                  ? "seo.ga4ApiDisabled"
                  : "seo.ga4Reconnect",
                { email: account.email ?? account.grant_id },
              )}
            />
          ),
      )}
      <div className={styles.toolbar}>
        <Select
          aria-label={t("seo.ga4Property")}
          className={styles.projectSelect}
          loading={busy === "properties"}
          value={choice}
          onChange={setChoice}
          placeholder={t("seo.ga4PickProperty")}
          options={(accounts ?? []).map((account) => ({
            label: account.email ?? account.grant_id,
            options: account.properties.map((property) => ({
              value: JSON.stringify([account.grant_id, property.property_id]),
              label: `${property.display_name} · ${property.account_display_name}`,
            })),
          }))}
        />
        <Button
          type="primary"
          disabled={!choice}
          loading={busy === "bind"}
          onClick={bind}
        >
          {t("seo.ga4Bind")}
        </Button>
      </div>
      {accounts && !accounts.some((account) => account.properties.length) && (
        <Text type="secondary">{t("seo.ga4NoProperties")}</Text>
      )}
    </div>
  );

  const range = overview?.request;
  const bound = connection && (
    <div className={styles.stack}>
      <div className={styles.toolbar}>
        <Text>
          {t("seo.ga4BoundTo", {
            property: connection.display_name,
            email: connection.email ?? connection.grant_id,
          })}
        </Text>
        <Popconfirm title={t("seo.ga4UnbindConfirm")} onConfirm={unbind}>
          <Button size="small" loading={busy === "unbind"}>
            {t("seo.gscUnbind")}
          </Button>
        </Popconfirm>
        <Segmented
          size="small"
          value={trend}
          onChange={(value) => setTrend(value as Trend)}
          options={[
            { value: "daily", label: t("seo.ga4Daily") },
            { value: "weekly", label: t("seo.ga4Weekly") },
          ]}
        />
      </div>
      {range && (
        <Text type="secondary">
          {t("seo.ga4RangeText", {
            start: range.resolved_date_range.start_date,
            end: range.resolved_date_range.end_date,
            prevStart: range.previous_date_range.start_date,
            prevEnd: range.previous_date_range.end_date,
            zone: range.time_zone,
          })}
        </Text>
      )}
      {overview?.diagnostics.some(
        (item) => item.code === "key_events_sharp_decline",
      ) && (
        <Alert type="warning" showIcon message={t("seo.ga4KeyEventsDrop")} />
      )}
      {overview?.has_limited_data && (
        <Alert type="info" showIcon message={t("seo.ga4LimitedData")} />
      )}
      {busy === "overview" && !overview && <Card loading size="small" />}
      {overview && (
        <div className={styles.metrics}>
          {METRICS.map(([metric, label]) => {
            const moved = change(overview, metric);
            return (
              <Card key={metric} size="small">
                <Text type="secondary">{t(label)}</Text>
                <div className={styles.metricValue}>
                  {show(metric, overview.current?.[metric])}
                </div>
                {moved && (
                  <Text type={moved.points >= 0 ? "success" : "danger"}>
                    {moved.text}
                  </Text>
                )}
              </Card>
            );
          })}
        </div>
      )}
      {overview && overview.trend.length > 0 && (
        <div className={styles.chart}>
          <ResponsiveContainer width="100%" height="100%" minWidth={0}>
            <LineChart data={overview.trend}>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="var(--fn-border-secondary)"
              />
              <XAxis dataKey={trend === "weekly" ? "yearWeek" : "date"} />
              <YAxis />
              <RechartsTooltip />
              <Line
                dataKey="sessions"
                name={t("seo.ga4Sessions")}
                stroke="#52c41a"
                dot={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );

  return (
    <Card size="small" title={t("seo.ga4Title")}>
      <div className={styles.stack}>
        {error && <Alert type="error" showIcon message={error} />}
        {picker}
        {bound}
      </div>
    </Card>
  );
}
