import { request } from "../request";

export interface SeoProject {
  project_id: string;
  name: string;
  domain: string;
  /** Null after the bound expert was deleted, until the project is rebound. */
  agent_id: string | null;
  location_code: number;
  language_code: string;
  created_at: string;
}
export interface KeywordRow {
  keyword: string;
  search_volume: number | null;
  cpc: number | null;
  competition: number | null;
  keyword_difficulty: number | null;
  intent: string | null;
  trend: { year: number; month: number; search_volume: number | null }[];
}
export interface SavedKeyword extends KeywordRow {
  keyword_id: string;
  location_code: number;
  language_code: string;
  observed_at: string;
  source_run_id: string;
  tags: string[];
}
export interface ResearchRun {
  run_id: string;
  project_id: string;
  status: "running" | "completed" | "partial" | "failed" | "unknown";
  observed_at: string;
  location_code: number;
  language_code: string;
  results: {
    seed: string;
    ok: boolean;
    source: string;
    used_fallback: boolean;
    rows: KeywordRow[];
    error?: string;
  }[];
  cost_usd: number | null;
  cost_known_usd?: number;
  cached: boolean;
  error?: string;
}
/** A run as listed: per-seed status and row counts, without keyword rows. */
export interface RunSummary extends Omit<ResearchRun, "results"> {
  results: {
    seed: string;
    ok: boolean;
    source: string;
    used_fallback: boolean;
    row_count: number;
    error?: string;
  }[];
}
export interface SerpItem {
  type: string;
  /** Position among all SERP elements. */
  rank: number | null;
  /** Position among organic results only; null for other elements. */
  organic_rank: number | null;
  title: string | null;
  url: string | null;
  domain: string | null;
  description: string | null;
}
export interface SerpSummary {
  serp_id: string;
  keyword: string;
  device: "desktop" | "mobile";
  depth: number;
  requested_depth: number;
  status: ResearchRun["status"];
  observed_at: string;
  item_count: number;
  cost_usd: number | null;
  cached: boolean;
  source_serp_id?: string | null;
  error?: string | null;
}
export interface SerpSnapshot extends SerpSummary {
  items: SerpItem[];
}
export interface RankedKeyword {
  keyword: string;
  /** Position among all SERP elements. */
  rank: number | null;
  /** Position among organic results only. */
  organic_rank: number | null;
  url: string | null;
  /** Estimated monthly visits from this keyword. */
  etv: number | null;
  search_volume: number | null;
  cpc: number | null;
  keyword_difficulty: number | null;
  intent: string;
}
export interface SerpCompetitor {
  domain: string;
  visibility: number | null;
  avg_position: number | null;
  median_position: number | null;
  etv: number | null;
  keywords_count: number | null;
  keywords_positions: Record<string, number[]>;
}
export interface LookupSummary {
  lookup_id: string;
  kind: "domain" | "competitors";
  domain: string | null;
  keywords: string[] | null;
  status: ResearchRun["status"];
  observed_at: string;
  item_count: number;
  overview: {
    organic_traffic: number | null;
    organic_keywords: number | null;
    pos_1: number | null;
    pos_2_3: number | null;
    pos_4_10: number | null;
  } | null;
  /** Every keyword the domain ranks for; items holds the top 100. */
  total_count: number | null;
  cost_usd: number | null;
  cached: boolean;
  source_lookup_id?: string | null;
  error?: string | null;
}
export interface Lookup extends LookupSummary {
  items: RankedKeyword[] | SerpCompetitor[];
}
export const SECTION_KEYS = [
  "business_overview",
  "current_goal",
  "positioning",
  "writing_preferences",
] as const;
export const KEY_PAGE_ROLES = ["hub", "spoke", "money", "other"] as const;
interface Authored {
  updated_at: string;
  updated_by: string;
}
export interface ProjectContext {
  sections: ({ key: string; content: string } & Authored)[];
  missing_sections: string[];
  custom_sections: ({
    slug: string;
    title: string | null;
    content: string;
  } & Authored)[];
  competitors: ({
    domain: string;
    name: string | null;
    notes: string | null;
  } & Authored)[];
  key_pages: ({
    url: string;
    role: (typeof KEY_PAGE_ROLES)[number];
    topic: string | null;
    notes: string | null;
  } & Authored)[];
  research_log: {
    log_id: string;
    entry_date: string;
    summary: string;
    created_by: string;
    created_at: string;
  }[];
  research_log_limit: number;
}
/** One project-background change; a batch is applied all-or-nothing. */
export type ContextOp =
  | { section: (typeof SECTION_KEYS)[number]; content: string }
  | { custom_section: string; title?: string; content: string }
  | { delete_custom_section: string }
  | { add_competitors: { domain: string; name?: string; notes?: string }[] }
  | { remove_competitors: string[] }
  | {
      add_key_pages: {
        url: string;
        role?: (typeof KEY_PAGE_ROLES)[number];
        topic?: string;
        notes?: string;
      }[];
    }
  | { remove_key_pages: string[] }
  | { remove_research_log: string[] };
