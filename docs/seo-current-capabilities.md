# SEO current implementation and verification

This is the current implementation map. The dated `seo-replica/` documents retain historical rollout evidence; they do not establish today's deployment status.

## Implemented surfaces

- Native DataForSEO research, saved keywords, SERP snapshots, competitors, top pages and backlinks.
- Site crawling and HTML/render comparison, GSC and GA4 reads, manually confirmed rank and AI visibility checks.
- Project-bound expert tools, business context, key pages and research log.
- Legacy keyword reports retain their run/hash contract. Technical audit, search-performance and comprehensive reports use explicit frozen source bundles.
- Audit findings can enter an action ledger: open → in progress → implemented → verified/needs review. The implementation record includes a concrete note; verification reads a later audit and never starts a paid request.

## Evidence reports

`POST /api/seo/projects/{project_id}/report-contexts` accepts `report_type` and `sources`:

```json
{"report_type":"technical-audit","sources":[{"kind":"audit","id":"audit-id"}]}
```

Stored source kinds: `research`, `audit`, `serp`, `lookup`, `ai_lookup`, `rank` (tracker ID), `ai` (run ID). `gsc` and `ga4` take `query` parameters instead of an ID, using the existing GSC performance / GA4 report command contracts. These are read-only queries; no supplier purchases occur. Results preserve resolved dates, pagination and partial coverage. Select source scope explicitly; a report snapshot is bounded to 2 MB and twelve sources.

A bundle contains the project background, the saved-keyword shortlist (membership, tags, metrics and original observation/source-run fields), and each source's result, reference/query, capture time and digest. Its immutable `context_id` and `context_hash` are used when saving HTML through the existing reports endpoint. It does not require a keyword run. New data requires a new bundle; old reports retain their evidence even if the original audit is deleted. AI source snapshots retain individual answers and citations, not just aggregate citation counts.

Expert tools: `seo_get_workflow`, `seo_prepare_report`, `seo_read_report_evidence`, `seo_save_evidence_report`. Evidence is paged as JSON text chunks: source_index=-1 reads background, -2 reads the frozen shortlist, and nonnegative indices read sources. Reassemble each before interpreting it. Older bundles without a shortlist return null; never substitute today's keywords. Legacy `seo_save_report` remains compatible; a keyword report incorporating additional sources should use a comprehensive bundle instead.

The audit panel exposes **Generate audit report**, the reports list exposes **Source evidence**, and the workbench has **Actions & rechecks**. Report generation uses the existing project expert chat; saving is confirmed only after the server receipt.

## Action verification limits

Track a real finding by audit ID, issue type and exact page URL. Repeated tracking of that source reuses the same action. Implementation requires a note describing the change or linking its evidence. Reopening retains history but clears current implementation/verification. The ledger links to existing projects and audit evidence; it is not a replacement for the agent's workspace TASKS.md.

Automatic verification is deliberately limited to directly observed page-local rules listed in `AUTO_VERIFY_TYPES` in `infra/seo/workflow.py`. It requires a different completed audit with full crawl/sitemap coverage, no rate limit, a successful non-shell/non-truncated HTML page observed after implementation, and a positively satisfied target field. For example, title/description checks require valid nonempty lengths and H1 checks require exactly one H1; deleting a long title is not a successful repair. Cross-page and render rules remain `needs_review` because rules may have been skipped or results capped. Missing pages, partial crawls and unavailable rendering never count as proof of repair. History retains the observations used for each recheck. New history entries record the authenticated actor separately from the data owner, including shared users through both HTTP and Agent tools. The actor is not accepted from model arguments or request bodies; old entries are not retroactively relabeled without evidence.

## Metric semantics

Native behavior intentionally differs from the fixed OpenSEO reference: a present clickstream volume of **0** is valid. Only null/missing clickstream values fall back to keyword_info. `volume_source` records clickstream/keyword_info/unknown; monthly null values remain null. The original upstream fixture remains unchanged, with the native divergence documented in its mapping test.

## Deliberate remaining boundaries

- Project research markets remain US/UK/CA/AU and English.
- No new CWV field-data or Lighthouse provider is added; crawler response timing is not LCP/INP/CLS.
- No new automatic paid schedule is added. Existing signed purchase confirmations remain required.
- The operator must verify actual Google/provider credentials, model quality and deployment identity separately. Local tests do not establish production availability.
- Fork SEO schema remains in `seo_workbench.sql` / `.pg.sql` via `_ensure_seo_schema`, outside upstream numbered migration watermarks. Existing report rows are preserved while run_id becomes nullable.

## Acceptance

Meaningful tests cover metric edge cases; evidence ownership, immutability and corruption; report creation with no keyword run; shared expert saving; legacy migration readback; implementation prerequisites; unobservable-page negatives; and a real crawler/rule + HTTP audit→report→implementation→recheck flow against a fake site. No external paid provider or model is needed for these checks. Run the repository's full quality gates before releasing.
