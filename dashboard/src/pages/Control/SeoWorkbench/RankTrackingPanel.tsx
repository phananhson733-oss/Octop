import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
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
  type RankCell,
  type RankCompare,
  type RankDevices,
  type RankResults,
  type RankRow,
  type RankSnapshot,
  type RankTrackerSummary,
} from "../../../api/modules/seo";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import { attemptKey } from "./attemptKey";
import { safeHttpUrl } from "./SerpPanel";
import { AskExpertButton } from "./AskExpert";
import styles from "./index.module.less";

const { Text } = Typography;
const POLL_MS = 3000;
const DEPTHS = [10, 20, 30, 50, 100];
const DEVICES: RankDevices[] = ["both", "desktop", "mobile"];
const COMPARES: RankCompare[] = ["1d", "7d", "30d", "90d"];
// OpenSEO's caps: 200 keywords per tracker.
const MAX_KEYWORDS = 200;

const usd = (value: number) => `$${Number(value.toFixed(4))}`;
const lines = (text: string) => [
  ...new Set(
    text
      .split("\n")
      .map((line) => line.trim().toLowerCase())
      .filter(Boolean),
  ),
];
const shown = (devices: RankDevices) =>
  devices === "both" ? (["desktop", "mobile"] as const) : [devices];

function Position({ cell }: { cell: RankCell }) {
  const { t } = useTranslation();
  if (!cell.checked_at) return <Text type="secondary">—</Text>;
  const { position, previous_position: before } = cell;
  let change = null;
  if (position != null && before != null && before !== position) {
    const up = before > position;
    change = (
      <Tag color={up ? "green" : "red"}>
        {up ? "↑" : "↓"}
        {Math.abs(before - position)}
      </Tag>
    );
  } else if (position != null && before == null && cell.previous_checked_at)
    change = <Tag color="green">{t("seo.rankNewEntry")}</Tag>;
  else if (position == null && before != null)
    change = <Tag color="red">{t("seo.rankLost")}</Tag>;
  return (
    <Space size={4}>
      <Text strong={position != null}>
        {position ?? t("seo.rankNotRanked")}
      </Text>
      {change}
    </Space>
  );
}

