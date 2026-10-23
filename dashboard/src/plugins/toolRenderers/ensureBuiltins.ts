import { DefaultToolRenderer } from "./builtin/DefaultToolRenderer";
import { SeoPurchaseRenderer } from "./builtin/SeoPurchaseRenderer";
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
  registerToolRenderer({
    id: "seo_purchase",
    pluginId: "builtin",
    tools: [
      "seo_propose_research",
      "seo_propose_serp",
      "seo_propose_competitor_lookup",
      "seo_propose_rank_check",
      "seo_propose_ai_check",
      "seo_propose_prompt_lookup",
      "seo_propose_brand_lookup",
    ],
    component: SeoPurchaseRenderer,
  });
  void builtinPluginHost;
}