export interface ResearchInput {
  keywords: string[];
  mode: "auto" | "related" | "suggestions" | "ideas";
  result_limit: number;
  clickstream: boolean;
  group_keywords: boolean;
  idempotency_key: string;
  confirm_paid: boolean;
}
export interface SeoReport {
  report_id: string;
  project_id: string;
  run_id: string;
  title: string;
  summary: string;
  skill: string;
  created_at: string;
  content_hash: string;
  size_bytes: number;
}
export type AuditSeverity = "critical" | "warning" | "info";
export interface SeoAudit {
  audit_id: string;
  project_id: string;
  requested_url: string;
  /** Where the crawl began, after redirects. */
  start_url: string | null;
  max_pages: number;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  phase: "discovery" | "crawl" | "render" | "finalize";
  pages_crawled: number;
  pages_total: number;
  discovered_count: number;
  sitemap_count: number;
  /** Every discovered URL was crawled; null until the crawl ends. */
  crawl_complete: boolean | null;
  rate_limited: boolean;
  error_code: "target_blocked" | "db_error" | "unknown" | null;
  error_detail: string | null;
  cancel_requested: boolean;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}
export interface SeoAuditDetail extends SeoAudit {
  issue_counts: Record<AuditSeverity, number>;
}
export interface AuditIssue {
  issue_type: string;
  severity: AuditSeverity;
  page_url: string;
  details: Record<string, unknown> | null;
}
export interface AuditIssueType {
  issue_type: string;
  severity: AuditSeverity;
  count: number;
  title: string;
  explanation: string;
  how_to_fix: string;
}
export interface AuditPage {
  url: string;
  status_code: number;
  fetch_class: "ok" | "blocked" | "rate_limited" | "error";
  redirect_url: string | null;
  title: string;
  word_count: number;
  is_html: boolean;
  javascript_shell: boolean;
  is_indexable: boolean;
  response_time_ms: number;
  crawl_depth: number | null;
  in_sitemap: boolean;
}
export interface GoogleAccount {
  grant_id: string;
  email: string | null;
  search_console: boolean;
  analytics: boolean;
  updated_at: number;
}
export interface GoogleStatus {
  configured: boolean;
  /** Register exactly this on the Google OAuth client. */
  redirect_uri: string;
  accounts: GoogleAccount[];
}
export interface GscAccountSites extends GoogleAccount {
  sites: { site_url: string; permission_level: string | null }[];
  requires_reconnect: boolean;
  api_disabled: boolean;
}
export interface GscConnection {
  project_id: string;
  grant_id: string;
  site_url: string;
  permission_level: string;
  connected_at: number;
  email: string | null;
}
export interface Ga4Property {
  /** properties/<number> */
  property_id: string;
  display_name: string;
  account_display_name: string;
}
export interface Ga4AccountProperties extends GoogleAccount {
  properties: Ga4Property[];
  requires_reconnect: boolean;
  api_disabled: boolean;
}
export interface Ga4Connection {
  project_id: string;
  grant_id: string;
  property_id: string;
  display_name: string;
  /** Report days follow the property's time zone. */
  time_zone: string;
  currency_code: string;
  connected_at: number;
  email: string | null;
}
export interface Ga4DateRange {
  start_date: string;
  end_date: string;
}
export interface Ga4Comparison {
  current: number | null;
  previous: number | null;
  absolute_change: number | null;
  /** 0-1 fraction; null when the previous value is 0. */
  percent_change: number | null;
}
export type Ga4Metrics = Record<string, number | null>;
export interface Ga4Overview {
  source: { property_id: string; display_name: string; email: string | null };
  request: {
    requested_date_range: Ga4DateRange | null;
    resolved_date_range: Ga4DateRange;
    previous_date_range: Ga4DateRange;
    time_zone: string;
    currency_code: string;
    channel: string;
    trend: "daily" | "weekly";
  };
  current: Ga4Metrics | null;
  previous: Ga4Metrics | null;
  comparison: Record<string, Ga4Comparison>;
  /** Rows keyed by date (YYYYMMDD) or yearWeek and metric names. */
  trend: (Ga4Metrics & { date?: string; yearWeek?: string })[];
  diagnostics: { code: string }[];
  has_limited_data: boolean;
  warnings: string[];
}
export type GscDateRange =
  | "last_7_days"
  | "last_28_days"
  | "last_3_months"
  | "last_6_months"
  | "last_12_months"
  | "last_16_months";
