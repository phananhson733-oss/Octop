import { useLocation } from "react-router-dom";
import ForbiddenPage from "../../../components/ForbiddenPage";
import PageLoading from "../../../components/PageLoading";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import {
  usePluginUisSettled,
  useWorkbenchPages,
  workbenchAccess,
} from "../../../plugins/workbenchPages";

/**
 * Route guard for the keep-alive Workbench. Same rule as RequirePermission for
 * terminal and browser; pages added by plugins are open to every signed-in
 * account, and an account that only has those waits for plugin UIs to load
 * instead of seeing "no permission" first.
 */
export default function WorkbenchGate({
  children,
}: {
  children: React.ReactNode;
}) {
  const user = useCurrentUser();
  const location = useLocation();
  const pages = useWorkbenchPages();
  const settled = usePluginUisSettled();
  const access = workbenchAccess(
    user,
    location.pathname,
    pages.map((page) => page.slug),
    settled,
  );
  if (access === "pending") return <PageLoading />;
  if (access === "deny") return <ForbiddenPage />;
  return <>{children}</>;
}
