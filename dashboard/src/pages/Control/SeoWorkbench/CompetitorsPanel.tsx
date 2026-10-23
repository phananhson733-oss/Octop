import { useEffect, useRef, useState } from "react";
import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Input,
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
  type Lookup,
  type LookupSummary,
  type ProjectContext,
  type RankedKeyword,
  type RankedPage,
  type SerpCompetitor,
} from "../../../api/modules/seo";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import { attemptKey } from "./attemptKey";
import { safeHttpUrl } from "./SerpPanel";
import styles from "./index.module.less";

const { Text } = Typography;
type Kind = LookupSummary["kind"];
type Target =
  | { kind: "domain" | "pages"; domain: string }
  | {
      kind: "competitors";
      keywords: string[];
    };
// DataForSEO serp_competitors takes up to 200 keywords.
const MAX_KEYWORDS = 200;
const SAVED_KEYWORD_PICKS = 20;

const number = (value: number | null | undefined) =>
  value == null ? "—" : Math.round(value).toLocaleString();
const hostOf = (value: string) =>
  value
    .trim()
    .toLowerCase()
    .replace(/^[a-z]+:\/\//, "")
    .replace(/[/?#].*$/, "")
    .replace(/^www\./, "");

/**
 * Saved lookups of one kind, the one shown, and the confirmed purchase. Opening
 * the tab reads saved lookups only; every purchase is a confirmed click.
 */
function useLookups(projectId: string, kind: Kind) {
  const { t } = useTranslation();
  const [history, setHistory] = useState<LookupSummary[]>([]);
  const [current, setCurrent] = useState<Lookup>();
  const [loading, setLoading] = useState(true);
  const [buying, setBuying] = useState(false);
  const [error, setError] = useState("");
  const attempt = useRef<{ input: string; key: string }>();
  useEffect(() => {
    let active = true;
    setLoading(true);
    seoApi
      .lookups(projectId, kind)
      .then(async (list) => {
        // A failed or empty lookup must not hide an earlier good one.
        const pick =
          list.find((item) => item.status === "completed" && item.item_count) ??
          list[0];
        const full = pick
          ? await seoApi.lookup(projectId, pick.lookup_id)
          : undefined;
        if (active) {
          setHistory(list);
          setCurrent(full);
        }
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
  }, [projectId, kind, t]);
  async function open(lookupId: string) {
    setLoading(true);
    setError("");
    try {
      setCurrent(await seoApi.lookup(projectId, lookupId));
    } catch (err) {
      setError(apiErrorMessage(err, t("seo.loadError"), t));
    } finally {
      setLoading(false);
    }
  }
  async function buy(target: Target) {
    const input = JSON.stringify(target);
    // An unchanged retry after an uncertain failure reuses the same key.
    if (attempt.current?.input !== input)
      attempt.current = { input, key: attemptKey() };
    setBuying(true);
    setError("");
    try {
      const bought = await seoApi.buyLookup(projectId, {
        ...target,
        idempotency_key: attempt.current.key,
        confirm_paid: true,
      });
      setCurrent(bought);
      setHistory((list) => [
        bought,
        ...list.filter((item) => item.lookup_id !== bought.lookup_id),
      ]);
      attempt.current = undefined;
    } catch (err) {
      setError(apiErrorMessage(err, t("seo.operationError"), t));
    } finally {
      setBuying(false);
    }
  }
  return { history, current, loading, buying, error, open, buy };
}

function LookupMeta({ lookup }: { lookup: Lookup }) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  return (
    <>
      <Space wrap>
        <Tag>{t(`seo.status_${lookup.status}`)}</Tag>
        <Text>
          {t("seo.observed", {
            date: formatServerIsoDateTime(lookup.observed_at, timeZone),
          })}
        </Text>
        <Text>
          {t("seo.cost", {
            cost:
              lookup.cost_usd == null
                ? t("seo.missing")
                : `$${lookup.cost_usd}`,
          })}
        </Text>
        {lookup.cached && <Tag>{t("seo.cached")}</Tag>}
      </Space>
      {lookup.error && <Alert type="warning" message={lookup.error} />}
    </>
  );
}

function HistorySelect({
  history,
  current,
  onOpen,
}: {
  history: LookupSummary[];
  current?: Lookup;
  onOpen: (lookupId: string) => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  if (history.length < 2) return null;
  return (
    <Select
      aria-label={t("seo.lookupHistory")}
      className={styles.projectSelect}
      value={current?.lookup_id}
      onChange={onOpen}
      options={history.map((item) => ({
        value: item.lookup_id,
        label: `${
          item.domain ??
          t("seo.lookupKeywordCount", { count: item.keywords?.length ?? 0 })
        } · ${formatServerIsoDateTime(item.observed_at, timeZone)}`,
      }))}
    />
  );
}

function BuyButton({
  label,
  disabled,
  buying,
  onConfirm,
}: {
  label: string;
  disabled: boolean;
  buying: boolean;
  onConfirm: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Popconfirm
      title={label}
      description={t("seo.lookupPaidConfirm")}
      okText={t("seo.serpConfirmBuy")}
      cancelText={t("seo.cancel")}
      onConfirm={onConfirm}
      disabled={disabled || buying}
    >
      <Button type="primary" loading={buying} disabled={disabled}>
        {label}
      </Button>
    </Popconfirm>
  );
}

function DomainCard({
  projectId,
  domain,
  competitors,
  configured,
}: {
  projectId: string;
  domain: string;
  competitors: string[];
  configured: boolean;
}) {
  const { t } = useTranslation();
  const lookups = useLookups(projectId, "domain");
  const [target, setTarget] = useState("");
  const current = lookups.current;
  const rows = (
    current?.kind === "domain" ? current.items : []
  ) as RankedKeyword[];
  const overview = current?.overview;
  const top3 =
    overview?.pos_1 == null || overview.pos_2_3 == null
      ? null
      : overview.pos_1 + overview.pos_2_3;
  const top10 =
    top3 == null || overview?.pos_4_10 == null
      ? null
      : top3 + overview.pos_4_10;
  return (
    <Card size="small" title={t("seo.lookupDomainTitle")}>
      <div className={styles.stack}>
        <Text type="secondary">{t("seo.lookupDomainHelp")}</Text>
        {lookups.error && (
          <Alert type="error" showIcon message={lookups.error} />
        )}
        <div className={styles.toolbar}>
          <AutoComplete
            aria-label={t("seo.lookupDomain")}
            className={styles.projectSelect}
            value={target}
            onChange={setTarget}
            placeholder={t("seo.lookupDomainPlaceholder")}
            options={[...new Set([domain, ...competitors])].map((value) => ({
              value,
            }))}
          />
          <BuyButton
            label={t("seo.lookupDomainBuy")}
            disabled={!configured || !hostOf(target)}
            buying={lookups.buying}
            onConfirm={() =>
              lookups.buy({ kind: "domain", domain: hostOf(target) })
            }
          />
          <HistorySelect
            history={lookups.history}
            current={current}
            onOpen={lookups.open}
          />
        </div>
        {current && (
          <>
            <Text strong>{current.domain}</Text>
            <LookupMeta lookup={current} />
            <div className={styles.metrics}>
              {(
                [
                  ["seo.lookupOrganicTraffic", overview?.organic_traffic],
                  ["seo.lookupOrganicKeywords", overview?.organic_keywords],
                  ["seo.lookupTop3", top3],
                  ["seo.lookupTop10", top10],
                ] as const
              ).map(([label, value]) => (
                <Card key={label} size="small">
                  <Text type="secondary">{t(label)}</Text>
                  <div className={styles.metricValue}>{number(value)}</div>
                </Card>
              ))}
            </div>
            <Text type="secondary">{t("seo.lookupEstimates")}</Text>
          </>
        )}
        <Table<RankedKeyword>
          rowKey={(row) => `${row.keyword}:${row.url}`}
          loading={lookups.loading}
          dataSource={rows}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          locale={{
            emptyText: t(current ? "seo.lookupEmpty" : "seo.lookupNotBought"),
          }}
          columns={[
            { title: t("seo.keyword"), dataIndex: "keyword" },
            {
              title: t("seo.serpOrganicRank"),
              dataIndex: "organic_rank",
              width: 90,
              sorter: (a, b) =>
                (a.organic_rank ?? 999) - (b.organic_rank ?? 999),
              render: (value: number | null) => value ?? "—",
            },
            {
              title: t("seo.volume"),
              dataIndex: "search_volume",
              width: 100,
              sorter: (a, b) =>
                (a.search_volume ?? -1) - (b.search_volume ?? -1),
              render: number,
            },
            {
              title: t("seo.lookupTraffic"),
              dataIndex: "etv",
              width: 100,
              defaultSortOrder: "descend",
              sorter: (a, b) => (a.etv ?? -1) - (b.etv ?? -1),
              render: number,
            },
            {
              title: t("seo.difficulty"),
              dataIndex: "keyword_difficulty",
              width: 80,
              render: (value: number | null) => value ?? "—",
            },
            {
              title: t("seo.serpPage"),
              dataIndex: "url",
              render: (value: string | null) => {
                const url = safeHttpUrl(value);
                return url ? (
                  <a href={url} target="_blank" rel="noopener noreferrer">
                    {new URL(url).pathname}
                  </a>
                ) : (
                  "—"
                );
              },
            },
          ]}
        />
        {current?.total_count != null && (
          <Text type="secondary">
            {t("seo.lookupTopOf", {
              shown: rows.length,
              total: current.total_count.toLocaleString(),
            })}
          </Text>
        )}
      </div>
    </Card>
  );
}

function PagesCard({
  projectId,
  domain,
  competitors,
  configured,
}: {
  projectId: string;
  domain: string;
  competitors: string[];
  configured: boolean;
}) {
  const { t } = useTranslation();
  const lookups = useLookups(projectId, "pages");
  const [target, setTarget] = useState("");
  const current = lookups.current;
  const rows = (current?.kind === "pages" ? current.items : []) as RankedPage[];
  const sum = (...values: (number | null)[]) =>
    values.some((value) => value == null)
      ? null
      : values.reduce<number>((total, value) => total + (value ?? 0), 0);
  return (
    <Card size="small" title={t("seo.lookupPagesTitle")}>
      <div className={styles.stack}>
        <Text type="secondary">{t("seo.lookupPagesHelp")}</Text>
        {lookups.error && (
          <Alert type="error" showIcon message={lookups.error} />
        )}
        <div className={styles.toolbar}>
          <AutoComplete
            aria-label={t("seo.lookupDomain")}
            className={styles.projectSelect}
            value={target}
            onChange={setTarget}
            placeholder={t("seo.lookupDomainPlaceholder")}
            options={[...new Set([domain, ...competitors])].map((value) => ({
              value,
            }))}
          />
          <BuyButton
            label={t("seo.lookupPagesBuy")}
            disabled={!configured || !hostOf(target)}
            buying={lookups.buying}
            onConfirm={() =>
              lookups.buy({ kind: "pages", domain: hostOf(target) })
            }
          />
          <HistorySelect
            history={lookups.history}
            current={current}
            onOpen={lookups.open}
          />
        </div>
        {current && (
          <>
            <Text strong>{current.domain}</Text>
            <LookupMeta lookup={current} />
          </>
        )}
        <Table<RankedPage>
          rowKey="url"
          loading={lookups.loading}
          dataSource={rows}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          locale={{
            emptyText: t(current ? "seo.lookupEmpty" : "seo.lookupNotBought"),
          }}
          columns={[
            {
              title: t("seo.serpPage"),
              dataIndex: "url",
              render: (value: string) => {
                const url = safeHttpUrl(value);
                return url ? (
                  <a
                    href={url}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={url}
                  >
                    {new URL(url).pathname}
                  </a>
                ) : (
                  value
                );
              },
            },
            {
              title: t("seo.lookupTraffic"),
              dataIndex: "etv",
              width: 100,
              defaultSortOrder: "descend",
              sorter: (a, b) => (a.etv ?? -1) - (b.etv ?? -1),
              render: number,
            },
            {
              title: t("seo.lookupOrganicKeywords"),
              dataIndex: "keywords",
              width: 110,
              sorter: (a, b) => (a.keywords ?? -1) - (b.keywords ?? -1),
              render: number,
            },
            {
              key: "top3",
              title: t("seo.lookupTop3"),
              width: 110,
              render: (_, row) => number(sum(row.pos_1, row.pos_2_3)),
            },
            {
              key: "top10",
              title: t("seo.lookupTop10"),
              width: 110,
              render: (_, row) =>
                number(sum(row.pos_1, row.pos_2_3, row.pos_4_10)),
            },
          ]}
        />
        {current && <Text type="secondary">{t("seo.lookupEstimates")}</Text>}
        {current?.total_count != null && (
          <Text type="secondary">
            {t("seo.lookupPagesTopOf", {
              shown: rows.length,
              total: current.total_count.toLocaleString(),
            })}
          </Text>
        )}
      </div>
    </Card>
  );
}

function SerpCompetitorsCard({
  projectId,
  domain,
  competitors,
  configured,
  onContext,
}: {
  projectId: string;
  domain: string;
  competitors: string[];
  configured: boolean;
  onContext: (context: ProjectContext) => void;
}) {
  const { t } = useTranslation();
  const lookups = useLookups(projectId, "competitors");
  const [text, setText] = useState("");
  const [adding, setAdding] = useState("");
  const [error, setError] = useState("");
  const keywords = [
    ...new Set(
      text
        .split("\n")
        .map((line) => line.trim().toLowerCase())
        .filter(Boolean),
    ),
  ];
  const current = lookups.current;
  const rows = (
    current?.kind === "competitors" ? current.items : []
  ) as SerpCompetitor[];
  const known = new Set(competitors);
  const own = hostOf(domain);
  async function fillSaved() {
    setError("");
    try {
      const { rows: saved } = await seoApi.keywords(projectId);
      const picks = [...saved]
        .sort((a, b) => (b.search_volume ?? -1) - (a.search_volume ?? -1))
        .slice(0, SAVED_KEYWORD_PICKS)
        .map((row) => row.keyword);
      if (picks.length) setText(picks.join("\n"));
      else setError(t("seo.lookupNoSaved"));
    } catch (err) {
      setError(apiErrorMessage(err, t("seo.loadError"), t));
    }
  }
  async function addCompetitor(competitor: string) {
    setAdding(competitor);
    setError("");
    try {
      onContext(
        await seoApi.updateContext(projectId, [
          { add_competitors: [{ domain: competitor }] },
        ]),
      );
    } catch (err) {
      setError(apiErrorMessage(err, t("seo.operationError"), t));
    } finally {
      setAdding("");
    }
  }
  const message = lookups.error || error;
  return (
    <Card size="small" title={t("seo.lookupCompetitorsTitle")}>
      <div className={styles.stack}>
        <Text type="secondary">{t("seo.lookupCompetitorsHelp")}</Text>
        {message && <Alert type="error" showIcon message={message} />}
        <Input.TextArea
          aria-label={t("seo.lookupKeywords")}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={t("seo.lookupKeywordsPlaceholder")}
          autoSize={{ minRows: 3, maxRows: 8 }}
        />
        <div className={styles.toolbar}>
          <Button onClick={fillSaved}>{t("seo.lookupUseSaved")}</Button>
          <BuyButton
            label={t("seo.lookupCompetitorsBuy", { count: keywords.length })}
            disabled={
              !configured || !keywords.length || keywords.length > MAX_KEYWORDS
            }
            buying={lookups.buying}
            onConfirm={() => lookups.buy({ kind: "competitors", keywords })}
          />
          {keywords.length > MAX_KEYWORDS && (
            <Text type="danger">
              {t("seo.lookupTooMany", { max: MAX_KEYWORDS })}
            </Text>
          )}
          <HistorySelect
            history={lookups.history}
            current={current}
            onOpen={lookups.open}
          />
        </div>
        {current && (
          <>
            <Text type="secondary">
              {t("seo.lookupKeywordsUsed", {
                keywords: (current.keywords ?? []).join(", "),
              })}
            </Text>
            <LookupMeta lookup={current} />
          </>
        )}
        <Table<SerpCompetitor>
          rowKey="domain"
          loading={lookups.loading}
          dataSource={rows}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          locale={{
            emptyText: t(current ? "seo.lookupEmpty" : "seo.lookupNotBought"),
          }}
          columns={[
            {
              title: t("seo.lookupDomain"),
              dataIndex: "domain",
              render: (value: string) => (
                <Space wrap>
                  <Text>{value}</Text>
                  {hostOf(value) === own && <Tag>{t("seo.lookupOwn")}</Tag>}
                  {known.has(hostOf(value)) && (
                    <Tag color="blue">{t("seo.lookupSaved")}</Tag>
                  )}
                </Space>
              ),
            },
            {
              title: t("seo.lookupVisibility"),
              dataIndex: "visibility",
              width: 100,
              render: (value: number | null) =>
                value == null ? "—" : value.toFixed(3),
            },
            {
              title: t("seo.lookupAvgPosition"),
              dataIndex: "avg_position",
              width: 100,
              render: (value: number | null) =>
                value == null ? "—" : value.toFixed(1),
            },
            {
              title: t("seo.lookupKeywordsRanked"),
              dataIndex: "keywords_count",
              width: 110,
              render: (value: number | null) =>
                value == null
                  ? "—"
                  : `${value} / ${current?.keywords?.length ?? "?"}`,
            },
            {
              title: t("seo.lookupTraffic"),
              dataIndex: "etv",
              width: 100,
              render: number,
            },
            {
              key: "add",
              width: 120,
              render: (_, row) => {
                const host = hostOf(row.domain);
                if (host === own || known.has(host)) return null;
                return (
                  <Button
                    size="small"
                    loading={adding === host}
                    onClick={() => addCompetitor(host)}
                  >
                    {t("seo.lookupAddCompetitor")}
                  </Button>
                );
              },
            },
          ]}
        />
      </div>
    </Card>
  );
}

/**
 * Competitor research (OpenSEO Domain overview and find_serp_competitors):
 * one domain's organic footprint, its top pages by traffic, and who ranks for a
 * keyword set. Bought from DataForSEO Labs on a confirmed click.
 */
export function CompetitorsPanel({
  projectId,
  domain,
  configured,
}: {
  projectId: string;
  domain: string;
  configured: boolean;
}) {
  const { t } = useTranslation();
  const [context, setContext] = useState<ProjectContext>();
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    seoApi
      .context(projectId)
      .then((result) => {
        if (active) setContext(result);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [projectId, t]);
  const competitors = (context?.competitors ?? []).map((row) => row.domain);
  return (
    <div className={styles.stack}>
      {!configured && (
        <Alert type="info" showIcon message={t("seo.lookupNeedsProvider")} />
      )}
      {error && <Alert type="error" showIcon message={error} />}
      <DomainCard
        projectId={projectId}
        domain={domain}
        competitors={competitors}
        configured={configured}
      />
      <PagesCard
        projectId={projectId}
        domain={domain}
        competitors={competitors}
        configured={configured}
      />
      <SerpCompetitorsCard
        projectId={projectId}
        domain={domain}
        competitors={competitors}
        configured={configured}
        onContext={setContext}
      />
    </div>
  );
}
