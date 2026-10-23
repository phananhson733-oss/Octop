import { useState } from "react";
import {
  Alert,
  Card,
  Checkbox,
  Input,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
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
  type BrandLookup,
  type BrandLookupSummary,
  type BrandPage,
  type BrandPlatform,
  type BrandQuery,
  type LlmModel,
  type PromptAnswer,
  type PromptCitation,
  type PromptLookup,
  type PromptLookupSummary,
  type PromptRequest,
} from "../../../api/modules/seo";
import {
  BuyButton,
  HistorySelect,
  LookupMeta,
  useLookups,
  type LookupApi,
} from "./CompetitorsPanel";
import { safeHttpUrl } from "./SerpPanel";
import { brandEstimateUsd, promptEstimate } from "../../../utils/seoCosts";
import styles from "./index.module.less";

/**
 * AI search (OpenSEO Prompt Explorer and Brand Lookup): one prompt asked of up
 * to four models side by side, and where a brand shows up in AI answers. Each
 * is bought on a confirmed click and kept as a lookup.
 */

const { Text, Paragraph } = Typography;
const MODELS: LlmModel[] = ["chat_gpt", "claude", "gemini", "perplexity"];
// The countries Claude documents; ChatGPT and Perplexity take them all, Gemini none.
const COUNTRIES =
  "AR AT AU BE BR CA CH CL CN DE DK ES FI FR GB HK ID IN IT JP KR MX MY NL NO NZ PH PL PT RU SA SE TR TW US ZA".split(
    " ",
  );
const MAX_PROMPT = 500;

type PromptTarget = Omit<PromptRequest, "country"> & { country?: string };

// Lookup ids come from the same kind's list, so the read is of that kind.
const promptLookups: LookupApi<
  "prompt",
  PromptLookupSummary,
  PromptLookup,
  PromptTarget
> = {
  list: (projectId, kind) => seoApi.aiLookups(projectId, kind),
  get: (projectId, lookupId) =>
    seoApi.aiLookup<PromptLookup>(projectId, lookupId),
  buy: (projectId, data) => seoApi.buyPromptLookup(projectId, data),
};

const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** promptExplorer.ts matchesBrand / computeBrandMentioned, applied on display. */
function brandMatcher(brand: string) {
  const needle = brand.trim();
  if (!needle) return null;
  // \b only works next to word characters ("C++", "AT&T" end in others).
  const lead = /^\w/.test(needle) ? "\\b" : `(?<!${escape(needle[0])})`;
  const trail = /\w$/.test(needle)
    ? "\\b"
    : `(?!${escape(needle[needle.length - 1])})`;
  const mention = new RegExp(`${lead}${escape(needle)}${trail}`, "i");
  const lower = needle.toLowerCase();
  const cited = (citation: PromptCitation) =>
    `${citation.url} ${citation.title ?? ""}`.toLowerCase().includes(lower);
  return {
    cited,
    mentioned: (row: PromptAnswer) =>
      row.citations.some(cited) || mention.test(row.text),
  };
}

