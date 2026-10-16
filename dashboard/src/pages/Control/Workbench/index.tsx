import { Suspense, useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Globe, LayoutGrid, TerminalSquare } from "lucide-react";
import PageShell from "../../../layouts/PageShell";
import PageLoading from "../../../components/PageLoading";
import { usePathTabs } from "../../../hooks/usePathTabs";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import { userCan } from "../../../utils/permissions";
import { createPluginUiHost } from "../../../plugins/toolRenderers";
import {
  BUILTIN_WORKBENCH_TABS,
  PluginPageBoundary,
  usePluginUisSettled,
  useWorkbenchPages,
  workbenchPageTitle,
  type WorkbenchPluginPage,
} from "../../../plugins/workbenchPages";
import TerminalPage from "../Terminal";
import RemoteBrowserPage from "../RemoteBrowser";
import styles from "./index.module.less";

export type BuiltinWorkbenchTab = (typeof BUILTIN_WORKBENCH_TABS)[number];
/** A built-in tab, or the slug of a page a plugin registered. */
export type WorkbenchTab = string;

const TAB_ICONS = {
  browser: Globe,
  terminal: TerminalSquare,
} as const;

function isBuiltinTab(tab: string): tab is BuiltinWorkbenchTab {
  return (BUILTIN_WORKBENCH_TABS as readonly string[]).includes(tab);
}

interface WorkbenchPageProps {
  /** True when the workbench keep-alive surface is currently shown. */
  isVisible?: boolean;
}

function PluginPagePanel({
  page,
  title,
  active,
  isVisible,
}: {
  page: WorkbenchPluginPage;
  title: string;
  active: boolean;
  isVisible: boolean;
}) {
  const host = useMemo(
    () => createPluginUiHost(page.pluginId),
    [page.pluginId],
  );
  const Page = page.component;
  return (
    <div
      className={`${styles.panel} ${styles.pluginPanel}`}
      style={{ display: active ? "flex" : "none" }}
      aria-hidden={!active}
    >
      <PluginPageBoundary title={title}>
        <Suspense fallback={<PageLoading />}>
          <Page
            pluginId={page.pluginId}
            pageId={page.id}
            host={host}
            isVisible={isVisible}
          />
        </Suspense>
      </PluginPageBoundary>
    </div>
  );
}

export default function WorkbenchPage({
  isVisible = true,
}: WorkbenchPageProps) {
  const { t, i18n } = useTranslation();
  const user = useCurrentUser();
  const pluginPages = useWorkbenchPages();
  const pluginUisSettled = usePluginUisSettled();
  const isAllowed = useCallback(
    (tab: WorkbenchTab) => (isBuiltinTab(tab) ? userCan(user, tab) : true),
    [user],
  );
  const tabs = useMemo(
    () => [...BUILTIN_WORKBENCH_TABS, ...pluginPages.map((page) => page.slug)],
    [pluginPages],
  );
  const defaultTab: WorkbenchTab =
    tabs.find((tab) => isAllowed(tab)) ?? "terminal";

  const { activeTab, pathTab, handleTabChange, isMounted } = usePathTabs({
    basePath: "/workbench",
    tabs,
    storageKey: "octop:workbench:tab",
    defaultTab,
    isAllowed,
    ready: pluginUisSettled,
  });

  const pageTitles = useMemo(
    () =>
      new Map(
        pluginPages.map((page) => [
          page.slug,
          workbenchPageTitle(page.title, i18n.language),
        ]),
      ),
    [pluginPages, i18n.language],
  );
  const tabLabel = useCallback(
    (tab: WorkbenchTab) =>
      isBuiltinTab(tab)
        ? t(`workbench.tabs.${tab}`)
        : pageTitles.get(tab) ?? tab,
    [pageTitles, t],
  );

  const pathTabs = useMemo(
    () => ({
      value: activeTab,
      onChange: handleTabChange,
      options: tabs
        .filter((value) => isAllowed(value))
        .map((value) => {
          const Icon = isBuiltinTab(value) ? TAB_ICONS[value] : LayoutGrid;
          return {
            value,
            label: tabLabel(value),
            icon: <Icon size={14} strokeWidth={2} />,
          };
        }),
    }),
    [activeTab, handleTabChange, isAllowed, tabLabel, tabs],
  );

  // Bare or not-yet-registered URL while plugin UIs load: which tab this is, is not known yet.
  const resolving = !pluginUisSettled && pathTab === null;
  const browserVisible = isVisible && activeTab === "browser";
  const terminalVisible = isVisible && activeTab === "terminal";
  const activePage = pluginPages.find((page) => page.slug === activeTab);
  const pageTitle = resolving
    ? t("workbench.title")
    : `${t("workbench.title")} / ${tabLabel(activeTab)}`;
  const subtitle = activePage
    ? activePage.description
      ? workbenchPageTitle(activePage.description, i18n.language)
      : undefined
    : t("workbench.description");

  return (
    <PageShell
      title={pageTitle}
      subtitle={resolving ? undefined : subtitle}
      fill
      pathTabs={pathTabs.options.length > 1 ? pathTabs : undefined}
    >
      <div className={styles.panels}>
        {resolving && <PageLoading />}
        {isMounted("browser") && (
          <div
            className={styles.panel}
            style={{ display: activeTab === "browser" ? "flex" : "none" }}
            aria-hidden={activeTab !== "browser"}
          >
            <RemoteBrowserPage embedded isVisible={browserVisible} />
          </div>
        )}
        {isMounted("terminal") && (
          <div
            className={styles.panel}
            style={{ display: activeTab === "terminal" ? "flex" : "none" }}
            aria-hidden={activeTab !== "terminal"}
          >
            <TerminalPage embedded isVisible={terminalVisible} />
          </div>
        )}
        {pluginPages.map(
          (page) =>
            isMounted(page.slug) && (
              <PluginPagePanel
                key={page.slug}
                page={page}
                title={tabLabel(page.slug)}
                active={activeTab === page.slug}
                isVisible={isVisible && activeTab === page.slug}
              />
            ),
        )}
      </div>
    </PageShell>
  );
}
