/** Idempotency key for one paid attempt. */
export function attemptKey(): string {
  // randomUUID only exists in secure contexts; plain-HTTP LAN access lacks it.
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
