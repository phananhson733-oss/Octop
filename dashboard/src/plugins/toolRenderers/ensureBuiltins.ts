import { DefaultToolRenderer } from "./builtin/DefaultToolRenderer";
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
  void builtinPluginHost;
}
