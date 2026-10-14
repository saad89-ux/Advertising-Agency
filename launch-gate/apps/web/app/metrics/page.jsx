"use client";

import React, { use } from "react";
import MetricsScreen from "../../components/MetricsScreen";

/**
 * Next.js Route Page: /metrics
 * Renders the Quality & Operational Metrics Dashboard.
 * Supports query params ?batch_id=<id> or ?client_id=<id>.
 */
export default function MetricsPage({ searchParams }) {
  const resolvedParams =
    searchParams && typeof searchParams.then === "function"
      ? use(searchParams)
      : searchParams;
  const batchId = resolvedParams?.batch_id || null;

  return (
    <main className="metrics-page-wrapper">
      <MetricsScreen batchId={batchId} />
    </main>
  );
}