function AnswerCard({
  row,
  brand,
}: {
  row: PromptAnswer;
  brand: ReturnType<typeof brandMatcher>;
}) {
  const { t } = useTranslation();
  const refused =
    row.error === "unsupported_country" || row.error === "unknown_model";
  return (
    <Card
      size="small"
      title={
        <Space wrap size={4}>
          <Text strong>{t(`seo.llm_${row.model}`)}</Text>
          {row.model_name && <Tag>{row.model_name}</Tag>}
        </Space>
      }
      extra={row.cost_usd == null ? null : <Text>${row.cost_usd}</Text>}
    >
      <div className={styles.stack}>
        {row.status !== "completed" && (
          <Alert
            type="warning"
            showIcon
            message={
              refused
                ? t(`seo.promptError_${row.error}`)
                : row.status === "unknown"
                ? t("seo.promptUnknown")
                : t("seo.promptFailed", { error: row.error })
            }
          />
        )}
        {row.status === "completed" && (
          <Space wrap size={4}>
            <Tag>
              {row.web_search
                ? t("seo.promptSearched")
                : t("seo.promptNotSearched")}
            </Tag>
            {brand && (
              <Tag color={brand.mentioned(row) ? "green" : undefined}>
                {brand.mentioned(row)
                  ? t("seo.promptMentioned")
                  : t("seo.promptNotMentioned")}
              </Tag>
            )}
          </Space>
        )}
        {row.status === "completed" &&
          (row.text ? (
            // Model output: plain text only, never rendered as markdown or HTML.
            <Paragraph className={`${styles.preWrap} ${styles.answerText}`}>
              {row.text}
            </Paragraph>
          ) : (
            <Text type="secondary">{t("seo.promptNoText")}</Text>
          ))}
        {row.citations.length > 0 && (
          <>
            <Text strong>
              {t("seo.promptCitations", { count: row.citations.length })}
            </Text>
            <ol>
              {row.citations.map((citation) => {
                const url = safeHttpUrl(citation.url);
                const label = citation.title || citation.domain || citation.url;
                return (
                  <li key={citation.url}>
                    <Space wrap size={4}>
                      {url ? (
                        <a
                          href={url}
                          target="_blank"
                          rel="noopener noreferrer"
                          title={url}
                        >
                          {label}
                        </a>
                      ) : (
                        <Text>{label}</Text>
                      )}
                      {brand?.cited(citation) && (
                        <Tag color="green">{t("seo.promptCited")}</Tag>
                      )}
                    </Space>
                  </li>
                );
              })}
            </ol>
          </>
        )}
        {row.fan_out_queries.length > 0 && (
          <>
            <Text strong>{t("seo.promptFanOut")}</Text>
            <Space wrap size={4}>
              {row.fan_out_queries.map((query) => (
                <Tag key={query}>{query}</Tag>
              ))}
            </Space>
          </>
        )}
      </div>
    </Card>
  );
}

export function PromptExplorerCard({
  projectId,
  domain,
  configured,
}: {
  projectId: string;
  domain: string;
  configured: boolean;
}) {
  const { t, i18n } = useTranslation();
  const lookups = useLookups(projectId, "prompt", promptLookups);
  const [prompt, setPrompt] = useState("");
  const [models, setModels] = useState<LlmModel[]>(["chat_gpt"]);
  const [webSearch, setWebSearch] = useState(true);
  const [country, setCountry] = useState("");
  const [brand, setBrand] = useState(
    () => domain.replace(/^www\./, "").split(".")[0] ?? "",
  );
  const current = lookups.current;
  const regions = new Intl.DisplayNames([i18n?.language ?? "en"], {
    type: "region",
  });
  const searchCountry = webSearch ? country : "";
  const { charged, usd: estimate } = promptEstimate(models, webSearch, country);
  const matcher = brandMatcher(brand);
  return (
    <Card size="small" title={t("seo.promptTitle")}>
      <div className={styles.stack}>
        <Text type="secondary">{t("seo.promptHelp")}</Text>
        {lookups.error && (
          <Alert type="error" showIcon message={lookups.error} />
        )}
        <Input.TextArea
          aria-label={t("seo.promptLabel")}
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder={t("seo.promptPlaceholder")}
          maxLength={MAX_PROMPT}
          showCount
          autoSize={{ minRows: 2, maxRows: 6 }}
        />
        <div className={styles.toolbar}>
          <Checkbox.Group
            aria-label={t("seo.promptModels")}
            value={models}
            onChange={(values) => setModels(values as LlmModel[])}
            options={MODELS.map((model) => ({
              value: model,
              label: t(`seo.llm_${model}`),
            }))}
          />
          <Checkbox
            checked={webSearch}
            onChange={(event) => setWebSearch(event.target.checked)}
          >
            {t("seo.promptWebSearch")}
          </Checkbox>
          <Select
            aria-label={t("seo.promptCountry")}
            className={styles.countrySelect}
            value={country}
            onChange={setCountry}
            disabled={!webSearch}
            showSearch
            optionFilterProp="label"
            options={[
              { value: "", label: t("seo.promptCountryAny") },
              ...COUNTRIES.map((code) => ({
                value: code,
                label: `${regions.of(code) ?? code} (${code})`,
              })),
            ]}
          />
          <Input
            aria-label={t("seo.promptBrand")}
            className={styles.filterInput}
            value={brand}
            onChange={(event) => setBrand(event.target.value)}
            placeholder={t("seo.promptBrandPlaceholder")}
          />
        </div>
        {searchCountry && models.includes("gemini") && (
          <Text type="secondary">{t("seo.promptCountryHint")}</Text>
        )}
        <div className={styles.toolbar}>
          <BuyButton
            label={t("seo.promptBuy")}
            description={t("seo.promptPaidConfirm", {
              count: charged.length,
              cost: estimate,
            })}
            disabled={!configured || !prompt.trim() || !models.length}
            buying={lookups.buying}
            onConfirm={() =>
              lookups.buy({
                prompt: prompt.trim(),
                models,
                web_search: webSearch,
                ...(searchCountry ? { country: searchCountry } : {}),
              })
            }
          />
          <HistorySelect
            history={lookups.history}
            current={current}
            onOpen={lookups.open}
          />
        </div>
        {!current && !lookups.loading && (
          <Text type="secondary">{t("seo.lookupNotBought")}</Text>
        )}
        {current && (
          <>
            <Text strong className={styles.preWrap}>
              {current.request.prompt}
            </Text>
            <LookupMeta lookup={current} />
            <div className={styles.answers}>
              {current.items.map((row) => (
                <AnswerCard key={row.model} row={row} brand={matcher} />
              ))}
            </div>
            <Text type="secondary">{t("seo.aiAnswerPlain")}</Text>
          </>
        )}
      </div>
    </Card>
  );
}