export interface GscTextFilter {
  operator: "contains" | "notContains" | "equals" | "notEquals";
  expression: string;
}
export interface GscReportQuery {
  date_range: GscDateRange;
  device?: "DESKTOP" | "MOBILE" | "TABLET";
  /** ISO 3166-1 alpha-3, lowercase, as Search Console reports it. */
  country?: string;
  page_filter?: GscTextFilter;
  query_filter?: GscTextFilter;
}
export interface GscTotals {
  clicks: number;
  impressions: number;
  /** 0-1 fraction. */
  ctr: number;
  position: number;
}
export interface GscDimensionRow {
  key: string;
  clicks: number;
  impressions: number;
  ctr: number;
  position: number | null;
}
export interface GscReport {
  site_url: string;
  email: string | null;
  range: {
    start_date: string;
    end_date: string;
    prev_start_date: string;
    prev_end_date: string;
  };
  totals: GscTotals;
  prev_totals: GscTotals;
  daily: { date: string; clicks: number | null; impressions: number | null }[];
  striking_distance: {
    query: string;
    page: string;
    clicks: number;
    impressions: number;
    position: number;
  }[];
  countries: GscDimensionRow[];
}
export interface GscTable {
  dimension: "query" | "page";
  page: number;
  page_size: number;
  has_next_page: boolean;
  total_count: number | null;
  rows: GscDimensionRow[];
}
export interface GscInspection {
  url: string;
  result: Record<string, unknown> | null;
  error?: string | null;
}
const projectPath = (id: string) => `/seo/projects/${encodeURIComponent(id)}`;
const auditPath = (id: string, auditId: string) =>
  `${projectPath(id)}/audits/${encodeURIComponent(auditId)}`;
