// A campaign is selected explicitly by history/launch. A new draft clears it.
export function campaignScope() {
  if (typeof window === "undefined") return {};
  const query = new URLSearchParams(window.location.search);
  const batchId = query.get("batch_id") || localStorage.getItem("launch_gate_active_batch") || "";
  const clientId = query.get("client_id") || localStorage.getItem("launch_gate_active_client") || "";
  return { batchId, clientId };
}

export function selectCampaign(batchId, clientId) {
  localStorage.setItem("launch_gate_active_batch", batchId || "");
  localStorage.setItem("launch_gate_active_client", clientId || "");
  window.dispatchEvent(new Event("campaign-changed"));
}

export function campaignHref(path, scope = campaignScope()) {
  if (path === "/" || !scope.batchId) return path;
  const url = new URL(path, "http://scope.local");
  url.searchParams.set("batch_id", scope.batchId);
  if (scope.clientId) url.searchParams.set("client_id", scope.clientId);
  return url.pathname + url.search;
}

const SCOPED_READS = new Set([
  "/campaign-flow/all-campaigns", "/campaign-flow/mcp-traces",
  "/campaign-flow/platform-performance", "/campaign-flow/revenue-analytics",
  "/campaign-flow/violations", "/attribution/summary", "/attribution/ads",
  "/attribution/live-events", "/alerts", "/dead-letter", "/batches",
]);

export function scopedFetch(input, options) {
  const url = new URL(input, window.location.origin);
  if ((!options?.method || options.method === "GET") && SCOPED_READS.has(url.pathname)) {
    const scope = campaignScope();
    if (scope.batchId && !url.searchParams.has("batch_id")) url.searchParams.set("batch_id", scope.batchId);
    if (scope.clientId && !url.searchParams.has("client_id")) url.searchParams.set("client_id", scope.clientId);
  }
  return window.fetch(url.toString(), options);
}
