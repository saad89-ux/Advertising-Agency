"use client";

import React from "react";

/**
 * BatchSummary Component
 * Displays batch metadata, verdict distribution, findings count, client list,
 * and a warning banner for any isolated rejected rows.
 */
export default function BatchSummary({ batch, onToggleRejected, showRejected = false, liveStatus = null }) {
  if (!batch) return null;

  const summary = batch.summary || {};
  const verdictCounts = summary.verdict_counts || {};
  const rejectedCount = batch.rejected_count || (batch.rejected_rows ? batch.rejected_rows.length : 0);
  const clientIds = summary.client_ids || [];

  return (
    <header className="batch-summary-card" data-testid="batch-summary">
      <div className="batch-header-top">
        <div className="batch-title-area">
          <div className="batch-badge-row">
            <span className="batch-badge">Batch Review</span>
            <a
              href={`/batches/${batch.batch_id}/metrics`}
              className="btn-view-metrics"
              data-testid="link-batch-metrics"
              title="View Batch Metrics Dashboard"
            >
              📊 Metrics Dashboard →
            </a>
            {liveStatus && (
              <span
                className={`live-status-pill live-status-${liveStatus}`}
                data-testid="live-status-pill"
                title={`Live Stream Status: ${liveStatus}`}
              >
                <span className="live-status-dot"></span>
                <span className="live-status-label">
                  {liveStatus === "live" && "Live Stream Active"}
                  {liveStatus === "connecting" && "Connecting Live Stream..."}
                  {liveStatus === "completed" && "Checks Complete"}
                  {liveStatus === "error" && "Stream Offline"}
                  {liveStatus === "disconnected" && "Offline"}
                </span>
              </span>
            )}
          </div>
          <h1 className="batch-filename" data-testid="batch-filename">
            {batch.filename || "Untitled Batch"}
          </h1>
          <div className="batch-meta-row">
            <span className="batch-meta-item" title="Batch UUID">
              <strong>Batch ID:</strong> <code data-testid="batch-id">{batch.batch_id}</code>
            </span>
            {batch.created_at && (
              <span className="batch-meta-item">
                <strong>Uploaded:</strong>{" "}
                <time dateTime={batch.created_at} data-testid="batch-created-at">
                  {new Date(batch.created_at).toLocaleString()}
                </time>
              </span>
            )}
            {batch.uploaded_by && (
              <span className="batch-meta-item">
                <strong>Uploaded By:</strong> <span data-testid="batch-uploaded-by">{batch.uploaded_by}</span>
              </span>
            )}
            {batch.sheet_hash && (
              <span className="batch-meta-item" title="Sheet content hash">
                <strong>Sheet Hash:</strong>{" "}
                <code data-testid="batch-sheet-hash">{batch.sheet_hash.slice(0, 12)}...</code>
              </span>
            )}
          </div>
        </div>

        {rejectedCount > 0 && (
          <div className="batch-rejected-alert" data-testid="rejected-rows-alert">
            <span className="alert-icon">⚠️</span>
            <div className="alert-content">
              <strong>{rejectedCount} Malformed Row{rejectedCount > 1 ? "s" : ""} Isolated</strong>
              <p>Skipped during ingest to protect valid rows.</p>
            </div>
            {onToggleRejected && (
              <button
                type="button"
                className="btn-toggle-rejected"
                onClick={onToggleRejected}
                data-testid="toggle-rejected-btn"
              >
                {showRejected ? "Hide Rejected" : "View Rejected"}
              </button>
            )}
          </div>
        )}
      </div>

      <div className="metrics-grid" data-testid="metrics-grid">
        <div className="metric-box total-ads-box">
          <span className="metric-label">Total Visible Ads</span>
          <span className="metric-value" data-testid="metric-total-ads">
            {summary.total_ads ?? (batch.ads ? batch.ads.length : 0)}
          </span>
        </div>

        <div className="metric-box verdict-pill ready-box" title="Clean, approved for launch">
          <span className="metric-label">Ready</span>
          <span className="metric-value" data-testid="metric-verdict-ready">
            {verdictCounts.ready ?? 0}
          </span>
        </div>

        <div className="metric-box verdict-pill fix-needed-box" title="Rule violations or fixes required">
          <span className="metric-label">Fix Needed</span>
          <span className="metric-value" data-testid="metric-verdict-fix-needed">
            {verdictCounts.fix_needed ?? 0}
          </span>
        </div>

        <div className="metric-box verdict-pill blocked-box" title="Critical blocker (policy/offer violation)">
          <span className="metric-label">Blocked</span>
          <span className="metric-value" data-testid="metric-verdict-blocked">
            {verdictCounts.blocked ?? 0}
          </span>
        </div>

        <div className="metric-box verdict-pill needs-person-box" title="Uncertain wording or novel claim">
          <span className="metric-label">Needs Person</span>
          <span className="metric-value" data-testid="metric-verdict-needs-person">
            {verdictCounts.needs_person ?? 0}
          </span>
        </div>

        <div className="metric-box verdict-pill model-unavailable-box" title="Upstream model rate-limited or temporarily unavailable">
          <span className="metric-label">Model Unavail</span>
          <span className="metric-value" data-testid="metric-verdict-model-unavailable">
            {verdictCounts.model_unavailable ?? verdictCounts["model unavailable"] ?? 0}
          </span>
        </div>

        <div className="metric-box verdict-pill pending-box" title="Check in progress">
          <span className="metric-label">Pending</span>
          <span className="metric-value" data-testid="metric-verdict-pending">
            {verdictCounts.pending ?? 0}
          </span>
        </div>

        <div className="metric-box findings-box">
          <span className="metric-label">Total Findings</span>
          <span className="metric-value" data-testid="metric-total-findings">
            {summary.total_findings ?? 0}
          </span>
        </div>

        <div className="metric-box false-alarms-box" title="Reviewer-dismissed false alarms">
          <span className="metric-label">False Alarms</span>
          <span className="metric-value" data-testid="metric-false-alarms">
            {summary.false_alarms_count ?? 0}
          </span>
        </div>

        <div className="metric-box clients-box">
          <span className="metric-label">Client Scope</span>
          <span className="metric-value client-tags" data-testid="metric-clients">
            {clientIds.length > 0 ? (
              clientIds.map((cid) => (
                <span key={cid} className="client-tag">
                  {cid}
                </span>
              ))
            ) : (
              <span className="client-tag-empty">Unspecified</span>
            )}
          </span>
        </div>
      </div>
    </header>
  );
}
