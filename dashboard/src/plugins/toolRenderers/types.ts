import type { ComponentType } from "react";

/** Parsed ``octop_ui`` envelope from a tool JSON string output. */
export interface OctopUiHint {
  renderer: string;
  version?: number;
}

export interface ParsedToolOutput {
  /** True when ``output`` parsed as JSON object. */
  isJson: boolean;
  raw: unknown;
  octopUi?: OctopUiHint;
  data?: unknown;
  text?: string;
}

export type ToolRenderStatus = "running" | "done" | "error";

export interface ToolRenderContext {
  agentId: string | null;
  threadId: string | null;
  locale: string;
  theme: "light" | "dark";
}

export interface ToolRenderProps {
  pluginId: string;
  toolName: string;
  displayName?: string;
  callId?: string;
  status: ToolRenderStatus;
  args: unknown;
  /** Parsed ``data`` field, or full parsed object / raw string. */
  data: unknown;
  textFallback?: string;
  host: OctopPluginUIHost;
  /** Original tool output string (for builtins that need media parsing). */
  output?: string;
  isStreaming: boolean;
  hideMediaPreview?: boolean;
  onAcpPermissionSelect?: (message: string) => void;
  agentId?: string | null;
}

export interface ToolRendererRegistration {
  /** Plugin-local renderer id (matches ``octop_ui.renderer``). */
  id: string;
  /** Owning plugin id (``builtin`` for first-party renderers). */
  pluginId: string;
  /** Tool names this renderer handles when ``octop_ui`` is absent. */
  tools?: string[];
  component: ComponentType<ToolRenderProps>;
}

/** Plain text, or one label per language (``zh`` / ``en``). */
export type WorkbenchPageTitle = string | Record<string, string>;

export interface WorkbenchPageProps {
  pluginId: string;
  pageId: string;
  host: OctopPluginUIHost;
  /** False while another Workbench tab or another route is showing. */
  isVisible: boolean;
}

/** A full-width page a plugin adds to the Workbench as its own tab. */
export interface WorkbenchPageRegistration {
  /** Plugin-local page id; with the plugin id it forms the URL segment. */
  id: string;
  pluginId: string;
  title: WorkbenchPageTitle;
  /** Shown under the page title while this tab is active. */
  description?: WorkbenchPageTitle;
  component: ComponentType<WorkbenchPageProps>;
  /** Module permission key an account needs to see the tab, its URL and the sidebar entry it adds (admins always do). UI only. */
  permission?: string;
}

export interface OpenSidePanelOptions {
  callId: string;
  title?: string;
  toolName?: string;
}

export interface OctopPluginUIHost {
  registerRenderer(
    reg: Omit<ToolRendererRegistration, "pluginId"> & { pluginId?: string },
  ): void;
  getToolContext(): ToolRenderContext;
  /** L2: update display payload for a tool call without re-running the LLM. */
  patchResult(callId: string, nextData: unknown): void;
  /** Authenticated Octop API request (path starts with ``/`` under ``/api``). */
  request<T = unknown>(path: string, init?: RequestInit): Promise<T>;
  /**
   * Authenticated binary download (images, files), e.g.
   * ``/agents/{id}/media/preview?source=…``. ``request`` reads non-JSON bodies as text.
   */
  requestBlob(path: string, init?: RequestInit): Promise<Blob>;
  /**
   * Authenticated multipart upload (POST by default), e.g.
   * ``/agents/{id}/workspace/upload?path=…&from_workspace=true``. ``request``
   * always sends ``Content-Type: application/json``, which breaks ``FormData``.
   */
  upload<T = unknown>(
    path: string,
    body: FormData,
    init?: RequestInit,
  ): Promise<T>;
  /** Open this tool call's UI in the chat side dock (one tab per callId). */
  openSidePanel(opts: OpenSidePanelOptions): void;
  /** Close the side dock tab for this tool call, if open. */
  closeSidePanel(callId: string): void;
  /**
   * Add a tab to the Workbench that renders ``component`` full width. It has
   * no tool call behind it: the page gets its data through ``request``.
   */
  registerWorkbenchPage(
    reg: Omit<WorkbenchPageRegistration, "pluginId"> & { pluginId?: string },
  ): void;
}

/** Shape expected from ``ui/dist/index.js``. */
export interface PluginUiModule {
  setup?: (host: OctopPluginUIHost) => void;
  default?: { setup?: (host: OctopPluginUIHost) => void };
}

export interface PluginUiManifest {
  renderers?: Array<{
    id: string;
    tools?: string[];
  }>;
}
