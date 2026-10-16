import { request } from "../request";

export interface SeoProject {
  project_id: string;
  name: string;
  domain: string;
  agent_id: string;
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
  runs: (id: string) => request<ResearchRun[]>(`${projectPath(id)}/runs`),
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