function History({
  projectId,
  trackerId,
  row,
  onClose,
}: {
  projectId: string;
  trackerId: string;
  row?: RankRow;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [items, setItems] = useState<RankSnapshot[]>();
  const [error, setError] = useState("");
  useEffect(() => {
    if (!row) return;
    let active = true;
    setItems(undefined);
    setError("");
    seoApi
      .rankHistory(projectId, trackerId, row.keyword_id)
      .then((list) => {
        if (active) setItems(list);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [projectId, trackerId, row, t]);
  return (
    <Drawer
      open={!!row}
      onClose={onClose}
      title={t("seo.rankHistoryTitle", { keyword: row?.keyword })}
      width="min(640px, 100vw)"
    >
      {error && <Alert type="error" showIcon message={error} />}
      <Table<RankSnapshot>
        rowKey={(item) => `${item.run_id}:${item.device}`}
        loading={!items && !error}
        dataSource={[...(items ?? [])].reverse()}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        columns={[
          {
            title: t("seo.rankCheckedAt"),
            dataIndex: "checked_at",
            render: (value: string) => formatServerIsoDateTime(value, timeZone),
          },
          {
            title: t("seo.rankDevices"),
            dataIndex: "device",
            width: 90,
            render: (value: string) => t(`seo.device_${value}`),
          },
          {
            title: t("seo.rankPosition"),
            dataIndex: "position",
            width: 90,
            render: (value: number | null) => value ?? t("seo.rankNotRanked"),
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
    </Drawer>
  );
}

function NewTracker({
  projectId,
  domain,
  onCreated,
}: {
  projectId: string;
  domain: string;
  onCreated: (results: RankResults) => void;
}) {
  const { t } = useTranslation();
  const [target, setTarget] = useState("");
  const [devices, setDevices] = useState<RankDevices>("both");
  const [depth, setDepth] = useState(20);
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const keywords = lines(text);
  async function create() {
    setSaving(true);
    setError("");
    try {
      onCreated(
        await seoApi.createRankTracker(projectId, {
          ...(target.trim() ? { domain: target.trim() } : {}),
          devices,
          depth,
          keywords,
        }),
      );
    } catch (err) {
      setError(apiErrorMessage(err, t("seo.operationError"), t));
    } finally {
      setSaving(false);
    }
  }
  return (
    <Card size="small" title={t("seo.rankNew")}>
      <div className={styles.stack}>
        {error && <Alert type="error" showIcon message={error} />}
        <div className={styles.toolbar}>
          <Input
            aria-label={t("seo.rankDomain")}
            className={styles.projectSelect}
            value={target}
            onChange={(event) => setTarget(event.target.value)}
            placeholder={t("seo.rankDomainPlaceholder", { domain })}
          />
          <Select
            aria-label={t("seo.rankDevices")}
            value={devices}
            onChange={setDevices}
            options={DEVICES.map((value) => ({
              value,
              label: t(`seo.rankDevices_${value}`),
            }))}
          />
          <Select
            aria-label={t("seo.rankDepth")}
            value={depth}
            onChange={setDepth}
            options={DEPTHS.map((value) => ({
              value,
              label: t("seo.rankDepthOption", { depth: value }),
            }))}
          />
        </div>
        <Input.TextArea
          aria-label={t("seo.rankKeywords")}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={t("seo.rankKeywords")}
          autoSize={{ minRows: 3, maxRows: 8 }}
        />
        <div className={styles.toolbar}>
          <Button
            type="primary"
            loading={saving}
            disabled={keywords.length > MAX_KEYWORDS}
            onClick={create}
          >
            {t("seo.rankCreate")}
          </Button>
          {keywords.length > MAX_KEYWORDS && (
            <Text type="danger">
              {t("seo.lookupTooMany", { max: MAX_KEYWORDS })}
            </Text>
          )}
        </div>
      </div>
    </Card>
  );
}

/**
 * Rank tracking (OpenSEO rank tracker): where a domain ranks on Google for a
 * keyword list in the project's market. Opening it reads saved checks only;
 * each check is a confirmed, paid click whose maximum cost is shown first.
 */
export function RankTrackingPanel({
  projectId,
  domain,
  configured,
  isVisible,
}: {
  projectId: string;
  domain: string;
  configured: boolean;
  isVisible: boolean;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [trackers, setTrackers] = useState<RankTrackerSummary[]>();
  const [trackerId, setTrackerId] = useState<string>();
  const [compare, setCompare] = useState<RankCompare>("7d");
  const [results, setResults] = useState<RankResults>();
  const [creating, setCreating] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  // A failed poll clears itself on the next good one.
  const [loadError, setLoadError] = useState("");
  const [historyRow, setHistoryRow] = useState<RankRow>();
  const attempt = useRef<{ input: string; key: string }>();
  useEffect(() => {
    let active = true;
    seoApi
      .rankTrackers(projectId)
      .then((list) => {
        if (!active) return;
        setTrackers(list);
        setTrackerId(list[0]?.tracker_id);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [projectId, t]);
  // Progress is polled only while a check runs and the tab is on screen.
  const running = results?.run?.status === "running";
  useEffect(() => {
    if (!trackerId || !isVisible) return;
    let active = true;
    const load = () =>
      seoApi
        .rankResults(projectId, trackerId, compare)
        .then((value) => {
          if (!active) return;
          setLoadError("");
          setResults(value);
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
  }, [projectId, trackerId, compare, isVisible, running, t]);
  const current =
    results && results.tracker.tracker_id === trackerId ? results : undefined;
  const tracker = current?.tracker;
  const run = current?.run;

  function show(next: RankResults) {
    setResults(next);
    setTrackerId(next.tracker.tracker_id);
    const id = next.tracker.tracker_id;
    setTrackers((list = []) =>
      list.some((item) => item.tracker_id === id)
        ? list.map((item) => (item.tracker_id === id ? next.tracker : item))
        : [...list, next.tracker],
    );
  }
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
  const reload = async (id: string) =>
    show(await seoApi.rankResults(projectId, id, compare));
  async function check() {
    if (!tracker) return;
    const input = JSON.stringify([tracker.tracker_id, tracker.estimate_usd]);
    // An unchanged retry after an uncertain failure reuses the same key.
    if (attempt.current?.input !== input)
      attempt.current = { input, key: attemptKey() };
    try {
      const started = await seoApi.startRankCheck(
        projectId,
        tracker.tracker_id,
        {
          idempotency_key: attempt.current.key,
          confirm_paid: true,
          max_cost_usd: tracker.estimate_usd,
        },
      );
      attempt.current = undefined;
      setResults((value) => value && { ...value, run: started });
    } catch (err) {
      // Refused (busy, or the estimate grew meanwhile): show what is true now.
      await reload(tracker.tracker_id).catch(() => undefined);
      throw err;
    }
  }
  async function fillSaved() {
    const { rows } = await seoApi.keywords(projectId);
    const picks = [...rows]
      .sort((a, b) => (b.search_volume ?? -1) - (a.search_volume ?? -1))
      .slice(0, Math.max(MAX_KEYWORDS - (tracker?.keyword_count ?? 0), 0))
      .map((row) => row.keyword);
    if (picks.length) setText(picks.join("\n"));
    else setError(t("seo.lookupNoSaved"));
  }
  const keywords = lines(text);
  return (
    <div className={styles.stack}>
      <Text type="secondary">{t("seo.rankHelp")}</Text>
      {!configured && (
        <Alert type="info" showIcon message={t("seo.rankNeedsProvider")} />
      )}
      {(error || loadError) && (
        <Alert type="error" showIcon message={error || loadError} />
      )}
      <div className={styles.toolbar}>
        {!!trackers?.length && (
          <Select
            aria-label={t("seo.rankTracker")}
            className={styles.projectSelect}
            value={trackerId}
            onChange={setTrackerId}
            options={trackers.map((item) => ({
              value: item.tracker_id,
              label: item.domain,
            }))}
          />
        )}
        <Button onClick={() => setCreating((value) => !value)}>
          {t("seo.rankNew")}
        </Button>
      </div>
      {trackers?.length === 0 && !creating && (
        <Alert type="info" message={t("seo.rankNone")} />
      )}
      {creating && (
        <NewTracker
          projectId={projectId}
          domain={domain}
          onCreated={(next) => {
            setCreating(false);
            show(next);
          }}
        />
      )}
      {tracker && (
        <Card size="small" title={tracker.domain}>
          <div className={styles.stack}>
            <div className={styles.toolbar}>
              <Select
                aria-label={t("seo.rankDevices")}
                value={tracker.devices}
                disabled={!!busy}
                onChange={(devices) =>
                  act("settings", async () =>
                    show(
                      await seoApi.updateRankTracker(
                        projectId,
                        tracker.tracker_id,
                        { devices },
                      ),
                    ),
                  )
                }
                options={DEVICES.map((value) => ({
                  value,
                  label: t(`seo.rankDevices_${value}`),
                }))}
              />
              <Select
                aria-label={t("seo.rankDepth")}
                value={tracker.depth}
                disabled={!!busy}
                onChange={(depth) =>
                  act("settings", async () =>
                    show(
                      await seoApi.updateRankTracker(
                        projectId,
                        tracker.tracker_id,
                        { depth },
                      ),
                    ),
                  )
                }
                options={DEPTHS.map((value) => ({
                  value,
                  label: t("seo.rankDepthOption", { depth: value }),
                }))}
              />
              <Select
                aria-label={t("seo.rankCompare")}
                value={compare}
                onChange={setCompare}
                options={COMPARES.map((value) => ({
                  value,
                  label: t(`seo.rankCompare_${value}`),
                }))}
              />
              <Popconfirm
                title={t("seo.rankCheck")}
                description={t("seo.rankPaidConfirm", {
                  checks: tracker.keyword_count * shown(tracker.devices).length,
                  cost: usd(tracker.estimate_usd),
                })}
                okText={t("seo.serpConfirmBuy")}
                cancelText={t("seo.cancel")}
                onConfirm={() => act("check", check)}
                disabled={
                  !configured || running || !tracker.keyword_count || !!busy
                }
              >
                <Button
                  type="primary"
                  loading={busy === "check"}
                  disabled={!configured || running || !tracker.keyword_count}
                >
                  {t("seo.rankCheck")}
                </Button>
              </Popconfirm>
              <Text type="secondary">
                {t("seo.rankEstimate", { cost: usd(tracker.estimate_usd) })}
              </Text>
              <Popconfirm
                title={t("seo.rankDeleteConfirm")}
                okText={t("seo.rankDelete")}
                cancelText={t("seo.cancel")}
                onConfirm={() =>
                  act("delete", async () => {
                    await seoApi.deleteRankTracker(
                      projectId,
                      tracker.tracker_id,
                    );
                    const rest = (trackers ?? []).filter(
                      (item) => item.tracker_id !== tracker.tracker_id,
                    );
                    setTrackers(rest);
                    setTrackerId(rest[0]?.tracker_id);
                    setResults(undefined);
                  })
                }
                disabled={running || !!busy}
              >
                <Button danger disabled={running}>
                  {t("seo.rankDelete")}
                </Button>
              </Popconfirm>
            </div>
            {run?.status === "running" ? (
              <Space wrap>
                <Text>
                  {t("seo.rankRunning", {
                    done: run.checks_done,
                    total: run.checks_total,
                  })}
                </Text>
                <Progress
                  className={styles.projectSelect}
                  percent={Math.round(
                    (100 * run.checks_done) / Math.max(run.checks_total, 1),
                  )}
                  size="small"
                />
              </Space>
            ) : run ? (
              <Space wrap>
                <Tag>{t(`seo.status_${run.status}`)}</Tag>
                <Text>
                  {tracker.last_checked_at
                    ? t("seo.rankLastChecked", {
                        date: formatServerIsoDateTime(
                          tracker.last_checked_at,
                          timeZone,
                        ),
                      })
                    : t("seo.rankNever")}
                </Text>
                <Text>
                  {t("seo.cost", {
                    cost:
                      run.cost_usd == null
                        ? t("seo.missing")
                        : usd(run.cost_usd),
                  })}
                </Text>
                {run.checks_failed > 0 && (
                  <Text type="warning">
                    {t("seo.rankFailedChecks", { count: run.checks_failed })}
                  </Text>
                )}
                <AskExpertButton
                  disabled={!!busy}
                  what={t("seo.askExpertWhat.rank", { id: tracker.tracker_id })}
                />
              </Space>
            ) : (
              <Text type="secondary">{t("seo.rankNever")}</Text>
            )}
            <Input.TextArea
              aria-label={t("seo.rankAddKeywords")}
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder={t("seo.rankKeywords")}
              autoSize={{ minRows: 2, maxRows: 6 }}
            />
            <div className={styles.toolbar}>
              <Button
                loading={busy === "add"}
                disabled={!keywords.length || !!busy}
                onClick={() =>
                  act("add", async () => {
                    await seoApi.addRankKeywords(
                      projectId,
                      tracker.tracker_id,
                      keywords,
                    );
                    setText("");
                    await reload(tracker.tracker_id);
                  })
                }
              >
                {t("seo.rankAddKeywords")}
              </Button>
              <Button onClick={() => act("saved", fillSaved)}>
                {t("seo.lookupUseSaved")}
              </Button>
              <Text type="secondary">
                {t("seo.rankKeywordCount", {
                  count: tracker.keyword_count,
                  max: MAX_KEYWORDS,
                })}
              </Text>
            </div>
            <Table<RankRow>
              rowKey="keyword_id"
              dataSource={current.rows}
              pagination={{ pageSize: 50, showSizeChanger: false }}
              locale={{ emptyText: t("seo.rankEmpty") }}
              columns={[
                { title: t("seo.keyword"), dataIndex: "keyword" },
                ...shown(tracker.devices).map((device) => ({
                  title: t(`seo.device_${device}`),
                  key: device,
                  width: 130,
                  sorter: (a: RankRow, b: RankRow) =>
                    (a[device].position ?? 999) - (b[device].position ?? 999),
                  render: (_: unknown, row: RankRow) => (
                    <Position cell={row[device]} />
                  ),
                })),
                {
                  title: t("seo.serpPage"),
                  key: "url",
                  render: (_, row) => {
                    const url = safeHttpUrl(row[shown(tracker.devices)[0]].url);
                    return url ? (
                      <a href={url} target="_blank" rel="noopener noreferrer">
                        {new URL(url).pathname}
                      </a>
                    ) : (
                      "—"
                    );
                  },
                },
                {
                  key: "actions",
                  width: 170,
                  render: (_, row) => (
                    <Space size={4}>
                      <Button size="small" onClick={() => setHistoryRow(row)}>
                        {t("seo.rankHistory")}
                      </Button>
                      <Popconfirm
                        title={t("seo.rankRemoveConfirm")}
                        okText={t("seo.rankRemove")}
                        cancelText={t("seo.cancel")}
                        onConfirm={() =>
                          act("remove", async () => {
                            await seoApi.removeRankKeyword(
                              projectId,
                              tracker.tracker_id,
                              row.keyword_id,
                            );
                            await reload(tracker.tracker_id);
                          })
                        }
                      >
                        <Button size="small" disabled={!!busy}>
                          {t("seo.rankRemove")}
                        </Button>
                      </Popconfirm>
                    </Space>
                  ),
                },
              ]}
            />
          </div>
        </Card>
      )}
      {tracker && (
        <History
          projectId={projectId}
          trackerId={tracker.tracker_id}
          row={historyRow}
          onClose={() => setHistoryRow(undefined)}
        />
      )}
    </div>
  );
}
