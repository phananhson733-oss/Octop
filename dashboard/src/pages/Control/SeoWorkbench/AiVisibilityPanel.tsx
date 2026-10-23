import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Drawer,
  Input,
  Popconfirm,
  Progress,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  seoApi,
  type AiAnswer,
  type AiAnswerRow,
  type AiBranded,
  type AiBrandSummary,
  type AiEngine,
  type AiOwner,
  type AiPrompt,
  type AiSource,
  type AiVisibility,
} from "../../../api/modules/seo";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import { attemptKey } from "./attemptKey";
import { safeHttpUrl } from "./SerpPanel";
import { AskExpertButton } from "./AskExpert";
import styles from "./index.module.less";

const { Text, Paragraph } = Typography;
const POLL_MS = 3000;
const ENGINES: AiEngine[] = ["chatgpt", "gemini", "google_ai_overview"];
const FILTERS: AiBranded[] = ["neutral", "branded", "all"];
// OpenSEO's cap per batch, kept per project here.
const MAX_PROMPTS = 100;

const usd = (value: number) => `$${Number(value.toFixed(4))}`;
const percent = (part: number, whole: number) =>
  whole ? `${Math.round((100 * part) / whole)}%` : "—";
const lines = (text: string) => [
  ...new Set(
    text
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean),
  ),
];

function Owner({ owner }: { owner: AiOwner }) {
  const { t } = useTranslation();
  if (!owner) return null;
  return (
    <Tag color={owner === "own" ? "green" : "orange"}>
      {t(`seo.aiOwner_${owner}`)}
    </Tag>
  );
}

