import { parseApiError } from "../../../utils/apiError";
import { isNetworkFetchError } from "../../../utils/networkError";

/**
 * True when a request never got an answer from Octop itself: the network
 * failed, or the host in front of it answered 502/503/504 without an Octop
 * error envelope (e.g. Railway's "Application failed to respond" while the
 * container restarts).
 */
export function isServerUnreachable(error: unknown): boolean {
  if (isNetworkFetchError(error)) return true;
  if (!(error instanceof Error)) return false;
  const status = /^Request failed: (\d{3})\b/.exec(error.message)?.[1];
  return (
    (status === "502" || status === "503" || status === "504") &&
    !parseApiError(error)?.code
  );
}