export const seoApi = {
  projects: () => request<SeoProject[]>("/seo/projects"),
  createProject: (data: Omit<SeoProject, "project_id" | "created_at">) =>
    request<SeoProject>("/seo/projects", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  bindAgent: (id: string, agentId: string) =>
    request<SeoProject>(projectPath(id), {
      method: "PATCH",
      body: JSON.stringify({ agent_id: agentId }),
    }),
  provider: () => request<{ configured: boolean }>("/seo/provider"),
  configureProvider: (data: { login: string; password: string }) =>
    request<{ configured: boolean }>("/seo/provider", {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  research: (id: string, data: ResearchInput) =>
    request<ResearchRun>(`${projectPath(id)}/research`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  runs: (id: string) => request<RunSummary[]>(`${projectPath(id)}/runs`),
  run: (id: string, runId: string) =>
    request<ResearchRun>(
      `${projectPath(id)}/runs/${encodeURIComponent(runId)}`,
    ),
  buySerp: (
    id: string,
    data: {
      keyword: string;
      device: SerpSummary["device"];
      depth: 20 | 100;
      idempotency_key: string;
      confirm_paid: true;
    },
  ) =>
    request<SerpSnapshot>(`${projectPath(id)}/serps`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  serps: (id: string, keyword: string) =>
    request<SerpSummary[]>(
      `${projectPath(id)}/serps?${new URLSearchParams({ keyword })}`,
    ),
  serp: (id: string, serpId: string) =>
    request<SerpSnapshot>(
      `${projectPath(id)}/serps/${encodeURIComponent(serpId)}`,
    ),
  buyLookup: (
    id: string,
    data: (
      | { kind: "domain"; domain: string }
      | { kind: "competitors"; keywords: string[] }
    ) & { idempotency_key: string; confirm_paid: true },
  ) =>
    request<Lookup>(`${projectPath(id)}/lookups`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  lookups: (id: string, kind: LookupSummary["kind"]) =>
    request<LookupSummary[]>(
      `${projectPath(id)}/lookups?${new URLSearchParams({ kind })}`,
    ),
  lookup: (id: string, lookupId: string) =>
    request<Lookup>(
      `${projectPath(id)}/lookups/${encodeURIComponent(lookupId)}`,
    ),
  context: (id: string) =>
    request<ProjectContext>(`${projectPath(id)}/context`),
  updateContext: (id: string, updates: ContextOp[]) =>
    request<ProjectContext>(`${projectPath(id)}/context`, {
      method: "PATCH",
      body: JSON.stringify({ updates }),
    }),
  keywords: (id: string, page = 1, search = "") =>
    request<{ rows: SavedKeyword[]; total_count: number }>(
      `${projectPath(id)}/keywords?${new URLSearchParams({
        page: String(page),
        page_size: "50",
        search,
      })}`,
    ),
  saveKeywords: (
    id: string,
    data: {
      run_id: string;
      keywords: string[];
      tags: string[];
      tag_mode: "append" | "replace";
    },
  ) =>
    request<{
      saved_count: number;
      created_count: number;
      updated_count: number;
    }>(`${projectPath(id)}/keywords`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  startAudit: (
    id: string,
    data: { start_url: string; max_pages: number; idempotency_key: string },
  ) =>
    request<SeoAudit>(`${projectPath(id)}/audits`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  audits: (id: string) => request<SeoAudit[]>(`${projectPath(id)}/audits`),
  audit: (id: string, auditId: string) =>
    request<SeoAuditDetail>(auditPath(id, auditId)),
  auditIssues: (id: string, auditId: string) =>
    request<{ summary: AuditIssueType[]; issues: AuditIssue[] }>(
      `${auditPath(id, auditId)}/issues`,
    ),
  auditPages: (id: string, auditId: string) =>
    request<{ pages: AuditPage[] }>(`${auditPath(id, auditId)}/pages`),
  cancelAudit: (id: string, auditId: string) =>
    request<SeoAudit>(`${auditPath(id, auditId)}/cancel`, { method: "POST" }),
  deleteAudit: (id: string, auditId: string) =>
    request<void>(auditPath(id, auditId), { method: "DELETE" }),
  reports: (id: string) => request<SeoReport[]>(`${projectPath(id)}/reports`),
  report: (id: string, reportId: string) =>
    request<SeoReport & { html: string }>(
      `${projectPath(id)}/reports/${encodeURIComponent(reportId)}`,
    ),
  google: () => request<GoogleStatus>("/seo/google"),
  connectGoogle: (returnPath: string) =>
    request<{ authorize_url: string }>("/seo/google/connect", {
      method: "POST",
      body: JSON.stringify({ return_path: returnPath }),
    }),
  disconnectGoogle: (grantId: string) =>
    request<void>(`/seo/google/accounts/${encodeURIComponent(grantId)}`, {
      method: "DELETE",
    }),
  gsc: (id: string) =>
    request<{ connection: GscConnection | null }>(`${projectPath(id)}/gsc`),
  gscSites: (id: string) =>
    request<{ accounts: GscAccountSites[] }>(`${projectPath(id)}/gsc/sites`),
  bindGsc: (id: string, data: { grant_id: string; site_url: string }) =>
    request<GscConnection>(`${projectPath(id)}/gsc`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  unbindGsc: (id: string) =>
    request<void>(`${projectPath(id)}/gsc`, { method: "DELETE" }),
  gscReport: (id: string, data: GscReportQuery) =>
    request<GscReport>(`${projectPath(id)}/gsc/report`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  gscTable: (
    id: string,
    data: GscReportQuery & {
      dimension: "query" | "page";
      page: number;
      page_size: number;
    },
  ) =>
    request<GscTable>(`${projectPath(id)}/gsc/table`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  gscInspect: (id: string, urls: string[]) =>
    request<{ site_url: string; results: GscInspection[] }>(
      `${projectPath(id)}/gsc/inspect`,
      { method: "POST", body: JSON.stringify({ urls }) },
    ),
  ga4: (id: string) =>
    request<{ connection: Ga4Connection | null }>(`${projectPath(id)}/ga4`),
  ga4Properties: (id: string) =>
    request<{ accounts: Ga4AccountProperties[] }>(
      `${projectPath(id)}/ga4/properties`,
    ),
  bindGa4: (id: string, data: { grant_id: string; property_id: string }) =>
    request<Ga4Connection>(`${projectPath(id)}/ga4`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  unbindGa4: (id: string) =>
    request<void>(`${projectPath(id)}/ga4`, { method: "DELETE" }),
  ga4Overview: (
    id: string,
    data: {
      start_date?: string;
      end_date?: string;
      trend?: "daily" | "weekly";
    },
  ) =>
    request<Ga4Overview>(`${projectPath(id)}/ga4/overview`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
};
