import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { seoApi, type AiEngine, type LlmModel } from "../../../api/modules/seo";
import { apiErrorMessage } from "../../../utils/apiError";
import { brandEstimateUsd, promptEstimate } from "../../../utils/seoCosts";
import type { ToolRenderProps } from "../types";
import { DefaultToolRenderer } from "./DefaultToolRenderer";

const { Text, Paragraph } = Typography;
const POLL_MS = 3000;
// A check still running after this is reported to the expert as it stands.
const MAX_WAIT_MS = 15 * 60_000;
const STORE = "octop.seoPurchase.";
const KINDS = [
  "research",
  "serp",
  "lookup",
  "rank_check",
  "ai_check",
  "prompt",
  "brand",
] as const;
type Kind = (typeof KINDS)[number];

/** A paid request the SEO expert proposed (``seo_propose_*``), as the tool returned it. */
interface Purchase {
  kind: Kind;
  project_id: string;
  idempotency_key: string;
  /** Validated by the tool against the route's model. */
  input: Record<string, unknown>;
  tracker_id?: string;
  domain?: string;
  checks?: number;
  keywords?: string[];
  keyword_count?: number;
  engines?: AiEngine[];
  answers?: number;
  prompts?: string[];
  prompt_count?: number;
}

interface Proposal {
  purchase: Purchase;
  /** The server's signature of ``purchase``; only a signed purchase can be bought. */
  signature: string;
}

interface Bought {
  id: string;
  status: string;
}

// Bought is saved as soon as the purchase is made, and notified once the expert is told.
type Outcome =
  | ({ state: "bought"; notified: boolean } & Bought)
  | { state: "declined" };

function parseProposal(output: string | undefined): Proposal | null {
  if (!output) return null;
  try {
    const { purchase, signature } = JSON.parse(output) as Partial<Proposal>;
    if (
      !purchase ||
      typeof signature !== "string" ||
      !/^[0-9a-f]{64}$/.test(signature) ||
      !KINDS.includes(purchase.kind) ||
      typeof purchase.project_id !== "string" ||
      typeof purchase.idempotency_key !== "string" ||
      typeof purchase.input !== "object" ||
      purchase.input === null
    )
      return null;
    return { purchase, signature };
  } catch {
    return null;
  }
}

// The card is redrawn from the tool output after a reload; this browser
// remembers what was clicked so the buttons do not come back.
function readOutcome(key: string): Outcome | null {
  try {
    const raw = localStorage.getItem(STORE + key);
    const outcome = raw ? (JSON.parse(raw) as Outcome) : null;
    return outcome?.state === "bought" || outcome?.state === "declined"
      ? outcome
      : null;
  } catch {
    return null;
  }
}

function saveOutcome(key: string, outcome: Outcome) {
  try {
    localStorage.setItem(STORE + key, JSON.stringify(outcome));
  } catch {
    // Without storage the card only forgets the click on reload; a second
    // click reads the same attempt through the idempotency key.
  }
}

const usd = (value: number) => `$${Number(value.toFixed(4))}`;
const list = (items: unknown) =>
  Array.isArray(items) ? items.map(String).join(", ") : "";

/** The click is the confirmation; the server buys exactly the signed purchase. */
async function buy({ purchase, signature }: Proposal): Promise<Bought> {
  const bought = await seoApi.confirmProposal(purchase.project_id, {
    purchase: { ...purchase },
    signature,
  });
  return { id: bought.record_id, status: bought.status };
}

/** Wait for a rank or AI check to finish, as its tab does. */
async function settle(
  p: Purchase,
  started: Bought,
  alive: () => boolean,
): Promise<Bought> {
  const deadline = Date.now() + MAX_WAIT_MS;
  let status = started.status;
  while (status === "running" && alive() && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    if (!alive()) break;
    try {
      if (p.kind === "rank_check") {
        const { run } = await seoApi.rankResults(
          p.project_id,
          p.tracker_id ?? "",
          "7d",
        );
        // One check runs per tracker at a time: a newer one means ours ended.
        if (run && run.run_id !== started.id) status = "ended";
        else if (run) status = run.status;
      } else {
        const { run } = await seoApi.aiVisibility(p.project_id, {
          run_id: started.id,
          branded: "neutral",
          gap: false,
        });
        if (run?.run_id === started.id) status = run.status;
      }
    } catch {
      // The purchase stands; a failed read only means asking again.
    }
  }
  return { id: started.id, status };
}

