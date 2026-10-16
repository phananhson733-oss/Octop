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
const projectPath = (id: string) => `/seo/projects/${encodeURIComponent(id)}`;
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
  reports: (id: string) => request<SeoReport[]>(`${projectPath(id)}/reports`),
  report: (id: string, reportId: string) =>
    request<SeoReport & { html: string }>(
      `${projectPath(id)}/reports/${encodeURIComponent(reportId)}`,
    ),
};
