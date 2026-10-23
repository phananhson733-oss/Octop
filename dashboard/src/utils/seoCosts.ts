import type { LlmModel } from "../api/modules/seo";

/**
 * What a paid SEO lookup may cost, shown before it is bought: on the workbench
 * page and on the confirm card the SEO expert proposes in chat.
 */

// OpenSEO's per-answer reserves (pricing.ts): with web search, then without.
const RESERVE_USD: Record<LlmModel, [number, number]> = {
  chat_gpt: [0.09, 0.05],
  claude: [0.18, 0.08],
  gemini: [0.11, 0.05],
  perplexity: [0.04, 0.04],
};

/** The models a prompt is sent to, and the most their answers can cost. */
export function promptEstimate(
  models: LlmModel[],
  webSearch: boolean,
  country: string,
): { charged: LlmModel[]; usd: number } {
  // Gemini takes no country: it is skipped unsent, at no charge.
  const charged = models.filter(
    (model) => !(webSearch && country && model === "gemini"),
  );
  const usd = charged.reduce(
    (sum, model) => sum + RESERVE_USD[model][webSearch ? 0 : 1],
    0,
  );
  return { charged, usd: Number(usd.toFixed(2)) };
}

// LLM Mentions costs $0.1 a call plus $0.001 a row: three calls a platform,
// plus a comparison a platform when competitors are given.
export const brandEstimateUsd = (competitors: number): number =>
  competitors ? 1.02 : 0.81;
