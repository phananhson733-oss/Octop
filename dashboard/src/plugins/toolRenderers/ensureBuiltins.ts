import { DefaultToolRenderer } from "./builtin/DefaultToolRenderer";
import { SeoReportRenderer } from "./builtin/SeoReportRenderer";
import { builtinPluginHost } from "./host";
import { exposeReactGlobals } from "./reactGlobals";
import { registerToolRenderer } from "./registry";

let builtinsRegistered = false;

/** Register first-party fallback renderer once. */
export function ensureBuiltinToolRenderers(): void {
  if (builtinsRegistered) return;
  builtinsRegistered = true;
  exposeReactGlobals();
  registerToolRenderer({
    id: "default",
    pluginId: "builtin",
    component: DefaultToolRenderer,
  });
  registerToolRenderer({
    id: "seo_report",
    pluginId: "builtin",
    tools: ["seo_save_report"],
    component: SeoReportRenderer,
  });
  void builtinPluginHost;
}
