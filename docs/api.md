# API Reference

Every route is mounted under `/api`. JSON unless otherwise noted; SSE
endpoints emit `text/event-stream`; chat turns use a WebSocket
described in [Chat (WebSocket)](#chat-websocket).

> **Interactive docs** (Scalar) live at `/api/docs` when
> `enable_api_docs=true` (or `OCTOP_ENABLE_API_DOCS=1`). The schema
> is at `/api/openapi.json`.

## Authentication

| Header | Value |
|--------|-------|
| `Authorization` | `Bearer <access_token>` from `POST /api/auth/login` |

Tokens expire after `OCTOP_ACCESS_TOKEN_TTL` seconds (default 24 h).
Rotating the JWT secret (`octop admin rotate-jwt-secret`) invalidates
every outstanding token immediately. Login attempts are rate-limited
(`OCTOP_LOGIN_MAX_ATTEMPTS` / `OCTOP_LOGIN_LOCKOUT_SECONDS`); the
admin can clear the lockout with `POST /api/users/{id}/unlock-login`.

### Auth column legend

- **public** — no token required.
- **user** — any logged-in account.
- **owner** — same user that owns the resource (or admin).
- **admin** — admin role required.

### Public endpoints (no token)

`/api/docs`, `/api/openapi.json`, `/api/health`, `/api/setup/*`,
`/api/auth/login`, `/api/auth/captcha`, `/api/auth/oidc/status`, `/api/auth/oidc/start`,
`/api/auth/oidc/callback`, `/api/auth/oidc/exchange`, `/api/auth/oauth/status`,
`/api/auth/oauth/start`, `/api/auth/oauth/callback`, `/api/auth/oauth/exchange`,
`/api/auth/ldap/status`,
`/api/connectors/oauth/callback`, and `/api/internal/mcp/*`. All other routes are JWT-gated by
`api/middleware/jwt_auth.py`; the setup lockdown middleware
(`api/middleware/setup_lockdown.py`) additionally blocks non-setup
routes until the wizard finishes.

Password login may require a vendor captcha token. `GET /api/auth/captcha`
returns `{provider: "slider"}` by default (dashboard slider only; no server
check). When a strong provider is active (`turnstile`, `hcaptcha`,
`recaptcha`, `recaptcha-v3`, `tencent`), `POST /api/auth/login` must include
`captcha_token` (max 4096 characters; for `tencent` the dashboard sends the
callback pair as `ticket:randstr`, verified via GET with the client IP).
Captcha failures do not increment
login lockout. OIDC login is unchanged. A request for an unknown username
with a garbage token still triggers one outbound siteverify request (bounded
by a 10s timeout and the token length limit); a known locked user is
rejected before siteverify.

Offline recovery: delete the `captcha.settings` row via local CLI /
`settings_repo` if a settings-sourced strong provider is unreachable. If
the process refused to start because `OCTOP_CAPTCHA_PROVIDER` is strong
without both keys and no readable settings blob, edit `~/.octop/env` on
disk — the dashboard and `PUT /api/envs` are unreachable until the
process starts. `GET /api/envs` redacts `OCTOP_CAPTCHA_SECRET` as
`********`; sending that sentinel on PUT keeps the on-disk value.

If you serve the dashboard behind a CSP, allow
`challenges.cloudflare.com`; `js.hcaptcha.com`, `newassets.hcaptcha.com`,
`api.hcaptcha.com`; `www.google.com`, `www.gstatic.com`;
`turing.captcha.qcloud.com`, `captcha.qq.com`, `ssl.captcha.qq.com`. Octop
does not set these headers itself.

## Setup & auth

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/setup/status` | public | `{required, completed, has_admin}` |
| `GET`    | `/setup/presets` | public | Provider templates for the wizard |
| `POST`   | `/setup/begin` | public | Begin a wizard session (no password) |
| `POST`   | `/setup/verify-password` | public | Exchange the wizard password for a session token |
| `GET`    | `/setup/validate-token` | public | Check a wizard session token |
| `POST`   | `/setup/initial-admin` | public | body `{username, password, display_name?, email?}` → `201` |
| `POST`   | `/setup/resume-wizard` | public | Issue a fresh wizard token mid-setup |
| `POST`   | `/setup/test-provider` | public | Ping a provider draft (kind/base_url/api_key/model) |
| `POST`   | `/setup/finish` | public | Finalise setup and unlock the rest of the API |
| `GET`    | `/auth/captcha` | public | `{provider, site_key?}` for the login widget; 503 while setup is required |
| `POST`   | `/auth/login` | public | body `{username, password, captcha_token?}` (`username` may be email) → `{access_token, role, user, ...}`. When no local password matches, the credentials are retried against the configured LDAP directory; a first directory login provisions an Octop account (unless `auto_provision` is off) and derives the role from `admin_groups` |
| `GET`    | `/auth/oidc/status` | public | OIDC login availability and provider display name |
| `POST`   | `/auth/oidc/start` | public | body `{redirect_after?}` → identity-provider authorization URL |
| `GET`    | `/auth/oidc/callback` | public | Identity-provider callback; redirects to dashboard login completion |
| `POST`   | `/auth/oidc/exchange` | public | body `{code}` → same JWT response as `/auth/login` |
| `GET`    | `/auth/oidc/config` | admin | OIDC provider configuration and callback URL; client secret is omitted |
| `PUT`    | `/auth/oidc/config` | admin | Write OIDC provider configuration; `client_secret` is write-only |
| `POST`   | `/auth/oidc/config/test` | admin | Verify configured discovery metadata and JWKS endpoint |
| `GET`    | `/auth/oauth/status` | public | `{providers:[{kind, display_name, enabled}]}` |
| `POST`   | `/auth/oauth/start` | public | body `{kind, redirect_after?}` → authorization URL |
| `GET`    | `/auth/oauth/callback` | public | App OAuth callback (`code` or DingTalk `authCode`); same completion page as OIDC |
| `POST`   | `/auth/oauth/exchange` | public | Same one-time code exchange as `/auth/oidc/exchange` |
| `POST`   | `/auth/oauth/bind/start` | user | body `{kind, redirect_after?}` → bind the identity to the current user |
| `POST`   | `/auth/oauth/unbind` | user | Unlink one SSO identity by `kind` (requires a local password when it is the last login method) |
| `GET`    | `/auth/oauth/providers/{kind}` | admin | Provider config; `kind` is `oidc`, `feishu`, `dingtalk`, or `wecom` |
| `PUT`    | `/auth/oauth/providers/{kind}` | admin | Upsert provider config; `client_secret` is write-only; WeCom uses `extra.agent_id` |
| `POST`   | `/auth/oauth/providers/{kind}/test` | admin | Test provider credentials |
| `GET`    | `/auth/ldap/status` | public | `{enabled, display_name}` for the login page hint |
| `GET`    | `/auth/ldap/config` | admin | Directory settings; `bind_password` is never returned (`has_bind_password` instead) |
| `PUT`    | `/auth/ldap/config` | admin | Upsert directory settings; `bind_password` is write-only (omit to keep the stored one) |
| `POST`   | `/auth/ldap/config/test` | admin | Service bind + probe of `user_base_dn` → `{ok, detail}` |
| `POST`   | `/auth/logout` | user | `204` |
| `GET`    | `/auth/me` | user | `{id, username, role, display_name, locale, ...}` |
| `PATCH`  | `/auth/me` | user | body `{display_name?, locale?, ...}` |
| `POST`   | `/auth/change-password` | user | body `{old_password, new_password}` → `204`. Accounts without a local password (directory/SSO-provisioned) get `PASSWORD_NOT_SET` |
| `GET`    | `/health` | public | `{status: "ok", version}` |

## Users (admin)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/users` | admin | `[{id, username, role, display_name, email, enabled, ...}]` |
| `POST`   | `/users` | admin | body `{username, password, role, display_name?, email?, permissions?, workspace_root_dir?, token_quota?}` → `201` |
| `GET`    | `/users/{id}` | admin | full user row |
| `PATCH`  | `/users/{id}` | admin | body subset of `{role, display_name, email, enabled, locale}` |
| `POST`   | `/users/{id}/reset-password` | admin | body `{new_password}` → `204` |
| `POST`   | `/users/{id}/unlock-login` | admin | `204` (clears the lockout) |
| `DELETE` | `/users/{id}` | admin | `204` |

## Agents

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/agents` | user | `[{id, agent_id, name, persona_mbti, state, unread_count, ...}]` |
| `POST`   | `/agents` | user | body `{name, persona_mbti?, default_model?, system_prompt?, description?, icon?, template_name?, config?}` → `201` |
| `GET`    | `/agents/{id}` | owner | full agent row |
| `PATCH`  | `/agents/{id}` | owner | body subset of create body |
| `DELETE` | `/agents/{id}` | owner | `204` |
| `POST`   | `/agents/{id}/start` | owner | `204` |
| `POST`   | `/agents/{id}/stop` | owner | `204` |
| `POST`   | `/agents/{id}/reload` | owner | `204` (rebuild harness runtime) |
| `POST`   | `/agents/{id}/read` | owner | `204` (mark unread badge cleared) |
| `GET`    | `/agents/{id}/status` | owner | `{state, last_error?, memory_maintenance?, ...}` |
| `POST`   | `/agents/from-expert/{expert_id}` | user | body `{name, ...}` → `201` (creates from bundled expert template) |
| `GET`    | `/agents/{id}/tool-settings` | owner | built-in + installed plugin tools with enable / disableable / available flags |
| `PUT`    | `/agents/{id}/tool-settings` | owner | body `{disabled_builtin: string[], plugins?}` — persists denylist + plugin flags (hot-sync, no reload) |
| `PATCH`  | `/agents/{id}/tool-settings/{tool_name}` | owner | body `{enabled, source, plugin_id?}` — toggle one tool (hot-sync) |

## Skills and skill packages

Skill copy operations create snapshots; they do not keep the source and
destination synchronized after the request completes.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/skill-packages?writable_only=true` | user (`skill_packages`) | List only packages the current user may modify; admins may modify all packages |
| `POST` | `/agents/{id}/skill-packages/{package_id}/copy` | owner (`skill_packages`) | body `{skill_slugs: string[], overwrite?: boolean}`; copy selected package skills into the workspace |
| `POST` | `/agents/{id}/skills/{slug}/push-to-package` | owner (`skill_packages`) | body `{package_id, overwrite?: boolean}`; copy a workspace skill into a package created by the user (or any package for admins) |

## Chat (WebSocket)

| Path | Auth | Notes |
|------|------|-------|
| `WS /agents/{id}/chat/ws?token=<jwt>` | owner | Primary dashboard turn endpoint. Send `{"type":"user_turn", ...}` frames (optional `hitl_policy` is a thread-scoped tool-approval bypass); server replies with harness stream chunks ending in `{"type":"done"}` or `{"type":"error","message":"..."}`. `{"type":"ping"}` → `{"type":"pong"}`. `{"type":"subscribe","thread_id"}` → `{"type":"turn_status","thread_id","active"}` (attach to an in-flight turn without cancelling on disconnect). `{"type":"cancel","thread_id"}` stops the active turn (explicit stop; disconnect alone does **not** cancel). |
| `GET /agents/{id}/chat/welcome` | agent access | `{welcome_message, quick_prompts, task_examples}`; `task_examples` is `null` when the workspace field is absent |
| `POST /agents/{id}/chat/page-runs` | owner | For workbench pages (no WebSocket). body `{prompt, session, title, fresh?}` → `202` `{run_id, thread_id, status, ...}`. The turn runs like a scheduled agent task in a dashboard thread of its own (session `page:<session>`; the caller's current chat thread is untouched) and shows up in the chat sidebar under `title` at once; its messages are written to the thread as they come. Same `session` name reuses that thread; a start while one is running returns the running run with `reused: true` (the new prompt is not sent). Agent must be running. |
| `GET /agents/{id}/chat/page-runs/{run_id}` | owner | → `{status: running \| done \| needs_input \| failed, reply, error, thread_id, ...}`; `needs_input` = the agent asked the user something, answer in the thread. In memory only: 404 after a restart (the thread shows how far the turn got). |
| `POST /agents/{id}/chat/polish` | owner | body `{text, default_model?}` → `{text}` (one-shot prompt refinement) |
| `POST /agents/{id}/chat/hitl/resume` | owner | body `{thread_id, decisions: [...], hitl_policy?}` → SSE chunk stream; finishes with `{"type":"done"}`. Optional `hitl_policy` (`ask` / `allow_all` / `allow_tools`) is a thread-scoped bypass and does not change global tool-approval settings. |

### Legacy SSE

The previous `POST /agents/{id}/chat/stream` is gone. The dashboard
streams turns over the WebSocket above; HITL resume stays on SSE
because each request is a one-shot continuation.

## Threads & history

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/agents/{id}/chat/sessions` | owner | `[{id, thread_id, title, archived, last_active, unread, ...}]` |
| `POST`   | `/agents/{id}/chat/sessions` | owner | body `{session_key?}` → `{thread_id, session_key}` |
| `PATCH`  | `/agents/{id}/chat/sessions/{thread_id}` | owner | body `{title?, pinned?}` → updated row |
| `DELETE` | `/agents/{id}/chat/sessions/{thread_id}` | owner | `204` (archives the active row) |
| `GET`    | `/agents/{id}/chat/sessions/{thread_id}/history` | owner | paginated message history; `turn_active` tells a reconnecting client whether to re-`subscribe` over the chat WebSocket |

**Tool result blocks.** `tool_result` blocks in history (and `tool_result`
frames on the chat WebSocket) carry the tool's return value in `output`. When a
plugin returns a large `octop_ui` payload, the backend offloads the envelope's
`data` field: `output` then contains the slim envelope with
`data_ref: "artifact"`, and the full payload is on the block's `artifact` key
(absent otherwise). An explicit `data` key always takes precedence over
`data_ref` when both appear. Clients that render plugin UIs must resolve
`data_ref` from `artifact`; clients that only read `output` keep working.

### Trajectory ledger

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/agents/{id}/threads/{thread_id}/trajectory` | owner | paginated event summaries; use `before_seq` for older rows |
| `GET` | `/agents/{id}/threads/{thread_id}/trajectory/events/{event_id}` | owner | full event payload |
| `GET` | `/agents/{id}/threads/{thread_id}/trajectory/metrics` | owner | aggregated turn, timing, and token metrics |
| `GET` | `/agents/{id}/threads/{thread_id}/trajectory/stream` | owner | live SSE events; resume with `after_seq` or `Last-Event-ID` |
| `GET` | `/agents/{id}/threads/{thread_id}/trajectory/export` | owner | full ledger download as JSONL (default) or JSON |

## Channels

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/agents/{aid}/channels` | owner | list of channel rows |
| `POST`   | `/agents/{aid}/channels` | owner | body `{kind, name, config}` → `201` |
| `GET`    | `/agents/{aid}/channels/{cid}` | owner | channel row |
| `PATCH`  | `/agents/{aid}/channels/{cid}` | owner | body subset → updated row |
| `DELETE` | `/agents/{aid}/channels/{cid}` | owner | `204` |
| `POST`   | `/agents/{aid}/channels/{cid}/test` | owner | `{ok, error?}` (instantiate → start → stop) |
| `POST`   | `/agents/{aid}/channels/probe` | owner | `{ok, reason?, detail?}` — preflight a candidate config |
| `POST`   | `/agents/{aid}/channels/{platform}/qrcode/generate` | owner | platform-specific bot creator (wecom, weixin, feishu, yuanbao) |
| `POST`   | `/agents/{aid}/channels/{platform}/qrcode/poll` | owner | poll bot creator state |
| `POST`   | `/agents/{aid}/channels/{platform}/bot-creator/start` | owner | start a bot-creator flow |
| `POST`   | `/agents/{aid}/channels/{platform}/bot-creator/poll` | owner | poll progress |
| `POST`   | `/agents/{aid}/channels/{platform}/bot-creator/stop` | owner | stop an in-flight bot creator |

## Cron

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/settings/timezone` | user | process-level `{timezone}` from `default_timezone` |
| `GET`    | `/settings/upload` | user | `{max_upload_mb, max_upload_bytes}` from `max_upload_mb` |
| `GET`    | `/settings/hitl` | user | `{enabled, tool_guard_require_approval, show_approval_ui}` — `show_approval_ui` is true when tool HITL is on or command guard is `require_approval` |
| `GET`    | `/settings/captcha` | `captcha` | `{active, available, providers, source, v3_min_score}`; secrets omitted |
| `PUT`    | `/settings/captcha` | `captcha` | merge `{active?, providers?}`; empty secret keeps ciphertext; `null` removes a pair |
| `GET`    | `/cron/settings` | user | compat alias of `/settings/timezone` |
| `GET`    | `/agents/{aid}/cron/examples` | agent access | `{task_examples}` from workspace `.octop/manifest.json` (`{zh,en}` string arrays, display-normalized to 3 or 6); `null` if the field is absent (dashboard keeps default cards). Prefer `GET /agents/{aid}/chat/welcome` which includes the same field. |
| `GET`    | `/agents/{aid}/cron` | owner only | list cron rows; non-owners (including admin) get `[]` |
| `POST`   | `/agents/{aid}/cron` | owner only | body `{name?, trigger, prompt, session_key?, fresh_thread?, enabled?, model?, task_type?}` → `201` |
| `GET`    | `/agents/{aid}/cron/{cid}` | owner only | cron row |
| `PATCH`  | `/agents/{aid}/cron/{cid}` | owner only | body subset → updated row |
| `DELETE` | `/agents/{aid}/cron/{cid}` | owner only | `204` |
| `POST`   | `/agents/{aid}/cron/{cid}/run-now` | owner only | `204` (fire immediately, off-schedule) |

`task_type` is `"text"` (push prompt directly to the session) or
`"agent"` (run the prompt through the LLM and push the reply).
Default: `"agent"`. `trigger` accepts cron expressions
(`"0 9 * * *"`) plus the `interval:N` / `date:ISO8601` aliases
documented in `infra/cron/trigger.py`. `prompt` must be non-empty and
≤ 2000 characters. `name` is an optional display label; when omitted,
the server derives one from `prompt`.

## Providers

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/providers` | user | providers visible to the user (own + shared) |
| `POST`   | `/providers` | user | body `{name, kind, base_url?, api_key?, model?, ...}` → `201` |
| `PATCH`  | `/providers/{id}` | owner | body subset → updated row |
| `DELETE` | `/providers/{id}` | owner | `204` (refuses if any agent references it) |
| `POST`   | `/providers/{id}/test` | user | `{ok, latency_ms?, error?}` (one-token ping with 10 s timeout) |
| `POST`   | `/admin/providers` | admin | same body as user POST; row has `user_id = NULL` |
| `PATCH`  | `/admin/providers/{id}` | admin | as user PATCH but works on shared rows |
| `DELETE` | `/admin/providers/{id}` | admin | `204` |

## Models

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/models/presets` | user | provider templates from `octop-harness` |
| `GET` | `/models` | user | resolved models across enabled providers |
| `GET` | `/models/active` | user | `{provider_name, model}` |
| `PUT` | `/models/active` | admin | body `{provider_name, model}` |

### Media generation models

These instance-wide endpoints require the `providers` permission (administrators bypass
permission checks). The Ark API key is write-only and encrypted at rest. Saving settings
reloads running agents so the image and video tools receive the new configuration.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/admin/media-generation` | providers | Return Volcengine Ark image/video settings; never returns the API key |
| `PUT` | `/admin/media-generation` | providers | Save enabled tools and Seedream/Seedance model IDs; an included API key is verified before saving |
| `POST` | `/admin/media-generation/test` | providers | Test credentials or a selected image/video model; model tests submit real, potentially billable requests |
| `GET` | `/admin/media-generation/chatgpt-image` | providers | Return the `chatgpt_image` switches (`enabled`, `admin_only`, `allow_credits`), whether a ChatGPT login is stored, and the live Codex allowance; generates nothing |
| `PUT` | `/admin/media-generation/chatgpt-image` | providers | Save those switches and reload running agents so the tool is mounted or removed |

## Voice

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`  | `/voice/presets` | user | voice provider presets |
| `GET`  | `/voice/providers` | user | user's voice providers |
| `GET`  | `/voice/active` | user | active TTS / STT configuration |
| `PUT`  | `/voice/active` | user | update active voice configuration |
| `POST` | `/voice/stt` | user | body `{audio, format?, language?}` → `{text, segments?}` |
| `POST` | `/voice/tts` | user | body `{text, voice?, format?}` → audio bytes |
| `GET`/`POST`/`PATCH`/`DELETE` | `/admin/voice/providers` | admin | admin voice provider CRUD |

## MBTI & personas

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/mbti/codes` | user | `[{code: "INTJ", ...}, ...]` (16 codes + `_default`) |
| `GET`    | `/mbti/codes/{code}` | user | full profile (dimensions, behaviour, UI metadata) |
| `GET`    | `/mbti/preview/{code}` | user | rendered persona template (legacy `/api/personas/{code}`) |
| `PUT`    | `/agents/{aid}/mbti` | owner | body `{code}` → apply persona and reload |
| `GET`    | `/personas` | user | `[{code}, ...]` (compat shim) |
| `GET`    | `/personas/{code}` | user | rendered template (compat shim) |

Persona content lives in `src/octop/infra/agents/persona/mbti_profiles.py` —
see [Personas](./personas.md).

## Experts

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/experts` | user | bundled expert catalog (includes `task_examples` `{zh,en}` when present) |
| `GET`    | `/experts/{expert_id}` | user | full expert template (SOUL.md, skills, files, `task_examples`) |
| `POST`   | `/agents/from-expert/{expert_id}` | user | body `{name, locale?, quick_prompts?, ...}` → `201`; optional `quick_prompts` overwrites workspace cards after seed |

Bundled experts live in `src/octop/infra/agents/experts/library/`
(en/zh divisions); the catalog is locale-aware via
`Accept-Language` / user preference.

## Workspace, skills, subagents, memory, files

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/agents/{aid}/workspace/tree` | owner | list dir (default `.`) |
| `GET`    | `/agents/{aid}/workspace/file` | owner | read a content file |
| `PUT`    | `/agents/{aid}/workspace/file` | owner | write a content file (via `BackendWorkspace`) |
| `DELETE` | `/agents/{aid}/workspace/file` | owner | delete a content file |
| `POST`   | `/agents/{aid}/workspace/rename` | owner | rename / move |
| `POST`   | `/agents/{aid}/workspace/upload` | owner | multipart upload → backend (not `max_upload_mb`) |
| `GET`    | `/agents/{aid}/workspace/download` | owner | download a file |
| `GET`    | `/agents/{aid}/workspace/glob` | owner | glob backend paths |
| `GET`    | `/agents/{aid}/workspace/grep` | owner | grep backend files |
| `GET`    | `/agents/{aid}/workspace/...` | owner | see `api/routers/workspace.py` |
| `POST`   | `/agents/{aid}/media/chatgpt-image` | owner | body `{prompt, aspect_ratio?, quality?}`: draw one PNG with the instance's ChatGPT login into `generated/image/` — same switches and allowance check as the `chatgpt_image` tool; a refusal or failure is still `200` with `status: "failed"` and an error code |
| `GET`    | `/agents/{aid}/skills` | owner | list installed skills |
| `PUT`    | `/agents/{aid}/skills/{slug}` | owner | enable / disable a skill |
| `GET`    | `/agents/{aid}/skills/hub/search` | user | Skill Hub search |
| `GET`    | `/agents/{aid}/skills/hub/rankings` | user | Skill Hub rankings |
| `POST`   | `/agents/{aid}/skills/hub/install` | owner | body `{slug, version?}` → `201` |
| `GET`    | `/subagent-catalog/divisions` | user | bundled subagent divisions |
| `GET`    | `/subagent-catalog` | user | bundled subagent catalog |
| `GET`    | `/subagent-catalog/{slug}` | user | full subagent definition |
| `GET`    | `/agents/{aid}/subagents` | owner | installed subagents for an agent |
| `POST`   | `/agents/{aid}/subagents` | owner | install a bundled subagent |
| `GET`    | `/agents/{aid}/heartbeat-config` | owner | read heartbeat YAML |
| `PUT`    | `/agents/{aid}/heartbeat-config` | owner | write heartbeat YAML |
| `GET`    | `/agents/{aid}/memory/daily` | owner | list daily memory files |
| `GET`    | `/agents/{aid}/memory/daily/{filename}` | owner | read one daily memory |
| `DELETE` | `/agents/{aid}/memory/daily/{filename}` | owner | delete one daily memory |
| `GET`/`POST` | `/memory/...` | user | memory API (dashboard memory tab) |

## ACP (Agent Client Protocol)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/acp` | user | current user's global runner list |
| `PUT`    | `/acp` | user | replace global runners |
| `GET`    | `/acp/{runner_name}` | user | one runner |
| `PUT`    | `/acp/{runner_name}` | user | upsert one runner |
| `DELETE` | `/acp/{runner_name}` | user | delete a custom runner (built-ins are protected) |
| `GET`    | `/agents/{aid}/acp` | owner | global runners + this agent's `tool_enabled` flag |
| `PUT`    | `/agents/{aid}/acp` | owner | update `tool_enabled` and optionally the global list |
| `PUT`    | `/agents/{aid}/acp/tool` | owner | toggle the `acp_runner` tool only |

See [ACP integration](./acp.md) for the runner object schema and the
Zed setup example.

## Storage backends

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`/`POST`/`PATCH`/`DELETE` | `/storage-backends` | user | per-user remote backend connections |
| `GET`/`POST`/`PATCH`/`DELETE` | `/admin/storage-backends` | admin | admin-managed backends |

## Host filesystem (dashboard)

Browse the host OS directory tree when configuring a local backend
`root_dir` (`local_shell` / `filesystem`). Authenticated users only;
sensitive mounts (`/proc`, `/sys`, `/dev`, `/etc`, `/root` on POSIX)
are rejected, except the process home and its subdirectories (so a
server running as root may use `/root` as the default `root_dir`).
Listing is single-level and capped; write probe runs only
for non-`/` paths.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/filesystem/dirs?path=<abs>` | user | `{path, entries: [{path, name}]}` — one directory level |
| `POST`   | `/filesystem/probe` | user | body `{path}` → `{ok, path?}` or `{ok: false, code, detail?}` (`not_directory`, `permission_denied`, `write_failed`, `not_allowed`) |
| `POST`   | `/filesystem/mkdir` | user | body `{path, base_name?}` → `{path, name}` — create child dir (`base_name` defaults to `New Folder`; collisions become `Name (2)`, …) |
| `POST`   | `/filesystem/rename` | user | body `{path, new_name}` → `{path, name}` — rename basename only |

## Connectors & OAuth

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/connectors/catalog` | user | connector catalog (Notion, Figma, …) |
| `GET`    | `/connectors/test-credentials` | user | preflight credentials |
| `GET`    | `/connector-instances` | user | list instances |
| `POST`   | `/connector-instances` | user | create instance |
| `GET`/`PATCH`/`DELETE` | `/connector-instances/{id}` | user | CRUD on an instance |
| `POST`   | `/connector-instances/{id}/test` | user | test a configured instance |
| `POST`   | `/connector-instances/{id}/refresh` | user | refresh OAuth tokens |
| `GET`    | `/connectors/auth/{kind}/info` | user | auth flow info |
| `GET`    | `/connectors/auth/{kind}/authorize-url` | user | build the authorize URL |
| `POST`   | `/connectors/auth/{kind}/exchange-code` | user | exchange auth code |
| `POST`   | `/connectors/oauth/start` | user | start OAuth (catalog or custom MCP via `target`) |
| `POST`   | `/connectors/oauth/{kind}/start` | user | legacy catalog OAuth start |
| `PUT`    | `/connectors/custom-mcp` | user | save custom MCP server map |
| `PATCH`  | `/connectors/custom-mcp/servers/{name}` | user | patch `enabled` / `default_open` on one server |
| `POST`   | `/connectors/custom-mcp/test` | user | probe a custom MCP server (inline spec or saved name) |
| `GET`    | `/connectors/oauth/callback` | public | OAuth redirect target |
| `GET`    | `/connectors/oauth/pending/{state_id}` | user | poll the OAuth result |

Custom MCP OAuth (streamable HTTP, public HTTPS URL only): Octop discovers the authorization
server from the MCP URL (401 / RFC 9728 protected-resource metadata), requires dynamic client
registration (DCR), stores encrypted tokens in the custom MCP spec, and injects `Authorization:
Bearer` when loading tools. Loopback and LAN MCP URLs may use HTTP and do not use remote OAuth
discovery.

## SEO workbench

Admin only. Paid DataForSEO calls (`research`, `serps`) require
`confirm_paid: true` and an `idempotency_key`; the same key with the same
input returns the earlier result instead of paying again. Deleting the bound
expert keeps the project and its evidence with `agent_id: null` until it is
rebound.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`/`POST` | `/seo/projects` | admin | list / create (bound to an owned, unshared expert) |
| `PATCH`  | `/seo/projects/{id}` | admin | body `{agent_id}` — rebind to another expert |
| `GET`/`PUT` | `/seo/provider` | admin | `{configured}`; `PUT {login, password}` stores encrypted credentials |
| `POST`   | `/seo/projects/{id}/research` | admin | paid keyword research → full run |
| `GET`    | `/seo/projects/{id}/runs` | admin | run summaries (per-seed `row_count`, no rows) |
| `GET`    | `/seo/projects/{id}/runs/{run_id}` | admin | one run with keyword rows |
| `POST`   | `/seo/projects/{id}/serps` | admin | body `{keyword, device, depth: 20\|100}` — paid Google SERP; a repeat within 12 h is served from the saved copy at no charge |
| `GET`    | `/seo/projects/{id}/serps?keyword=` | admin | snapshot summaries, newest first |
| `GET`    | `/seo/projects/{id}/serps/{serp_id}` | admin | one snapshot with result rows |
| `GET`/`PATCH` | `/seo/projects/{id}/context` | admin | project background; `PATCH {updates: [...]}` applies all ops or none |
| `GET`/`POST` | `/seo/projects/{id}/keywords` | admin | saved keywords (paged) / save rows from a run |
| `GET`    | `/seo/projects/{id}/report-context?run_id=` | admin | evidence and `context_hash` for an agent report |
| `GET`/`POST` | `/seo/projects/{id}/reports` | admin | report metadata / save an immutable HTML report |
| `GET`    | `/seo/projects/{id}/reports/{report_id}` | admin | report with HTML |
| `POST`   | `/seo/projects/{id}/audits` | admin | body `{idempotency_key, start_url?, max_pages: 10–200 (default 50)}` — queue a free technical site audit; one active audit per project (`409`) |
| `GET`    | `/seo/projects/{id}/audits` | admin | audits, newest first, with progress |
| `GET`    | `/seo/projects/{id}/audits/{audit_id}` | admin | one audit with `issue_counts` by severity |
| `GET`    | `/seo/projects/{id}/audits/{audit_id}/issues?severity=&issue_type=` | admin | per-type summary with explanation and fix (request language) and every affected URL |
| `GET`    | `/seo/projects/{id}/audits/{audit_id}/pages?fetch_class=&status_code=&url_contains=` | admin | crawled pages |
| `POST`   | `/seo/projects/{id}/audits/{audit_id}/cancel` | admin | queued: cancelled at once; running: stops after the current page, results so far kept |
| `DELETE` | `/seo/projects/{id}/audits/{audit_id}` | admin | delete the audit and its results (`204`) |
| `GET`    | `/seo/google` | admin | `{configured, redirect_uri, accounts}` — the callback URI to register and the admin's connected Google accounts |
| `POST`   | `/seo/google/connect` | admin | body `{return_path?}` → `{authorize_url}`; one consent covers read-only Search Console and Analytics |
| `GET`    | `/seo/google/callback` | public | Google's redirect; the single-use state (10 min) names the admin. `303` back to `return_path` with `seo_google=connected` or `seo_google_error=<reason>` |
| `DELETE` | `/seo/google/accounts/{grant_id}` | admin | disconnect; projects bound through it lose their property (`204`) |
| `GET`/`PUT`/`DELETE` | `/seo/projects/{id}/gsc` | admin | the bound Search Console property; `PUT {grant_id, site_url}` needs a verified property on that grant |
| `GET`    | `/seo/projects/{id}/gsc/sites` | admin | properties per connected account, with `requires_reconnect` / `api_disabled` |
| `POST`   | `/seo/projects/{id}/gsc/report` | admin | body `{date_range, device?, country?, page_filter?, query_filter?}` — totals vs the previous period, daily series, striking-distance queries (best page 5–20), countries |
| `POST`   | `/seo/projects/{id}/gsc/table` | admin | report filters plus `{dimension: query\|page, page, page_size ≤ 100}`; `has_next_page` from one extra row |
| `POST`   | `/seo/projects/{id}/gsc/inspect` | admin | body `{urls: 1–10, language_code?}` — Google's stored index status; one URL's error is reported next to it |
| `GET`/`PUT`/`DELETE` | `/seo/projects/{id}/ga4` | admin | the bound GA4 property; `PUT {grant_id, property_id: "properties/<n>"}` needs a property that grant can read |
| `GET`    | `/seo/projects/{id}/ga4/properties` | admin | properties per connected account, with `requires_reconnect` / `api_disabled` |
| `POST`   | `/seo/projects/{id}/ga4/overview` | admin | body `{start_date?, end_date?, trend: daily\|weekly}` — organic-search totals vs the previous equal-length period and a trend; days follow the property's time zone, default the last 28 complete days |
| `POST`   | `/seo/projects/{id}/ga4/report` | admin | body `{kind, start_date?, end_date?, channel, limit ≤ 1000, offset, …breakdowns}` — one page of a report kind (`landing_pages`, `page_performance`, `key_events`, `traffic_acquisition`, `ecommerce_performance`, `site_search`, `audience_breakdown`); `page_info.next_offset` continues it |

Site audits crawl on the server as `Octop-SEO-Audit/1.0`: robots.txt and its sitemaps are
followed, one page at a time (1 s apart, backing off on `429`), without running JavaScript, and
only to public addresses (every DNS answer is checked; redirects are not followed blindly). The
crawl is stored as it runs, so an audit interrupted by a restart or deploy resumes where it
stopped.

Search Console and GA4 need `OCTOP_SEO_GOOGLE_CLIENT_ID` and `OCTOP_SEO_GOOGLE_CLIENT_SECRET` (a Google
OAuth web client with `GET /seo/google`'s `redirect_uri` registered). Tokens are stored
encrypted per admin and refreshed on use; data is read live from Google on every call and
nothing else is stored. `409 SEO_GOOGLE_NOT_CONFIGURED`, `SEO_GOOGLE_NOT_CONNECTED` or
`SEO_GOOGLE_RECONNECT` say what to set up; `502 SEO_GOOGLE_API` carries Google's status.

## Internal MCP (octop-harness agents)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST`/`GET`/… | `/internal/mcp/*` | public (mTLS / network-isolated) | MCP gateway used by octop-harness agents (not the dashboard) |

## Observability & security

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`/`PUT` | `/admin/observability` | admin | Langfuse configuration (host, project, env) |
| `GET`    | `/admin/security` | admin | global security policy |
| `PUT`    | `/admin/security` | admin | update global policy |
| `GET`    | `/admin/security/tool-guard/rules` | admin | active command guard rules |
| `GET`    | `/admin/security/tool-guard/rules/raw` | admin | editable YAML |
| `PUT`    | `/admin/security/tool-guard/rules/raw` | admin | save YAML |
| `POST`   | `/admin/security/tool-guard/rules/reset` | admin | reset to shipped defaults |
| `GET`    | `/admin/security/defaults` | admin | defaults + rule catalogs |

## TLS (Let's Encrypt)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`  | `/admin/tls/status` | admin | current cert + issuance task status |
| `POST` | `/admin/tls/preflight` | admin | preflight (port 80, DNS) |
| `POST` | `/admin/tls/issue` | admin | start HTTP-01 issuance |

`GET /.well-known/acme-challenge/{token}` is the HTTP-01 challenge
endpoint (public, mounted directly in `api/app.py`).

## Browser, terminal, uploads

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `WS`/`POST`/`GET`/… | `/agents/{aid}/terminal` | owner | AI-assisted remote PTY |
| `GET` | `/agents/{aid}/terminal/context` | owner | recent terminal context for the AI helper |
| `WS`/`POST`/`GET`/… | `/browser/...` | user | octop-browser sessions, live stream, record/replay |
| `POST` | `/browser/shutdown` | user | stop the current user's Octop-managed Chrome |
| `POST` | `/agents/{aid}/upload` | user | multipart upload → `{workspace}/inbound/` |
| `POST` | `/agents/{aid}/files/access-urls` | user | refresh inbound media URLs (signed) |
| `GET`  | `/agents/{aid}/files/{path}` | owner | read an inbound file |

## Updates, ollama, i18n, plugins, slash, preferences

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`/`POST` | `/update/status`, `/check`, `/upgrade`, `/progress`, `/restart` | admin | in-place server update flow |
| `GET`/`POST`/`DELETE` | `/ollama-models/...` | `ollama_models` | Ollama model discovery + downloads |
| `GET`/`PUT` | `/ollama-models/service` | `ollama_models` | Local daemon toggle; omit `enabled` to set `models_dir` only |
| `GET` | `/i18n/tools` | user | server-owned tool display names (locale-aware) |
| `GET` | `/i18n/locales` | public | available locales + fallback chain |
| `GET` | `/i18n/locales/{locale}/{namespace}` | public | one namespace bundle (errors, tools, channel, slash) |
| `GET`/`POST` | `/preferences` | user | UI preferences (per-user key/value) |
| `GET` | `/slash/commands` | user | slash command catalog for the composer menu |
| `GET`/`POST` | `/plugins` | user | installed plugin list / install flow |
| `POST` | `/plugins/reload` | admin (`plugins`) | reload plugins from disk into process |
| `PATCH` | `/plugins/{id}` | admin (`plugins`) | enable/disable plugin (`{ "enabled": bool }`) |
| `GET` | `/plugins/{id}/ui/{path}` | user | serve prebuilt plugin UI assets (`ui/dist/…`) |
| `DELETE` | `/plugins/{id}` | admin (`plugins`) | uninstall plugin |

## Usage & admin

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/usage/summary` | user | token usage summary for the current user |
| `GET`    | `/admin/usage` | admin | global token usage summary |
| `GET`    | `/admin/overview` | admin | `{user_count, agent_count, ...}` |
| `GET`    | `/admin/audit-log` | admin | recent audit rows |
| `GET`    | `/admin/metrics` | admin | `{messages_total, stream_errors_total, cron_runs_total, cron_errors_total, agent_active, ...}` |

## Envs

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`/`PUT` | `/agents/{aid}/envs` | owner | env-var preset for an agent's tool calls |
| `GET`/`POST`/`DELETE` | `/envs/presets` | user | reusable presets |

## Error envelope

Every non-2xx JSON response uses:

```json
{ "error": { "code": "AGENT_NOT_FOUND", "message": "...", "details": { } } }
```

`code` matches the `ErrorCode` enum in `octop.infra.errors`; the
server-localized `message` is rendered by `OctopError.to_envelope`
with the locale from `Accept-Language` (falling back to `en`). The
dashboard mirrors every code under `apiErrors.*` in
`dashboard/src/locales/{en,zh}.json`.

| Code | HTTP | Meaning |
|------|------|---------|
| `AUTH_FAILED` | 401 | Bad credentials |
| `TOKEN_EXPIRED` | 401 | JWT past its TTL |
| `LOGIN_LOCKED` | 423 | Too many failed attempts — wait `login_lockout_seconds` or call `/users/{id}/unlock-login` |
| `SETUP_REQUIRED` | 409 | Initial admin not yet created (or wizard not finished) |
| `FORBIDDEN` | 403 | Authenticated but not allowed |
| `NOT_FOUND` | 404 | No such row / route |
| `USER_DISABLED` | 403 | Account flag flipped off |
| `USERNAME_TAKEN` | 409 | Conflict on `users.username` |
| `AGENT_NOT_FOUND` | 404 | Agent row missing or owned by another user |
| `AGENT_FAILED` | 500 | Runtime errored during a call |
| `AGENT_BUSY` | 409 | Operation refused while another is in flight |
| `PROVIDER_NAME_TAKEN` | 409 | Conflict on `providers.name` |
| `PROVIDER_NOT_VISIBLE` | 400 | Agent config references a provider the user can't see |
| `PROVIDER_REFERENCED` | 409 | Delete blocked because agents still reference the row |
| `PROVIDER_TEST_FAILED` | 400 | `/providers/{id}/test` failed |
| `CHANNEL_KIND_UNSUPPORTED` | 400 | `kind` not in registered builders |
| `CHANNEL_INVALID_CREDENTIALS` | 400 | Channel config rejected by the platform |
| `CHANNEL_PROBE_INCOMPLETE` | 400 | Probe couldn't reach the platform |
| `CRON_TRIGGER_INVALID` | 400 | Trigger string did not parse |
| `CRON_PROMPT_INVALID` | 400 | Empty or too-long prompt |
| `SLASH_UNKNOWN` | 400 | `/<cmd>` is not a registered handler |
| `SLASH_BAD_ARGS` | 400 | Slash handler rejected its arguments |
| `ATTACHMENT_UNSUPPORTED_TYPE` | 400 | Chat / inbound attachment rejected (media type not allowed) |
| `ATTACHMENT_TOO_LARGE` | 413 | Chat / inbound attachment exceeds the configured size limit (`max_mb` in details) |
| `WORKSPACE_PATH_INVALID` | 400 | Path outside the agent's workspace |
| `STORAGE_BACKEND_UNREACHABLE` | 502 | Remote backend connect / list failed |
| `CONNECTOR_OAUTH_FAILED` | 400 | OAuth flow could not complete |
| `INTERNAL_ERROR` | 500 | Unhandled exception (logged with traceback) |
