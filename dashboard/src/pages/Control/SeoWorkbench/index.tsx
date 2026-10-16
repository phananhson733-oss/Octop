import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  seoApi,
  type SeoProject,
  type ResearchRun,
  type KeywordRow,
  type ResearchInput,
  type RunSummary,
  type SavedKeyword,
  type SeoReport,
} from "../../../api/modules/seo";
import { octopThreadsApi } from "../../../api/modules/octopThreads";
import { useAgent } from "../../../context/AgentContext";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  groupSharedVolumeRows,
  type KeywordResearchDisplayRow,
} from "./groupSharedVolumeRows";
import { staticReportHtml } from "./reportPreview";
import { attemptKey } from "./attemptKey";
import { AuditPanel } from "./AuditPanel";
import { ProjectContextPanel } from "./ProjectContextPanel";
import { SerpPanel } from "./SerpPanel";
import { useSearchTabs } from "./searchTabs";
import styles from "./index.module.less";

const { Text, Paragraph } = Typography;

export default function SeoWorkbench({
  isVisible = true,
}: {
  isVisible?: boolean;
}) {
  const { t } = useTranslation();
  const { agents } = useAgent();
  const [query, setQuery] = useSearchParams();
  const requestedProject = query.get("project");
  const user = useCurrentUser();
  const [projects, setProjects] = useState<SeoProject[]>([]);
  const [projectId, setProjectId] = useState<string>();
  const [configured, setConfigured] = useState(false);
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState<"project" | "provider" | null>(null);
  const [busy, setBusy] = useState(false);
  const [projectForm] = Form.useForm();
  const [providerForm] = Form.useForm();
  const owned = agents.filter(
    (a) =>
      (a.is_owner || a.user_id === user?.id) &&
      !a.is_shared &&
      !a.bridge &&
      a.kind !== "team",
  );
  useEffect(() => {
    let active = true;
    Promise.all([seoApi.projects(), seoApi.provider()])
      .then(([list, provider]) => {
        if (!active) return;
        setProjects(list);
        setProjectId((selected) => selected ?? list[0]?.project_id);
        setConfigured(provider.configured);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [t]);
  useEffect(() => {
    if (
      requestedProject &&
      projects.some((p) => p.project_id === requestedProject)
    )
      setProjectId(requestedProject);
  }, [requestedProject, projects]);
  const selectProject = (value: string) => {
    setProjectId(value);
    setQuery({ project: value, view: "research" }, { replace: true });
  };
  const project = projects.find((p) => p.project_id === projectId);
  async function submitDialog(values: Record<string, string>) {
    setBusy(true);
    setError("");
    try {
      if (dialog === "provider") {
        const result = await seoApi.configureProvider({
          login: values.login,
          password: values.password,
        });
        setConfigured(result.configured);
        providerForm.resetFields();
      } else {
        const created = await seoApi.createProject({
          name: values.name,
          domain: values.domain,
          agent_id: values.agent_id,
          location_code: Number(values.location_code),
          language_code: values.language_code,
        });
        setProjects((list) => [...list, created]);
        selectProject(created.project_id);
        projectForm.resetFields();
      }
      setDialog(null);
    } catch (err) {
      setError(apiErrorMessage(err, t("seo.saveError"), t));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className={styles.page}>
      <div className={styles.stack}>
        {error && (
          <Alert
            type="error"
            showIcon
            message={error}
            closable
            onClose={() => setError("")}
          />
        )}
        <div className={styles.toolbar}>
          <Select
            aria-label={t("seo.project")}
            className={styles.projectSelect}
            value={projectId}
            placeholder={t("seo.project")}
            onChange={selectProject}
            options={projects.map((p) => ({
              value: p.project_id,
              label: `${p.name} · ${p.domain}`,
            }))}
          />
          <Button onClick={() => setDialog("project")}>
            {t("seo.createProject")}
          </Button>
          <Button onClick={() => setDialog("provider")}>
            {t("seo.provider")}
          </Button>
          <Tag color={configured ? "green" : "default"}>
            {t(configured ? "seo.configured" : "seo.notConfigured")}
          </Tag>
        </div>
        {project ? (
          <ProjectWorkspace
            key={project.project_id}
            project={project}
            configured={configured}
            isVisible={isVisible}
            agents={owned}
            onProject={(updated) =>
              setProjects((list) =>
                list.map((p) =>
                  p.project_id === updated.project_id ? updated : p,
                ),
              )
            }
          />
        ) : (
          <Empty description={t("seo.emptyProjects")} />
        )}
        <Modal
          open={dialog !== null}
          title={t(
            dialog === "provider" ? "seo.provider" : "seo.createProject",
          )}
          footer={null}
          onCancel={() => {
            setDialog(null);
            providerForm.resetFields();
          }}
          destroyOnClose
        >
          {dialog === "provider" ? (
            <Form form={providerForm} layout="vertical" onFinish={submitDialog}>
              <Paragraph>{t("seo.providerHelp")}</Paragraph>
              <Form.Item
                name="login"
                label={t("seo.login")}
                rules={[{ required: true }]}
              >
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item
                name="password"
                label={t("seo.password")}
                rules={[{ required: true }]}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
              <Button htmlType="submit" type="primary" loading={busy}>
                {t("seo.saveProvider")}
              </Button>
            </Form>
          ) : (
            <Form
              form={projectForm}
              layout="vertical"
              onFinish={submitDialog}
              initialValues={{ location_code: "2840", language_code: "en" }}
            >
              <Form.Item
                name="name"
                label={t("seo.name")}
                rules={[{ required: true }]}
              >
                <Input maxLength={120} />
              </Form.Item>
              <Form.Item
                name="domain"
                label={t("seo.domain")}
                rules={[{ required: true }]}
              >
                <Input placeholder="example.com" />
              </Form.Item>
              <Form.Item
                name="agent_id"
                label={t("seo.agent")}
                rules={[{ required: true }]}
              >
                <Select
                  options={owned.map((a) => ({
                    value: a.agent_id,
                    label: a.name,
                  }))}
                />
              </Form.Item>
              <Paragraph type="secondary">{t("seo.agentHelp")}</Paragraph>
              <Form.Item name="location_code" label={t("seo.market")}>
                <Select
                  options={[
                    ["2840", "us"],
                    ["2826", "uk"],
                    ["2124", "ca"],
                    ["2036", "au"],
                  ].map(([value, key]) => ({ value, label: t(`seo.${key}`) }))}
                />
              </Form.Item>
              <Form.Item name="language_code" label={t("seo.language")}>
                <Select options={[{ value: "en", label: t("seo.english") }]} />
              </Form.Item>
              <Button htmlType="submit" type="primary" loading={busy}>
                {t("seo.createProject")}
              </Button>
            </Form>
          )}
        </Modal>
      </div>
    </div>
  );
}

/** One search tab per seed of a research run. */
function tabsOf(run: { run_id: string; results: { seed: string }[] }) {
  return run.results.map((result) => ({
    runId: run.run_id,
    seed: result.seed,
  }));
}

function ProjectWorkspace({
  project,
  configured,
  isVisible,
  agents,
  onProject,
}: {
  project: SeoProject;
  configured: boolean;
  isVisible: boolean;
  agents: { agent_id: string; name: string }[];
  onProject: (updated: SeoProject) => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [query, setQuery] = useSearchParams();
  const view = query.get("view") || "research";
  const timeZone = useServerTimezone();
  const id = project.project_id;
  const tabs = useSearchTabs(id);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loaded, setLoaded] = useState<Record<string, ResearchRun>>({});
  const [saved, setSaved] = useState<SavedKeyword[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [reports, setReports] = useState<SeoReport[]>([]);
  const [preview, setPreview] = useState<SeoReport & { html: string }>();
  const [seeds, setSeeds] = useState("");
  const [mode, setMode] = useState<ResearchInput["mode"]>("auto");
  const [limit, setLimit] = useState(150);
  const [clickstream, setClickstream] = useState(false);
  const [group, setGroup] = useState(false);
  const [paid, setPaid] = useState(false);
  const [selected, setSelected] = useState<React.Key[]>([]);
  const [tags, setTags] = useState<string[]>([]);
  const [tagMode, setTagMode] = useState<"append" | "replace">("append");
  const [serpKeyword, setSerpKeyword] = useState<string>();
  const [bindTo, setBindTo] = useState<string>();
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const attempt = useRef<{ input: string; key: string }>();
  const { open: openTabs, openIfEmpty } = tabs;
  const date = (value: string) => formatServerIsoDateTime(value, timeZone);
  const fail = (err: unknown) =>
    setError(apiErrorMessage(err, t("seo.operationError"), t));
  const showRuns = useCallback(
    (list: RunSummary[]) => {
      setRuns(list);
      // A run still in progress may have settled; completed runs never change.
      setLoaded((all) =>
        Object.fromEntries(
          Object.entries(all).filter(
            ([runId, run]) =>
              list.find((item) => item.run_id === runId)?.status === run.status,
          ),
        ),
      );
      if (list[0]) openIfEmpty(tabsOf(list[0]));
      setSelected([]);
    },
    [openIfEmpty],
  );
  useEffect(() => {
    if (!isVisible) return;
    let active = true;
    Promise.all([seoApi.runs(id), seoApi.reports(id)])
      .then(([list, reportList]) => {
        if (!active) return;
        showRuns(list);
        setReports(reportList);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [id, t, isVisible, showRuns]);
  const active = tabs.active;
  const activeRun = active ? loaded[active.runId] : undefined;
  const missingRun = active && !activeRun ? active.runId : undefined;
  useEffect(() => {
    if (!missingRun) return;
    let live = true;
    // Only the open tab's run is loaded in full; the history list is summaries.
    seoApi
      .run(id, missingRun)
      .then((run) => {
        if (live) setLoaded((all) => ({ ...all, [run.run_id]: run }));
      })
      .catch((err) => {
        if (live) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      live = false;
    };
  }, [id, missingRun, t]);
  useEffect(() => setSelected([]), [tabs.activeId]);
  useEffect(() => {
    let active = true;
    setSaved([]);
    seoApi
      .keywords(id, page, search)
      .then((result) => {
        if (active) {
          setSaved(result.rows);
          setTotal(result.total_count);
        }
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [id, page, search, t]);
  const result = activeRun?.results.find((r) => r.seed === active?.seed);
  const rows = useMemo(() => {
    const unique = new Map<string, KeywordRow>();
    if (result?.ok)
      for (const row of result.rows)
        if (!unique.has(row.keyword)) unique.set(row.keyword, row);
    return [...unique.values()];
  }, [result]);
  const displayRows = useMemo(
    () => groupSharedVolumeRows(rows, result?.seed, group),
    [rows, result, group],
  );
  const runHasRows = !!activeRun?.results.some((r) => r.ok && r.rows.length);
  const metric = (value: number | null) =>
    value == null ? t("seo.missing") : value.toLocaleString();
  const columns = [
    {
      title: t("seo.keyword"),
      dataIndex: "keyword",
      render: (
        keyword: string,
        row: KeywordRow & { parentKeyword?: string | null },
      ) =>
        row.parentKeyword ? (
          <span
            style={{ paddingInlineStart: 20 }}
            title={t("seo.sharedWith", { keyword: row.parentKeyword })}
          >
            ↳ {keyword}
          </span>
        ) : (
          keyword
        ),
    },
    { title: t("seo.volume"), dataIndex: "search_volume", render: metric },
    { title: t("seo.cpc"), dataIndex: "cpc", render: metric },
    { title: t("seo.competition"), dataIndex: "competition", render: metric },
    {
      title: t("seo.difficulty"),
      dataIndex: "keyword_difficulty",
      render: metric,
    },
    {
      title: t("seo.intent"),
      dataIndex: "intent",
      render: (value: string | null) =>
        !value || value === "unknown" ? t("seo.missing") : value,
    },
    {
      title: t("seo.serp"),
      key: "serp",
      render: (_: unknown, row: KeywordRow) => (
        <Button size="small" onClick={() => setSerpKeyword(row.keyword)}>
          {t("seo.viewSerp")}
        </Button>
      ),
    },
  ];
  async function research() {
    const keywords = [
      ...new Set(
        seeds
          .split(/[\n,]/)
          .map((s) => s.trim().toLowerCase())
          .filter(Boolean),
      ),
    ];
    if (!keywords.length || keywords.length > 5) {
      setError(t("seo.seedLimit"));
      return;
    }
    const input = {
      keywords,
      mode,
      result_limit: limit,
      clickstream,
      group_keywords: group,
      confirm_paid: paid,
    };
    const fingerprint = JSON.stringify(input);
    if (attempt.current?.input !== fingerprint)
      attempt.current = { input: fingerprint, key: attemptKey() };
    setBusy("research");
    setError("");
    setNotice("");
    try {
      const run = await seoApi.research(id, {
        ...input,
        idempotency_key: attempt.current!.key,
      });
      setLoaded((all) => ({ ...all, [run.run_id]: run }));
      setRuns((list) => [
        {
          ...run,
          results: run.results.map(({ rows, ...rest }) => ({
            ...rest,
            row_count: rows.length,
          })),
        },
        ...list.filter((r) => r.run_id !== run.run_id),
      ]);
      openTabs(tabsOf(run));
      attempt.current = undefined;
    } catch (err) {
      fail(err);
      setNotice(t("seo.recoverHelp"));
    } finally {
      setBusy("");
    }
  }
  function openRun(runId: string) {
    const summary = runs.find((r) => r.run_id === runId);
    if (!summary) return;
    if (summary.results.length) openTabs(tabsOf(summary));
    else
      setNotice(
        t("seo.runWithoutSeeds", {
          status: t(`seo.status_${summary.status}`),
        }),
      );
  }
  async function save() {
    if (!activeRun) return;
    setBusy("save");
    setError("");
    try {
      const result = await seoApi.saveKeywords(id, {
        run_id: activeRun.run_id,
        keywords: selected.map(String),
        tags,
        tag_mode: tagMode,
      });
      setNotice(
        t("seo.savedCount", {
          created: result.created_count,
          updated: result.updated_count,
          count: result.saved_count,
        }),
      );
      const list = await seoApi.keywords(id, page, search);
      setSaved(list.rows);
      setTotal(list.total_count);
    } catch (err) {
      fail(err);
    } finally {
      setBusy("");
    }
  }
  async function generateReport() {
    const agentId = project.agent_id;
    if (!activeRun || !agentId) return;
    setBusy("report");
    setError("");
    try {
      const thread = await octopThreadsApi.create(agentId);
      navigate(
        `/chat/${encodeURIComponent(agentId)}/${encodeURIComponent(
          thread.thread_id,
        )}`,
        {
          state: {
            prefillInput: t("seo.reportPrompt", {
              projectId: id,
              runId: activeRun.run_id,
            }),
          },
        },
      );
    } catch (err) {
      fail(err);
    } finally {
      setBusy("");
    }
  }
  async function bind() {
    if (!bindTo) return;
    setBusy("bind");
    setError("");
    try {
      onProject(await seoApi.bindAgent(id, bindTo));
      setBindTo(undefined);
    } catch (err) {
      fail(err);
    } finally {
      setBusy("");
    }
  }
  const previewHtml = useMemo(
    () => (preview ? staticReportHtml(preview.html) : ""),
    [preview],
  );
  return (
    <div className={styles.stack}>
      <Text type="secondary">
        {t("seo.scope", {
          market: project.location_code,
          language: project.language_code,
        })}
      </Text>
      {!project.agent_id && (
        <Alert
          type="warning"
          showIcon
          message={t("seo.unbound")}
          description={
            <div className={styles.toolbar}>
              <Select
                aria-label={t("seo.agent")}
                className={styles.projectSelect}
                value={bindTo}
                onChange={setBindTo}
                placeholder={t("seo.agent")}
                options={agents.map((a) => ({
                  value: a.agent_id,
                  label: a.name,
                }))}
              />
              <Button
                disabled={!bindTo || !!busy}
                loading={busy === "bind"}
                onClick={bind}
              >
                {t("seo.bindAgent")}
              </Button>
            </div>
          }
        />
      )}
      {error && <Alert type="error" showIcon message={error} />}
      {notice && <Alert type="info" showIcon message={notice} />}
      <Tabs
        activeKey={
          ["research", "saved", "reports", "audit", "context"].includes(view)
            ? view
            : "research"
        }
        onChange={(next) =>
          setQuery({ project: id, view: next }, { replace: true })
        }
        items={[
          {
            key: "research",
            label: t("seo.research"),
            children: (
              <div className={styles.stack}>
                <Card size="small">
                  <div className={styles.stack}>
                    <label>
                      {t("seo.seeds")}
                      <Input.TextArea
                        aria-label={t("seo.seeds")}
                        value={seeds}
                        onChange={(event) => setSeeds(event.target.value)}
                        rows={2}
                        placeholder={t("seo.seedsPlaceholder")}
                      />
                    </label>
                    <div className={styles.toolbar}>
                      <Select
                        aria-label={t("seo.mode")}
                        value={mode}
                        onChange={setMode}
                        options={[
                          "auto",
                          "related",
                          "suggestions",
                          "ideas",
                        ].map((value) => ({
                          value,
                          label: t(`seo.mode_${value}`),
                        }))}
                      />
                      <Select
                        aria-label={t("seo.limit")}
                        value={limit}
                        onChange={setLimit}
                        options={[150, 300, 500].map((value) => ({
                          value,
                          label: String(value),
                        }))}
                      />
                      <Checkbox
                        checked={clickstream}
                        onChange={(e) => setClickstream(e.target.checked)}
                      >
                        {t("seo.clickstream")}
                      </Checkbox>
                      <Checkbox
                        checked={group}
                        onChange={(e) => setGroup(e.target.checked)}
                      >
                        {t("seo.group")}
                      </Checkbox>
                    </div>
                    <Checkbox
                      checked={paid}
                      onChange={(e) => setPaid(e.target.checked)}
                    >
                      {t("seo.confirmPaid")}
                    </Checkbox>
                    <Space>
                      <Button
                        type="primary"
                        loading={busy === "research"}
                        disabled={!configured || !paid || !!busy}
                        onClick={research}
                      >
                        {t("seo.runResearch")}
                      </Button>
                      <Text type="secondary">{t("seo.explicitOnly")}</Text>
                    </Space>
                  </div>
                </Card>
                <div className={styles.toolbar}>
                  <Select
                    className={styles.projectSelect}
                    aria-label={t("seo.history")}
                    value={active?.runId}
                    onChange={openRun}
                    options={runs.map((r) => ({
                      value: r.run_id,
                      label: `${date(r.observed_at)} · ${t(
                        `seo.status_${r.status}`,
                      )} · ${r.results.map((item) => item.seed).join(", ")}`,
                    }))}
                    placeholder={t("seo.history")}
                  />
                  <Button
                    disabled={!!busy}
                    onClick={() => {
                      void seoApi.runs(id).then(showRuns).catch(fail);
                    }}
                  >
                    {t("seo.refreshRuns")}
                  </Button>
                </div>
                {tabs.tabs.length ? (
                  <Tabs
                    type="editable-card"
                    hideAdd
                    size="small"
                    activeKey={tabs.activeId ?? undefined}
                    onChange={tabs.select}
                    onEdit={(key, action) => {
                      if (action === "remove" && typeof key === "string")
                        tabs.close(key);
                    }}
                    items={tabs.tabs.map((tab) => {
                      const summary = runs.find((r) => r.run_id === tab.runId);
                      return {
                        key: tab.id,
                        label: (
                          <span>
                            {tab.seed}{" "}
                            {summary && (
                              <Text type="secondary">
                                {date(summary.observed_at)}
                              </Text>
                            )}
                          </span>
                        ),
                      };
                    })}
                  />
                ) : (
                  <Empty description={t("seo.noSearchTabs")} />
                )}
                {active && (
                  <>
                    {activeRun && (
                      <>
                        <Space wrap>
                          <Tag>{t(`seo.status_${activeRun.status}`)}</Tag>
                          <Text>
                            {t("seo.observed", {
                              date: date(activeRun.observed_at),
                            })}
                          </Text>
                          <Text>
                            {t("seo.cost", {
                              cost:
                                activeRun.cost_usd == null
                                  ? t("seo.missing")
                                  : `$${activeRun.cost_usd}`,
                            })}
                          </Text>
                          {activeRun.cost_usd == null &&
                            activeRun.cost_known_usd != null && (
                              <Text>
                                {t("seo.knownCost", {
                                  cost: `$${activeRun.cost_known_usd}`,
                                })}
                              </Text>
                            )}
                          {activeRun.cached && <Tag>{t("seo.cached")}</Tag>}
                        </Space>
                        {activeRun.error && (
                          <Alert type="warning" message={activeRun.error} />
                        )}
                        {activeRun.results
                          .filter((r) => !r.ok)
                          .map((r) => (
                            <Alert
                              key={r.seed}
                              type="warning"
                              message={`${r.seed}: ${
                                r.error || t("seo.status_failed")
                              }`}
                            />
                          ))}
                      </>
                    )}
                    <Text type="secondary">
                      {t("seo.missingHelp")} {group && t("seo.groupHelp")}
                    </Text>
                    <Table<KeywordResearchDisplayRow>
                      rowKey="keyword"
                      loading={!activeRun}
                      dataSource={displayRows}
                      columns={columns}
                      scroll={{ x: 860 }}
                      rowSelection={{
                        selectedRowKeys: selected,
                        onChange: setSelected,
                      }}
                      pagination={{ pageSize: 20 }}
                      locale={{ emptyText: t("seo.emptyResults") }}
                    />
                    <div className={styles.toolbar}>
                      <Select
                        mode="tags"
                        aria-label={t("seo.tags")}
                        placeholder={t("seo.tags")}
                        style={{ minWidth: 200 }}
                        value={tags}
                        onChange={setTags}
                        maxCount={20}
                      />
                      <Select
                        aria-label={t("seo.tagMode")}
                        value={tagMode}
                        onChange={setTagMode}
                        options={[
                          { value: "append", label: t("seo.append") },
                          { value: "replace", label: t("seo.replace") },
                        ]}
                      />
                      <Button
                        loading={busy === "save"}
                        disabled={
                          !!busy ||
                          !selected.length ||
                          selected.length > 500 ||
                          (tagMode === "replace" &&
                            !tags.some((tag) => tag.trim()))
                        }
                        onClick={save}
                      >
                        {t("seo.saveKeywords", { count: selected.length })}
                      </Button>
                      <Button
                        disabled={!!busy || !runHasRows || !project.agent_id}
                        loading={busy === "report"}
                        onClick={generateReport}
                      >
                        {t("seo.generateReport")}
                      </Button>
                    </div>
                    <Text type="secondary">{t("seo.reportHelp")}</Text>
                  </>
                )}
              </div>
            ),
          },
          {
            key: "saved",
            label: t("seo.saved"),
            children: (
              <div className={styles.stack}>
                <Input.Search
                  aria-label={t("seo.searchSaved")}
                  placeholder={t("seo.searchSaved")}
                  onSearch={(value) => {
                    setSearch(value);
                    setPage(1);
                  }}
                  allowClear
                />
                <Table<SavedKeyword>
                  rowKey="keyword_id"
                  dataSource={saved}
                  columns={[
                    ...columns,
                    {
                      title: t("seo.tags"),
                      dataIndex: "tags",
                      render: (values: string[]) =>
                        values.map((tag) => <Tag key={tag}>{tag}</Tag>),
                    },
                    {
                      title: t("seo.sourceDate"),
                      dataIndex: "observed_at",
                      render: date,
                    },
                  ]}
                  scroll={{ x: 1100 }}
                  pagination={{
                    current: page,
                    pageSize: 50,
                    total,
                    onChange: setPage,
                    showSizeChanger: false,
                  }}
                />
              </div>
            ),
          },
          {
            key: "reports",
            label: t("seo.reports"),
            children: (
              <div className={styles.stack}>
                <Button
                  onClick={() => {
                    void seoApi.reports(id).then(setReports).catch(fail);
                  }}
                >
                  {t("seo.refreshReports")}
                </Button>
                <Table<SeoReport>
                  rowKey="report_id"
                  dataSource={reports}
                  columns={[
                    { title: t("seo.title"), dataIndex: "title" },
                    { title: t("seo.summary"), dataIndex: "summary" },
                    {
                      title: t("seo.created"),
                      dataIndex: "created_at",
                      render: date,
                    },
                    {
                      title: t("seo.preview"),
                      key: "preview",
                      render: (_, report) => (
                        <Button
                          onClick={() => {
                            void seoApi
                              .report(id, report.report_id)
                              .then(setPreview)
                              .catch(fail);
                          }}
                        >
                          {t("seo.preview")}
                        </Button>
                      ),
                    },
                  ]}
                />
              </div>
            ),
          },
          {
            key: "audit",
            label: t("seo.audit"),
            children: (
              <AuditPanel
                projectId={id}
                domain={project.domain}
                isVisible={isVisible && view === "audit"}
              />
            ),
          },
          {
            key: "context",
            label: t("seo.context"),
            children: <ProjectContextPanel projectId={id} />,
          },
        ]}
      />
      <Drawer
        open={!!serpKeyword}
        title={t("seo.serpTitle", { keyword: serpKeyword })}
        width="min(760px, 100vw)"
        onClose={() => setSerpKeyword(undefined)}
        destroyOnHidden
      >
        {serpKeyword && (
          <SerpPanel
            key={serpKeyword}
            projectId={id}
            keyword={serpKeyword}
            configured={configured}
          />
        )}
      </Drawer>
      <Modal
        open={!!preview}
        title={preview?.title}
        footer={null}
        width="90vw"
        onCancel={() => setPreview(undefined)}
        destroyOnClose
      >
        <Paragraph type="secondary">{t("seo.staticPreview")}</Paragraph>
        <Paragraph copyable>{preview?.content_hash}</Paragraph>
        <iframe
          className={styles.preview}
          title={t("seo.preview")}
          sandbox=""
          referrerPolicy="no-referrer"
          srcDoc={previewHtml}
        />
      </Modal>
    </div>
  );
}
