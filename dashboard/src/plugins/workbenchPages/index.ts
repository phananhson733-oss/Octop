export {
  BUILTIN_WORKBENCH_TABS,
  getWorkbenchPages,
  registerWorkbenchPage,
  unregisterWorkbenchPages,
  workbenchPageSlug,
  workbenchPageTitle,
  type WorkbenchPluginPage,
} from "./registry";
export { workbenchAccess, workbenchTabOf } from "./access";
export {
  useInstalledPluginUis,
  usePluginUisSettled,
  useWorkbenchPages,
} from "./useWorkbenchPages";
export { PluginPageBoundary } from "./PluginPageBoundary";
