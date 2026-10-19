import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  seoApi,
  type SerpItem,
  type SerpSnapshot,
  type SerpSummary,
} from "../../../api/modules/seo";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import { attemptKey } from "./attemptKey";
import styles from "./index.module.less";

const { Text } = Typography;

/** Provider URLs are untrusted: only http(s) links become clickable. */
export function safeHttpUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:"
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

/**
 * Google SERP for one keyword in the project market. Opening it reads saved
 * snapshots only; every purchase is a confirmed click (OpenSEO SerpAnalysisCard).
 */
export function SerpPanel({
  projectId,
  keyword,
  configured,
}: {
  projectId: string;
  keyword: string;
  configured: boolean;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [device, setDevice] = useState<SerpSummary["device"]>("desktop");
  const [serp, setSerp] = useState<SerpSnapshot>();
  const [loading, setLoading] = useState(true);
  const [buying, setBuying] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [error, setError] = useState("");
  const attempt = useRef<{ input: string; key: string }>();
  useEffect(() => {
    let active = true;
    setSerp(undefined);
    setError("");
    setLoading(true);
    seoApi
      .serps(projectId, keyword)
      .then(async (list) => {
        const mine = list.filter((item) => item.device === device);
        // A transient empty crawl must not hide an earlier good snapshot.
        const pick =
          mine.find((item) => item.status === "completed" && item.item_count) ??
          mine[0];
        const snapshot = pick
          ? await seoApi.serp(projectId, pick.serp_id)
          : undefined;
        if (active) setSerp(snapshot);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [projectId, keyword, device, t]);
  async function buy(depth: 20 | 100) {
    const input = JSON.stringify({ keyword, device, depth });
    // An unchanged retry after an uncertain failure reuses the same key.
    if (attempt.current?.input !== input)
      attempt.current = { input, key: attemptKey() };
    setBuying(true);
    setError("");
    try {
      setSerp(
        await seoApi.buySerp(projectId, {
          keyword,
          device,
          depth,
          idempotency_key: attempt.current.key,
          confirm_paid: true,
        }),
      );
      attempt.current = undefined;
    } catch (err) {
      setError(apiErrorMessage(err, t("seo.operationError"), t));
    } finally {
      setBuying(false);
    }
  }
  const buyButton = (depth: 20 | 100) => (
    <Popconfirm
      title={t(depth === 20 ? "seo.serpBuyTop20" : "seo.serpBuyTop100")}
      description={t("seo.serpPaidConfirm")}
      okText={t("seo.serpConfirmBuy")}
      cancelText={t("seo.cancel")}
      onConfirm={() => buy(depth)}
      disabled={!configured || buying}
    >
      <Button
        type={depth === 20 ? "primary" : "default"}
        loading={buying}
        disabled={!configured}
      >
        {t(depth === 20 ? "seo.serpBuyTop20" : "seo.serpBuyTop100")}
      </Button>
    </Popconfirm>
  );
  const items = (serp?.items ?? []).filter(
    (item) => showAll || item.type === "organic",
  );
  return (
    <div className={styles.stack}>
      <Text type="secondary">{t("seo.serpHelp")}</Text>
      {error && <Alert type="error" showIcon message={error} />}
      <div className={styles.toolbar}>
        <Select
          aria-label={t("seo.serpDevice")}
          value={device}
          onChange={setDevice}
          options={(["desktop", "mobile"] as const).map((value) => ({
            value,
            label: t(`seo.device_${value}`),
          }))}
        />
        <Checkbox
          checked={showAll}
          onChange={(event) => setShowAll(event.target.checked)}
        >
          {t("seo.serpShowAll")}
        </Checkbox>
        {/* Within 12 hours the server answers a repeat from its saved copy for free. */}
        {!loading && buyButton(20)}
        {!loading && buyButton(100)}
      </div>
      {serp && (
        <Space wrap>
          <Tag>{t(`seo.status_${serp.status}`)}</Tag>
          <Text>
            {t("seo.observed", {
              date: formatServerIsoDateTime(serp.observed_at, timeZone),
            })}
          </Text>
          <Text>{t("seo.serpDepth", { depth: serp.depth })}</Text>
          <Text>
            {t("seo.cost", {
              cost:
                serp.cost_usd == null ? t("seo.missing") : `$${serp.cost_usd}`,
            })}
          </Text>
          {serp.cached && <Tag>{t("seo.cached")}</Tag>}
        </Space>
      )}
      {serp?.error && <Alert type="warning" message={serp.error} />}
      <Table<SerpItem>
        rowKey={(item) => `${item.rank}:${item.type}:${item.url}`}
        loading={loading}
        dataSource={items}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        locale={{
          emptyText: t(serp ? "seo.serpEmpty" : "seo.serpNotBought"),
        }}
        columns={[
          {
            title: t("seo.serpRank"),
            key: "rank",
            width: 70,
            render: (_, item) =>
              (showAll ? item.rank : item.organic_rank) ?? t("seo.missing"),
          },
          ...(showAll
            ? [
                {
                  title: t("seo.serpOrganicRank"),
                  dataIndex: "organic_rank",
                  width: 90,
                  render: (value: number | null) => value ?? "—",
                },
                { title: t("seo.serpType"), dataIndex: "type", width: 140 },
              ]
            : []),
          {
            title: t("seo.serpPage"),
            key: "page",
            render: (_, item) => {
              const url = safeHttpUrl(item.url);
              const label = item.title || item.url || "—";
              return (
                <div>
                  {url ? (
                    <a href={url} target="_blank" rel="noopener noreferrer">
                      {label}
                    </a>
                  ) : (
                    label
                  )}
                  <div>
                    <Text type="secondary">{item.domain}</Text>
                  </div>
                </div>
              );
            },
          },
        ]}
      />
    </div>
  );
}
