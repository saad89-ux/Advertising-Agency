"use client";

import React, { use } from "react";
import MetricsScreen from "../../../../components/MetricsScreen";

/**
 * Next.js Dynamic Route Page: /batches/[batchId]/metrics
 * Loads and renders the Quality & Operational Metrics Screen for a specific batch.
 * Directly links with SSE live stream for real-time updates.
 */
export default function BatchMetricsPage({ params }) {
  const resolvedParams =
    params && typeof params.then === "function" ? use(params) : params;
  const batchId = resolvedParams?.batchId;

  return (
    <main className="batch-metrics-page-wrapper">
      <MetricsScreen batchId={batchId} />
    </main>
  );
}
