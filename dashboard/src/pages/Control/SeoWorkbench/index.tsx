import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
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

function ProjectWorkspace({
  project,
  configured,
  isVisible,
}: {
  project: SeoProject;
  configured: boolean;
  isVisible: boolean;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [query, setQuery] = useSearchParams();
  const view = query.get("view") || "research";
  const timeZone = useServerTimezone();
  const [runs, setRuns] = useState<ResearchRun[]>([]);
  const [run, setRun] = useState<ResearchRun>();
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
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const attempt = useRef<{ input: string; key: string }>();
  const id = project.project_id;
  const date = (value: string) => formatServerIsoDateTime(value, timeZone);
  const fail = (err: unknown) =>
    setError(apiErrorMessage(err, t("seo.operationError"), t));
  const refreshRuns = async () => {
    const list = await seoApi.runs(id);
    setRuns(list);
    setRun(
      (previous) => list.find((r) => r.run_id === previous?.run_id) ?? list[0],
    );
    setSelected([]);
  };
  useEffect(() => {
    if (!isVisible) return;
    let active = true;
    Promise.all([seoApi.runs(id), seoApi.reports(id)])
      .then(([list, reportList]) => {
        if (!active) return;
        setRuns(list);
        setRun(
          (previous) =>
            list.find((item) => item.run_id === previous?.run_id) ?? list[0],
        );
        setSelected([]);
        setReports(reportList);
      })
      .catch((err) => {
        if (active) setError(apiErrorMessage(err, t("seo.loadError"), t));
      });
    return () => {
      active = false;
    };
  }, [id, t, isVisible]);
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
  const rows = useMemo(() => {
    const unique = new Map<string, KeywordRow>();
    for (const result of run?.results ?? [])
      if (result.ok)
        for (const row of result.rows)
          if (!unique.has(row.keyword)) unique.set(row.keyword, row);
    return [...unique.values()];
  }, [run]);
  const displayRows = useMemo(
    () =>
      groupSharedVolumeRows(rows, run?.results.find((r) => r.ok)?.seed, group),
    [rows, run, group],
  );
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
      attempt.current = { input: fingerprint, key: crypto.randomUUID() };
    setBusy("research");
    setError("");
    setNotice("");
    try {
      const result = await seoApi.research(id, {
        ...input,
        idempotency_key: attempt.current!.key,
      });
      setRun(result);
      setSelected([]);
      setRuns((list) => [
        result,
        ...list.filter((r) => r.run_id !== result.run_id),
      ]);
      attempt.current = undefined;
    } catch (err) {
      fail(err);
      setNotice(t("seo.recoverHelp"));
    } finally {
      setBusy("");
    }
  }
  async function save() {
    if (!run) return;
    setBusy("save");
    setError("");
    try {
      const result = await seoApi.saveKeywords(id, {
        run_id: run.run_id,
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
    if (!run) return;
    setBusy("report");
    setError("");
    try {
      const thread = await octopThreadsApi.create(project.agent_id);
      navigate(
        `/chat/${encodeURIComponent(project.agent_id)}/${encodeURIComponent(
          thread.thread_id,
        )}`,
        {
          state: {
            prefillInput: t("seo.reportPrompt", {
              projectId: id,
              runId: run.run_id,
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
      {error && <Alert type="error" showIcon message={error} />}
      {notice && <Alert type="info" showIcon message={notice} />}
      <Tabs
        activeKey={
          ["research", "saved", "reports"].includes(view) ? view : "research"
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
                    value={run?.run_id}
                    onChange={(value) => {
                      setRun(runs.find((r) => r.run_id === value));
                      setSelected([]);
                    }}
                    options={runs.map((r) => ({
                      value: r.run_id,
                      label: `${date(r.observed_at)} · ${t(
                        `seo.status_${r.status}`,
                      )}`,
                    }))}
                    placeholder={t("seo.history")}
                  />
                  <Button
                    disabled={!!busy}
                    onClick={() => {
                      void refreshRuns().catch(fail);
                    }}
                  >
                    {t("seo.refreshRuns")}
                  </Button>
                </div>
                {run && (
                  <>
                    <Space wrap>
                      <Tag>{t(`seo.status_${run.status}`)}</Tag>
                      <Text>
                        {t("seo.observed", { date: date(run.observed_at) })}
                      </Text>
                      <Text>
                        {t("seo.cost", {
                          cost:
                            run.cost_usd == null
                              ? t("seo.missing")
                              : `$${run.cost_usd}`,
                        })}
                      </Text>
                      {run.cost_usd == null && run.cost_known_usd != null && (
                        <Text>
                          {t("seo.knownCost", {
                            cost: `$${run.cost_known_usd}`,
                          })}
                        </Text>
                      )}
                      {run.cached && <Tag>{t("seo.cached")}</Tag>}
                    </Space>
                    {run.error && <Alert type="warning" message={run.error} />}
                    {run.results
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
                    <Text type="secondary">
                      {t("seo.missingHelp")} {group && t("seo.groupHelp")}
                    </Text>
                    <Table<KeywordResearchDisplayRow>
                      rowKey="keyword"
                      dataSource={displayRows}
                      columns={columns}
                      scroll={{ x: 760 }}
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
                        disabled={!!busy || !rows.length}
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
                  scroll={{ x: 1000 }}
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
        ]}
      />
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