/**
 * ``seo_propose_*``: a card with what the expert wants to buy and what it may
 * cost. Nothing is bought until the user clicks; then the expert is told.
 */
export function SeoPurchaseRenderer(props: ToolRenderProps) {
  const { t } = useTranslation();
  const proposal =
    props.status === "done" && props.toolName.startsWith("seo_propose_")
      ? parseProposal(props.output)
      : null;
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [phase, setPhase] = useState<"idle" | "buying" | "waiting" | "queued">(
    "idle",
  );
  const [error, setError] = useState("");
  const alive = useRef(true);
  // A check settles minutes after the click: tell the expert through the
  // chat as it is then, not as it was when the card was drawn.
  const sendRef = useRef(props.onAcpPermissionSelect);
  useEffect(() => {
    sendRef.current = props.onAcpPermissionSelect;
  });
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );
  if (!proposal) return <DefaultToolRenderer {...props} />;

  const { purchase, signature } = proposal;
  const input = purchase.input;
  const shown = outcome ?? readOutcome(purchase.idempotency_key);
  const what = t(
    `seo.purchase.kind.${
      purchase.kind === "lookup"
        ? `lookup_${String(input.kind)}`
        : purchase.kind
    }`,
  );
  const on = (value: unknown) =>
    value ? t("seo.purchase.on") : t("seo.purchase.off");
  const preview = (items: string[] | undefined, total: number | undefined) =>
    list(items) +
    (total && items && total > items.length
      ? ` ${t("seo.purchase.more", { count: total - items.length })}`
      : "");
  const details: [string, string][] = [];
  let cost = "";
  switch (purchase.kind) {
    case "research":
      details.push(
        [t("seo.purchase.keywords"), list(input.keywords)],
        [t("seo.mode"), t(`seo.mode_${String(input.mode)}`)],
        [t("seo.purchase.resultLimit"), String(input.result_limit)],
      );
      if (input.clickstream)
        details.push([t("seo.clickstream"), on(input.clickstream)]);
      if (input.group_keywords)
        details.push([t("seo.group"), on(input.group_keywords)]);
      cost = t("seo.purchase.researchCost");
      break;
    case "serp":
      details.push(
        [t("seo.purchase.keywords"), String(input.keyword)],
        [t("seo.purchase.device"), String(input.device)],
        [t("seo.purchase.depth"), String(input.depth)],
      );
      cost = t("seo.serpPaidConfirm");
      break;
    case "lookup": {
      details.push(
        input.domain
          ? [t("seo.purchase.target"), String(input.domain)]
          : [t("seo.purchase.keywords"), list(input.keywords)],
      );
      const help = {
        domain: "seo.lookupDomainHelp",
        competitors: "seo.lookupCompetitorsHelp",
        pages: "seo.lookupPagesHelp",
      }[String(input.kind)];
      cost = help
        ? `${t(help)} ${t("seo.lookupPaidConfirm")}`
        : t("seo.backlinksPaidConfirm");
      break;
    }
    case "rank_check":
      details.push(
        [t("seo.purchase.target"), purchase.domain ?? ""],
        [
          t("seo.purchase.keywords"),
          preview(purchase.keywords, purchase.keyword_count),
        ],
        [t("seo.purchase.checks"), String(purchase.checks ?? "")],
      );
      cost = t("seo.rankPaidConfirm", {
        checks: purchase.checks,
        cost: usd(Number(input.max_cost_usd)),
      });
      break;
    case "ai_check":
      details.push(
        [t("seo.purchase.engines"), list(purchase.engines)],
        [
          t("seo.purchase.prompts"),
          preview(purchase.prompts, purchase.prompt_count),
        ],
        [t("seo.purchase.answers"), String(purchase.answers ?? "")],
      );
      cost = t("seo.aiPaidConfirm", {
        answers: purchase.answers,
        cost: usd(Number(input.max_cost_usd)),
      });
      break;
    case "prompt": {
      const models = (input.models ?? []) as LlmModel[];
      const webSearch = input.web_search !== false;
      const estimate = promptEstimate(
        models,
        webSearch,
        typeof input.country === "string" ? input.country : "",
      );
      details.push(
        [t("seo.purchase.prompt"), String(input.prompt)],
        [t("seo.purchase.models"), list(models)],
        [t("seo.promptWebSearch"), on(webSearch)],
      );
      if (typeof input.country === "string")
        details.push([t("seo.purchase.country"), input.country]);
      cost = t("seo.promptPaidConfirm", {
        count: estimate.charged.length,
        cost: estimate.usd,
      });
      break;
    }
    case "brand": {
      const rivals = Array.isArray(input.competitors) ? input.competitors : [];
      details.push([t("seo.purchase.target"), String(input.target)]);
      if (rivals.length)
        details.push([t("seo.purchase.competitors"), list(rivals)]);
      cost = t("seo.brandPaidConfirm", {
        cost: brandEstimateUsd(rivals.length),
      });
      break;
    }
  }

  function remember(next: Outcome) {
    saveOutcome(purchase.idempotency_key, next);
    if (alive.current) setOutcome(next);
  }

  // Only ids and the outcome go back: proposal text reaching the chat as the
  // user's own words would read as the user's instructions.
  async function tellExpert(bought: Bought) {
    let final = bought;
    if (
      (purchase.kind === "rank_check" || purchase.kind === "ai_check") &&
      bought.status === "running"
    ) {
      setPhase("waiting");
      final = await settle(purchase, bought, () => alive.current);
    }
    // Unmounted while waiting: the card offers to tell the expert when it is back.
    if (!alive.current) return;
    const sent = sendRef.current?.(
      t("seo.purchase.doneMessage", {
        what,
        id: final.id,
        status: final.status,
      }),
    );
    // Queued behind a running turn: a reload drops the queue, so the card
    // offers to tell the expert again after one.
    remember({
      state: "bought",
      notified: sent !== false,
      id: final.id,
      status: final.status,
    });
    setPhase(sent === false ? "queued" : "idle");
  }

  async function confirm() {
    setPhase("buying");
    setError("");
    let bought: Bought;
    try {
      bought = await buy({ purchase, signature });
    } catch (err) {
      if (alive.current) {
        setError(apiErrorMessage(err, t("seo.purchase.failed"), t));
        setPhase("idle");
      }
      return;
    }
    remember({ state: "bought", notified: false, ...bought });
    await tellExpert(bought);
  }

  function decline() {
    const sent = sendRef.current?.(
      t("seo.purchase.declinedMessage", {
        what,
        key: purchase.idempotency_key,
      }),
    );
    // A queued answer is lost on reload; the buttons then come back.
    if (sent === false) setOutcome({ state: "declined" });
    else remember({ state: "declined" });
  }

  return (
    <>
      <DefaultToolRenderer {...props} />
      <Card
        size="small"
        title={t("seo.purchase.title", { what })}
        style={{ marginTop: 6, maxWidth: 560 }}
      >
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          {details.map(([label, value]) => (
            <div key={label} style={{ display: "flex", gap: 8, minWidth: 0 }}>
              <Text type="secondary" style={{ flexShrink: 0 }}>
                {label}
              </Text>
              <Text style={{ wordBreak: "break-word" }}>{value}</Text>
            </div>
          ))}
          <Paragraph type="secondary" style={{ marginBottom: 0 }}>
            {cost}
          </Paragraph>
          {error && <Alert type="error" showIcon message={error} />}
          {shown?.state === "bought" &&
          (shown.notified || phase === "waiting" || phase === "queued") ? (
            phase === "waiting" ? (
              <Text type="secondary">
                {t("seo.purchase.waiting", { id: shown.id })}
              </Text>
            ) : phase === "queued" ? (
              <Text type="secondary">
                {t("seo.purchase.queued", { id: shown.id })}
              </Text>
            ) : (
              <Text type="success">
                {t("seo.purchase.done", { id: shown.id, status: shown.status })}
              </Text>
            )
          ) : shown?.state === "bought" ? (
            <Space wrap>
              <Text>{t("seo.purchase.untold", { id: shown.id })}</Text>
              <Button size="small" onClick={() => void tellExpert(shown)}>
                {t("seo.purchase.tell")}
              </Button>
            </Space>
          ) : shown?.state === "declined" ? (
            <Text type="secondary">{t("seo.purchase.declined")}</Text>
          ) : (
            <Space wrap>
              <Button
                type="primary"
                size="small"
                loading={phase !== "idle"}
                disabled={props.isStreaming}
                onClick={() => void confirm()}
              >
                {t("seo.purchase.confirm")}
              </Button>
              <Button
                size="small"
                disabled={props.isStreaming || phase !== "idle"}
                onClick={decline}
              >
                {t("seo.purchase.decline")}
              </Button>
            </Space>
          )}
        </Space>
      </Card>
    </>
  );
}