function Answer({
  projectId,
  runId,
  row,
  onClose,
}: {
  projectId: string;
  runId?: string;
  row?: AiAnswerRow;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [answer, setAnswer] = useState<AiAnswer>();
  const [error, setError] = useState("");
  useEffect(() => {
    if (!row || !runId) return;
    let active = true;
    setAnswer(undefined);
    setError("");
    seoApi
      .aiAnswer(projectId, runId, row.prompt_id, row.engine)
      .then((value) => {
        if (active) setAnswer(value);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [projectId, runId, row, t]);
  return (
    <Drawer
      open={!!row}
      onClose={onClose}
      title={
        row &&
        t("seo.aiAnswerTitle", { engine: t(`seo.aiEngine_${row.engine}`) })
      }
      width="min(720px, 100vw)"
    >
      <div className={styles.stack}>
        {error && <Alert type="error" showIcon message={error} />}
        <Text strong>{row?.prompt}</Text>
        {answer && (
          <>
            <Space wrap size={4}>
              {answer.brands
                .filter((brand) => brand.mentioned || brand.cited)
                .map((brand) => (
                  <Tag
                    key={brand.domain}
                    color={brand.own ? "green" : "orange"}
                  >
                    {brand.name}
                    {brand.cited ? ` · ${t("seo.aiCited")}` : ""}
                  </Tag>
                ))}
            </Space>
            {/* Model output: plain text only, never rendered as markdown or HTML. */}
            {answer.answer_markdown ? (
              <Paragraph className={styles.preWrap}>
                {answer.answer_markdown}
              </Paragraph>
            ) : (
              <Text type="secondary">
                {answer.status === "failed"
                  ? t("seo.aiStatus_failed")
                  : t("seo.aiNoAnswerText")}
              </Text>
            )}
            <Text type="secondary">{t("seo.aiAnswerPlain")}</Text>
            {answer.citations.length > 0 && (
              <Table
                size="small"
                rowKey="url"
                dataSource={answer.citations}
                pagination={{ pageSize: 20, showSizeChanger: false }}
                columns={[
                  { title: "#", dataIndex: "position", width: 50 },
                  {
                    title: t("seo.serpPage"),
                    dataIndex: "url",
                    render: (value: string, citation) => {
                      const url = safeHttpUrl(value);
                      return (
                        <Space size={4}>
                          {url ? (
                            <a
                              href={url}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              {citation.title || citation.domain}
                            </a>
                          ) : (
                            citation.domain
                          )}
                          <Owner owner={citation.owner} />
                        </Space>
                      );
                    },
                  },
                ]}
              />
            )}
          </>
        )}
      </div>
    </Drawer>
  );
}

/**
 * AI visibility (OpenSEO prompt tracking): whether ChatGPT, Gemini and Google
 * AI Overviews name the project, and cite it, for saved prompts. Opening it
 * reads saved checks only; each check is a confirmed, paid click whose cost
 * is shown first.
 */
export function AiVisibilityPanel({
  projectId,
  configured,
  isVisible,
}: {
  projectId: string;
  configured: boolean;
  isVisible: boolean;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [runId, setRunId] = useState<string>();
  const [branded, setBranded] = useState<AiBranded>("neutral");
  const [gap, setGap] = useState(false);
  const [data, setData] = useState<AiVisibility>();
  const [text, setText] = useState("");
  const [topic, setTopic] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  // A failed poll clears itself on the next good one.
  const [loadError, setLoadError] = useState("");
  const [answerRow, setAnswerRow] = useState<AiAnswerRow>();
  const attempt = useRef<{ input: string; key: string }>();
  const latest = data?.runs[0];
  // Progress is polled only while a check runs and the tab is on screen.
  const running = latest?.status === "running";
  useEffect(() => {
    if (!isVisible) return;
    let active = true;
    const load = () =>
      seoApi
        .aiVisibility(projectId, { run_id: runId, branded, gap })
        .then((value) => {
          if (!active) return;
          setLoadError("");
          setData(value);
        })
        .catch((err) => {
          if (active) setLoadError(apiErrorMessage(err, t("seo.loadError"), t));
        });
    void load();
    const timer = running ? window.setInterval(load, POLL_MS) : undefined;
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [projectId, runId, branded, gap, isVisible, running, t]);

  async function act(name: string, work: () => Promise<void>) {
    setBusy(name);
    setError("");
    try {
      await work();
    } catch (err) {
      setError(apiErrorMessage(err, t("seo.operationError"), t));
    } finally {
      setBusy("");
    }
  }
  const reload = async () =>
    setData(
      await seoApi.aiVisibility(projectId, { run_id: runId, branded, gap }),
    );
  async function check() {
    if (!data) return;
    const { estimate_usd: cost } = data.settings;
    const input = JSON.stringify([projectId, cost, data.settings.engines]);
    // An unchanged retry after an uncertain failure reuses the same key.
    if (attempt.current?.input !== input)
      attempt.current = { input, key: attemptKey() };
    try {
      const started = await seoApi.startAiCheck(projectId, {
        idempotency_key: attempt.current.key,
        confirm_paid: true,
        max_cost_usd: cost,
      });
      attempt.current = undefined;
      setRunId(undefined);
      setData((value) => value && { ...value, runs: [started, ...value.runs] });
    } catch (err) {
      // Refused (busy, or the estimate grew meanwhile): show what is true now.
      await reload().catch(() => undefined);
      throw err;
    }
  }

  const prompts = lines(text);
  const settings = data?.settings;
  const run = data?.run;
  const own = data?.brands.find((brand) => brand.own);
  return (
    <div className={styles.stack}>
      <Text type="secondary">{t("seo.aiHelp")}</Text>
      {!configured && (
        <Alert type="info" showIcon message={t("seo.aiNeedsProvider")} />
      )}
      {(error || loadError) && (
        <Alert type="error" showIcon message={error || loadError} />
      )}
      {settings && (
        <Card size="small" title={t("seo.aiPrompts")}>
          <div className={styles.stack}>
            <div className={styles.toolbar}>
              <Text>{t("seo.aiEngines")}</Text>
              <Checkbox.Group
                aria-label={t("seo.aiEngines")}
                value={settings.engines}
                disabled={!!busy}
                onChange={(values) =>
                  values.length &&
                  act("engines", async () =>
                    setData(
                      await seoApi.setAiEngines(
                        projectId,
                        values as AiEngine[],
                      ),
                    ),
                  )
                }
                options={ENGINES.map((value) => ({
                  value,
                  label: t(`seo.aiEngine_${value}`),
                }))}
              />
              <Popconfirm
                title={t("seo.aiCheck")}
                description={t("seo.aiPaidConfirm", {
                  answers: settings.prompt_count * settings.engines.length,
                  cost: usd(settings.estimate_usd),
                })}
                okText={t("seo.serpConfirmBuy")}
                cancelText={t("seo.cancel")}
                onConfirm={() => act("check", check)}
                disabled={
                  !configured || running || !settings.prompt_count || !!busy
                }
              >
                <Button
                  type="primary"
                  loading={busy === "check"}
                  disabled={!configured || running || !settings.prompt_count}
                >
                  {t("seo.aiCheck")}
                </Button>
              </Popconfirm>
              <Text type="secondary">
                {t("seo.aiEstimate", { cost: usd(settings.estimate_usd) })}
              </Text>
            </div>
            {latest?.status === "running" && (
              <Space wrap>
                <Text>
                  {t("seo.aiRunning", {
                    done: latest.answers_done,
                    total: latest.answers_total,
                  })}
                </Text>
                <Progress
                  className={styles.projectSelect}
                  percent={Math.round(
                    (100 * latest.answers_done) /
                      Math.max(latest.answers_total, 1),
                  )}
                  size="small"
                />
              </Space>
            )}
            <Input.TextArea
              aria-label={t("seo.aiAddPrompts")}
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder={t("seo.aiPromptsPlaceholder")}
              autoSize={{ minRows: 2, maxRows: 6 }}
            />
            <div className={styles.toolbar}>
              <Input
                aria-label={t("seo.aiTopic")}
                className={styles.projectSelect}
                value={topic}
                onChange={(event) => setTopic(event.target.value)}
                placeholder={t("seo.aiTopicPlaceholder")}
              />
              <Button
                loading={busy === "add"}
                disabled={!prompts.length || !!busy}
                onClick={() =>
                  act("add", async () => {
                    await seoApi.addAiPrompts(projectId, prompts, topic.trim());
                    setText("");
                    await reload();
                  })
                }
              >
                {t("seo.aiAddPrompts")}
              </Button>
              <Text type="secondary">
                {t("seo.aiPromptCount", {
                  count: settings.prompt_count,
                  max: MAX_PROMPTS,
                })}
              </Text>
            </div>
            <Table<AiPrompt>
              size="small"
              rowKey="prompt_id"
              dataSource={data.prompts}
              pagination={{ pageSize: 20, showSizeChanger: false }}
              locale={{ emptyText: t("seo.aiNoPrompts") }}
              columns={[
                { title: t("seo.aiTopic"), dataIndex: "topic", width: 140 },
                {
                  title: t("seo.aiPrompt"),
                  dataIndex: "prompt",
                  render: (value: string, prompt) => (
                    <Space size={4} wrap>
                      {value}
                      {prompt.branded && <Tag>{t("seo.aiBranded")}</Tag>}
                    </Space>
                  ),
                },
                {
                  key: "actions",
                  width: 90,
                  render: (_, prompt) => (
                    <Popconfirm
                      title={t("seo.aiRemoveConfirm")}
                      okText={t("seo.aiRemove")}
                      cancelText={t("seo.cancel")}
                      onConfirm={() =>
                        act("remove", async () => {
                          await seoApi.removeAiPrompt(
                            projectId,
                            prompt.prompt_id,
                          );
                          await reload();
                        })
                      }
                    >
                      <Button size="small" disabled={!!busy}>
                        {t("seo.aiRemove")}
                      </Button>
                    </Popconfirm>
                  ),
                },
              ]}
            />
          </div>
        </Card>
      )}
      {data && (
        <Card size="small" title={t("seo.aiResults")}>
          <div className={styles.stack}>
            <Text type="secondary">{t("seo.aiCompetitorsHint")}</Text>
            <div className={styles.toolbar}>
              {data.runs.length > 0 && (
                <Select
                  aria-label={t("seo.aiRun")}
                  className={styles.projectSelect}
                  value={run?.run_id}
                  onChange={(value) =>
                    setRunId(value === data.runs[0].run_id ? undefined : value)
                  }
                  options={data.runs.map((item) => ({
                    value: item.run_id,
                    label: `${formatServerIsoDateTime(
                      item.started_at,
                      timeZone,
                    )} · ${t(`seo.status_${item.status}`)}`,
                  }))}
                />
              )}
              <Select
                aria-label={t("seo.aiFilter")}
                value={branded}
                onChange={setBranded}
                options={FILTERS.map((value) => ({
                  value,
                  label: t(`seo.aiFilter_${value}`),
                }))}
              />
              <Checkbox
                checked={gap}
                onChange={(event) => setGap(event.target.checked)}
              >
                {t("seo.aiGap")}
              </Checkbox>
            </div>
            {run ? (
              <Space wrap>
                <Tag>{t(`seo.status_${run.status}`)}</Tag>
                <Text>{t("seo.aiCoverage", data.coverage)}</Text>
                <Text>
                  {t("seo.cost", {
                    cost:
                      run.cost_usd == null
                        ? t("seo.missing")
                        : usd(run.cost_usd),
                  })}
                </Text>
                {run.answers_failed > 0 && (
                  <Text type="warning">
                    {t("seo.aiFailedAnswers", { count: run.answers_failed })}
                  </Text>
                )}
                {run.status !== "running" && (
                  <AskExpertButton
                    what={t("seo.askExpertWhat.ai", { id: run.run_id })}
                  />
                )}
              </Space>
            ) : (
              <Text type="secondary">{t("seo.aiNever")}</Text>
            )}
            <Table<AiBrandSummary>
              size="small"
              rowKey="domain"
              dataSource={data.summaries}
              pagination={false}
              locale={{ emptyText: t("seo.aiEmpty") }}
              columns={[
                {
                  title: t("seo.aiBrand"),
                  dataIndex: "name",
                  render: (value: string, brand) => (
                    <Space size={4}>
                      {value}
                      {brand.own && (
                        <Tag color="green">{t("seo.aiOwner_own")}</Tag>
                      )}
                    </Space>
                  ),
                },
                {
                  title: t("seo.aiMentionRate"),
                  key: "mentions",
                  render: (_, brand) => percent(brand.mentions, brand.answers),
                },
                {
                  title: t("seo.aiCitationRate"),
                  key: "citations",
                  render: (_, brand) => percent(brand.citations, brand.answers),
                },
                {
                  title: t("seo.aiAvgPosition"),
                  dataIndex: "avg_position",
                  render: (value: number | null) => value ?? "—",
                },
              ]}
            />
            <Table<AiAnswerRow>
              size="small"
              rowKey={(row) => `${row.prompt_id}:${row.engine}`}
              dataSource={data.rows}
              pagination={{ pageSize: 25, showSizeChanger: false }}
              locale={{ emptyText: t("seo.aiEmpty") }}
              columns={[
                { title: t("seo.aiPrompt"), dataIndex: "prompt" },
                {
                  title: t("seo.aiEngine"),
                  dataIndex: "engine",
                  width: 150,
                  render: (value: AiEngine) => t(`seo.aiEngine_${value}`),
                },
                {
                  title: own?.name ?? t("seo.aiOwner_own"),
                  key: "own",
                  width: 170,
                  render: (_, row) => {
                    if (row.status !== "answered")
                      return (
                        <Text type="secondary">
                          {t(`seo.aiStatus_${row.status}`)}
                        </Text>
                      );
                    const match = row.brands.find((brand) => brand.own);
                    return (
                      <Space size={4}>
                        {match?.mentioned && (
                          <Tag color="green">{t("seo.aiMentioned")}</Tag>
                        )}
                        {match?.cited && (
                          <Tag color="blue">{t("seo.aiCited")}</Tag>
                        )}
                        {!match?.mentioned && !match?.cited && (
                          <Text type="secondary">{t("seo.aiAbsent")}</Text>
                        )}
                      </Space>
                    );
                  },
                },
                {
                  title: t("seo.aiCompetitorsNamed"),
                  key: "competitors",
                  render: (_, row) =>
                    row.brands
                      .filter(
                        (brand) =>
                          !brand.own && (brand.mentioned || brand.cited),
                      )
                      .map((brand) => brand.name)
                      .join(", ") || "—",
                },
                {
                  key: "actions",
                  width: 90,
                  render: (_, row) => (
                    <Button size="small" onClick={() => setAnswerRow(row)}>
                      {t("seo.aiOpen")}
                    </Button>
                  ),
                },
              ]}
            />
            <Table<AiSource>
              size="small"
              rowKey="domain"
              title={() => t("seo.aiSources")}
              dataSource={data.sources}
              pagination={{ pageSize: 10, showSizeChanger: false }}
              locale={{ emptyText: t("seo.aiEmpty") }}
              columns={[
                {
                  title: t("seo.aiDomain"),
                  dataIndex: "domain",
                  render: (value: string, source) => (
                    <Space size={4}>
                      {value}
                      <Owner owner={source.owner} />
                    </Space>
                  ),
                },
                {
                  title: t("seo.aiCitations"),
                  dataIndex: "citations",
                  width: 110,
                },
                { title: t("seo.aiAnswers"), dataIndex: "answers", width: 110 },
              ]}
            />
          </div>
        </Card>
      )}
      <Answer
        projectId={projectId}
        runId={run?.run_id}
        row={answerRow}
        onClose={() => setAnswerRow(undefined)}
      />
    </div>
  );
}
