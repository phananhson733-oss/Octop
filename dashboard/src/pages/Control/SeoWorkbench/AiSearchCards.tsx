import { useState } from "react";
import {
  Alert,
  Card,
  Checkbox,
  Input,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  seoApi,
  type AiLookup,
  type AiLookupSummary,
  type LlmModel,
  type PromptAnswer,
  type PromptCitation,
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
import styles from "./index.module.less";

/**
 * AI search (OpenSEO Prompt Explorer): one prompt asked of up to four models
 * side by side, bought on a confirmed click and kept as a lookup.
 */

const { Text, Paragraph } = Typography;
const MODELS: LlmModel[] = ["chat_gpt", "claude", "gemini", "perplexity"];
// OpenSEO's per-answer reserves (pricing.ts): with web search, then without.
const RESERVE_USD: Record<LlmModel, [number, number]> = {
  chat_gpt: [0.09, 0.05],
  claude: [0.18, 0.08],
  gemini: [0.11, 0.05],
  perplexity: [0.04, 0.04],
};
// The countries Claude documents; ChatGPT and Perplexity take them all, Gemini none.
const COUNTRIES =
  "AR AT AU BE BR CA CH CL CN DE DK ES FI FR GB HK ID IN IT JP KR MX MY NL NO NZ PH PL PT RU SA SE TR TW US ZA".split(
    " ",
  );
const MAX_PROMPT = 500;

type PromptTarget = Omit<PromptRequest, "country"> & { country?: string };

const promptLookups: LookupApi<
  "prompt",
  AiLookupSummary,
  AiLookup,
  PromptTarget
> = {
  list: (projectId, kind) => seoApi.aiLookups(projectId, kind),
  get: (projectId, lookupId) => seoApi.aiLookup(projectId, lookupId),
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
  // Gemini takes no country: it is skipped unsent, at no charge.
  const charged = models.filter(
    (model) => !(searchCountry && model === "gemini"),
  );
  const estimate = charged.reduce(
    (sum, model) => sum + RESERVE_USD[model][webSearch ? 0 : 1],
    0,
  );
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
              cost: Number(estimate.toFixed(2)),
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
