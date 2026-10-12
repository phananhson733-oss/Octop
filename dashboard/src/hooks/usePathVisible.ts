import { useCallback } from "react";
import { resolveSelectedKey } from "../routes/navKey";
import { navKeyVisible } from "../utils/permissions";
import { useCurrentUser } from "./useCurrentUser";

/** Whether a link to ``path`` leads to a page the role's sidebar limit leaves visible. */
export function usePathVisible(): (path: string) => boolean {
  const user = useCurrentUser();
  return useCallback(
    (path: string) =>
      navKeyVisible(user, resolveSelectedKey(path.split(/[?#]/)[0])),
    [user],
  );
}
