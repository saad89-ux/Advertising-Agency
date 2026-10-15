"use client";

import React, { use } from "react";
import BatchReviewScreen from "../../../components/BatchReviewScreen";

/**
 * Next.js Dynamic Route Page: /batches/[batchId]
 * Loads and renders the Batch Review Screen for a specific batch.
 */
export default function BatchPage({ params }) {
  // In Next.js 15+, params is a Promise that can be unwrapped with React.use();
  // In Next.js 13/14, params can also be a plain object.
  const resolvedParams = params && typeof params.then === "function" ? use(params) : params;
  const batchId = resolvedParams?.batchId;

  return (
    <div className="batch-page-wrapper">
      <BatchReviewScreen batchId={batchId} />
    </div>
  );
}
