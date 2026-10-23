import { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";

/**
 * Sends a hand-off from another page (router state ``autoSendInput``, e.g. the
 * SEO workbench's "ask the expert") once as the user's message, after its
 * thread has loaded and the agent can chat. The request stays in the router
 * state until then, so a reload before it is sent picks it up again; the
 * location key keeps a re-run effect from taking the same navigation twice.
 */
export function useHandOffSend({
  threadId,
  ready,
  failed,
  send,
  fallback,
}: {
  threadId: string | null;
  /** The agent can chat and the thread's history is loaded, with no turn running. */
  ready: boolean;
  /** The thread's history did not load. */
  failed: boolean;
  send: (text: string) => void;
  /** Leaves the request in the input to send by hand. */
  fallback: (text: string) => void;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const takenKeyRef = useRef<string | null>(null);
  useEffect(() => {
    const text = (location.state as { autoSendInput?: string } | null)
      ?.autoSendInput;
    if (!text || !threadId || takenKeyRef.current === location.key) return;
    if (!failed && !ready) return;
    takenKeyRef.current = location.key;
    navigate(location.pathname, { replace: true, state: {} });
    if (failed) fallback(text);
    else send(text);
  }, [
    location.state,
    location.key,
    location.pathname,
    navigate,
    threadId,
    ready,
    failed,
    send,
    fallback,
  ]);
}