const MAX_COMPETITORS = 5;
const amount = (value: number | null | undefined) =>
  value == null ? "—" : Math.round(value).toLocaleString();

type BrandTarget = { target: string; competitors: string[] };

const brandLookups: LookupApi<
  "brand",
  BrandLookupSummary,
  BrandLookup,
  BrandTarget
> = {
  list: (projectId, kind) => seoApi.aiLookups(projectId, kind),
  get: (projectId, lookupId) =>
    seoApi.aiLookup<BrandLookup>(projectId, lookupId),
  buy: (projectId, data) => seoApi.buyBrandLookup(projectId, data),
};

function SafeLink({ url, label }: { url: string; label?: string | null }) {
  const safe = safeHttpUrl(url);
  if (!safe) return <Text>{label || url}</Text>;
  return (
    <a href={safe} target="_blank" rel="noopener noreferrer" title={safe}>
      {label || safe}
    </a>
  );
}

export function BrandLookupCard({
  projectId,
  domain,
  configured,
}: {
  projectId: string;
  domain: string;
  configured: boolean;
}) {
  const { t } = useTranslation();
  const lookups = useLookups(projectId, "brand", brandLookups);
  const [target, setTarget] = useState(domain);
  const [competitors, setCompetitors] = useState<string[]>([]);
  const current = lookups.current;
  const overview = current?.overview ?? null;
  const rows = current?.items ?? [];
  const pages = rows.filter((row): row is BrandPage => row.section === "page");
  const queries = rows.filter(
    (row): row is BrandQuery => row.section === "query",
  );
  const platform = (value: BrandPlatform) => t(`seo.brandPlatform_${value}`);
  const platformTag = (value: BrandPlatform) => <Tag>{platform(value)}</Tag>;
  const table = { pagination: { pageSize: 20, showSizeChanger: false } };
  const share = overview?.share_of_voice;
  return (
    <Card size="small" title={t("seo.brandTitle")}>
      <div className={styles.stack}>
        <Text type="secondary">{t("seo.brandHelp")}</Text>
        {lookups.error && (
          <Alert type="error" showIcon message={lookups.error} />
        )}
        <div className={styles.toolbar}>
          <Input
            aria-label={t("seo.brandTarget")}
            className={styles.projectSelect}
            value={target}
            onChange={(event) => setTarget(event.target.value)}
            placeholder={t("seo.brandTargetPlaceholder")}
            maxLength={250}
          />
          <Select
            aria-label={t("seo.brandCompetitors")}
            className={styles.projectSelect}
            mode="tags"
            suffixIcon={null}
            notFoundContent={null}
            maxCount={MAX_COMPETITORS}
            tokenSeparators={[",", "，"]}
            value={competitors}
            onChange={(values: string[]) =>
              setCompetitors(
                values.map((value) => value.trim()).filter(Boolean),
              )
            }
            placeholder={t("seo.brandCompetitorsPlaceholder")}
          />
          <BuyButton
            label={t("seo.brandBuy")}
            description={t("seo.brandPaidConfirm", {
              cost: brandEstimateUsd(competitors.length),
            })}
            disabled={!configured || !target.trim()}
            buying={lookups.buying}
            onConfirm={() =>
              lookups.buy({ target: target.trim(), competitors })
            }
          />
          <HistorySelect
            history={lookups.history}
            current={current}
            onOpen={lookups.open}
          />
        </div>
        {!current && !lookups.loading && (
          <Text type="secondary">{t("seo.lookupNotBought")}</Text>
        )}
        {current && (
          <>
            <Space wrap size={4}>
              <Text strong>{current.request.target}</Text>
              {current.request.competitors.map((name) => (
                <Tag key={name}>{name}</Tag>
              ))}
            </Space>
            <LookupMeta lookup={current} />
            {overview && (
              <>
                <div className={styles.metrics}>
                  <Card size="small">
                    <Text type="secondary">{t("seo.brandMentions")}</Text>
                    <div className={styles.metricValue}>
                      {amount(overview.total_mentions)}
                    </div>
                  </Card>
                  <Card size="small">
                    <Text type="secondary">{t("seo.brandVolume")}</Text>
                    <div className={styles.metricValue}>
                      {amount(overview.total_ai_search_volume)}
                    </div>
                  </Card>
                  {overview.platforms.map((row) => (
                    <Card key={row.platform} size="small">
                      <Text type="secondary">{platform(row.platform)}</Text>
                      <div className={styles.metricValue}>
                        {row.status === "failed"
                          ? t("seo.brandPlatformFailed")
                          : t("seo.brandPlatformMetric", {
                              mentions: amount(row.mentions),
                              volume: amount(row.ai_search_volume),
                            })}
                      </div>
                    </Card>
                  ))}
                </div>
                {!overview.chatgpt_counted && (
                  <Text type="secondary">{t("seo.brandChatgptApart")}</Text>
                )}
                {overview.monthly_volume.length > 1 && (
                  <>
                    <Text strong>{t("seo.brandTrend")}</Text>
                    <div className={styles.chart}>
                      <ResponsiveContainer
                        width="100%"
                        height="100%"
                        minWidth={0}
                      >
                        <LineChart
                          data={overview.monthly_volume.map((row) => ({
                            month: `${row.year}-${String(row.month).padStart(
                              2,
                              "0",
                            )}`,
                            volume: row.volume,
                          }))}
                        >
                          <CartesianGrid
                            strokeDasharray="3 3"
                            stroke="var(--fn-border-secondary)"
                          />
                          <XAxis dataKey="month" />
                          <YAxis />
                          <RechartsTooltip />
                          <Line
                            dataKey="volume"
                            name={t("seo.brandVolume")}
                            stroke="#1677ff"
                            dot={false}
                          />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  </>
                )}
                {share && (
                  <>
                    <Text strong>
                      {t("seo.brandShare", {
                        platforms: share.platforms.map(platform).join(" + "),
                      })}
                    </Text>
                    <Table
                      size="small"
                      pagination={false}
                      rowKey="label"
                      dataSource={share.entries}
                      columns={[
                        {
                          title: t("seo.brandName"),
                          dataIndex: "label",
                          render: (value: string, row) =>
                            row.is_target ? <Text strong>{value}</Text> : value,
                        },
                        {
                          title: t("seo.brandMentions"),
                          dataIndex: "mentions",
                          width: 120,
                          render: amount,
                        },
                        {
                          title: t("seo.brandSharePct"),
                          dataIndex: "share_pct",
                          width: 120,
                          render: (value: number | null) =>
                            value == null ? "—" : `${value.toFixed(1)}%`,
                        },
                      ]}
                    />
                  </>
                )}
              </>
            )}
            <Tabs
              items={[
                {
                  key: "pages",
                  label: t("seo.brandPagesTab", { count: pages.length }),
                  children: (
                    <Table<BrandPage>
                      {...table}
                      rowKey={(row) => `${row.platform}>${row.url}`}
                      dataSource={pages}
                      locale={{ emptyText: t("seo.lookupEmpty") }}
                      expandable={{
                        rowExpandable: (row) => row.keywords.length > 0,
                        expandedRowRender: (row) => (
                          <Space direction="vertical" size={2}>
                            <Text type="secondary">
                              {t("seo.brandPagePrompts")}
                            </Text>
                            {row.keywords.map((keyword) => (
                              <Text key={keyword.question}>
                                {keyword.question} ·{" "}
                                {amount(keyword.ai_search_volume)}
                              </Text>
                            ))}
                          </Space>
                        ),
                      }}
                      columns={[
                        {
                          title: t("seo.brandPage"),
                          dataIndex: "url",
                          render: (value: string) => <SafeLink url={value} />,
                        },
                        {
                          title: t("seo.brandPlatform"),
                          dataIndex: "platform",
                          width: 150,
                          render: platformTag,
                        },
                        {
                          title: t("seo.brandMentions"),
                          dataIndex: "mentions",
                          width: 100,
                          render: amount,
                        },
                        {
                          title: t("seo.brandCapturedVolume"),
                          dataIndex: "captured_volume",
                          width: 140,
                          render: amount,
                        },
                      ]}
                    />
                  ),
                },
                {
                  key: "queries",
                  label: t("seo.brandQueriesTab", { count: queries.length }),
                  children: (
                    <Table<BrandQuery>
                      {...table}
                      rowKey={(row) => `${row.platform}>${row.question}`}
                      dataSource={queries}
                      locale={{ emptyText: t("seo.lookupEmpty") }}
                      expandable={{
                        rowExpandable: (row) =>
                          row.cited_sources.length > 0 ||
                          row.brands_mentioned.length > 0,
                        expandedRowRender: (row) => (
                          <Space direction="vertical" size={4}>
                            {row.cited_sources.length > 0 && (
                              <>
                                <Text type="secondary">
                                  {t("seo.promptCitations", {
                                    count: row.cited_sources.length,
                                  })}
                                </Text>
                                {row.cited_sources.map((source) => (
                                  <SafeLink
                                    key={source.url}
                                    url={source.url}
                                    label={source.title || source.domain}
                                  />
                                ))}
                              </>
                            )}
                            {row.brands_mentioned.length > 0 && (
                              <>
                                <Text type="secondary">
                                  {t("seo.brandMentioned")}
                                </Text>
                                <Space wrap size={4}>
                                  {row.brands_mentioned.map((name) => (
                                    <Tag key={name}>{name}</Tag>
                                  ))}
                                </Space>
                              </>
                            )}
                          </Space>
                        ),
                      }}
                      columns={[
                        {
                          title: t("seo.brandQuestion"),
                          dataIndex: "question",
                        },
                        {
                          title: t("seo.brandPlatform"),
                          dataIndex: "platform",
                          width: 150,
                          render: platformTag,
                        },
                        {
                          title: t("seo.brandVolume"),
                          dataIndex: "ai_search_volume",
                          width: 120,
                          render: amount,
                        },
                        {
                          title: t("seo.brandLastSeen"),
                          dataIndex: "last_seen_at",
                          width: 110,
                          render: (value: string | null) =>
                            value ? value.slice(0, 10) : "—",
                        },
                      ]}
                    />
                  ),
                },
              ]}
            />
            <Text type="secondary">{t("seo.brandEstimates")}</Text>
          </>
        )}
      </div>
    </Card>
  );
}
