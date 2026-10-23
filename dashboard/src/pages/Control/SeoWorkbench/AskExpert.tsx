import { createContext, useContext, useState } from "react";
import { Button } from "antd";
import { useTranslation } from "react-i18next";
import type { NavigateFunction } from "react-router-dom";
import { octopThreadsApi } from "../../../api/modules/octopThreads";

/**
 * Opens a new chat with ``agentId``: with the request in the input
 * (``prefillInput``) or sent on arrival (``autoSendInput``).
 */
export async function openExpertChat(
  navigate: NavigateFunction,
  agentId: string,
  state: { prefillInput: string } | { autoSendInput: string },
) {
  const thread = await octopThreadsApi.create(agentId);
  navigate(
    `/chat/${encodeURIComponent(agentId)}/${encodeURIComponent(
      thread.thread_id,
    )}`,
    { state },
  );
}

/**
 * Opens a new chat with this account's expert for the project and sends it a
 * request to analyze ``what``; null when the account has no such expert.
 */
export const AskExpertContext = createContext<
  ((what: string) => Promise<void>) | null
>(null);

/** 交给专家分析: ``what`` names the result and the tool that reads it. */
export function AskExpertButton({
  what,
  disabled,
}: {
  what: string;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const ask = useContext(AskExpertContext);
  const [busy, setBusy] = useState(false);
  if (!ask) return null;
  return (
    <Button
      disabled={disabled}
      loading={busy}
      onClick={() => {
        setBusy(true);
        void ask(what).finally(() => setBusy(false));
      }}
    >
      {t("seo.askExpert")}
    </Button>
  );
}
