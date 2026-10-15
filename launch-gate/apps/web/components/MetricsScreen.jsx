"use client";

import React, { useState, useEffect, useMemo } from "react";

/**
 * MetricsScreen Component (Task 52, PRD §4.7, Needs: 50).
 * Displays full operational, quality, reviewer, financial, and CI release gate metrics.
 * Connects to SSE live status stream (Task 50) for real-time batch updates.
 */
export default function MetricsScreen({
  batchId: initialBatchId = null,
  initialMetrics = null,
  apiBaseUrl = "http://localhost:8000",
  authToken = null,
}) {
  const [activeBatchId, setActiveBatchId] = useState(initialBatchId);
  const [viewMode, setViewMode] = useState(initialBatchId ? "batch" : "global"); // "global" | "batch"
  const [selectedClient, setSelectedClient] = useState("all");
  const [metrics, setMetrics] = useState(initialMetrics);
  const [batchesList, setBatchesList] = useState([]);
  const [loading, setLoading] = useState(!initialMetrics);
  const [error, setError] = useState(null);
  const [liveStatus, setLiveStatus] = useState("disconnected"); // "live" | "connecting" | "completed" | "error" | "disconnected"
  const [lastRefreshed, setLastRefreshed] = useState(null);

  // Retrieve token
  const token = useMemo(() => {
    return (
      authToken ||
      (typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null)
    );
  }, [authToken]);

  // Fetch list of accessible batches for the batch selector dropdown
  useEffect(() => {
    let isMounted = true;
    const headers = { Accept: "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;

    fetch(`${apiBaseUrl}/batches`, { headers })
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => {
        if (isMounted && Array.isArray(data)) {
          setBatchesList(data);
        }
      })
      .catch(() => {});

    return () => {
      isMounted = false;
    };
  }, [apiBaseUrl, token]);

  // Main metrics fetcher
  const fetchMetrics = React.useCallback(async () => {
    setLoading(true);
    setError(null);

    const headers = { Accept: "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;

    try {
      let url = "";
      if (viewMode === "batch" && activeBatchId) {
        url = `${apiBaseUrl}/batches/${encodeURIComponent(activeBatchId)}/metrics`;
      } else {
        const queryParams = new URLSearchParams();
        if (selectedClient && selectedClient !== "all") {
          queryParams.set("client_id", selectedClient);
        }
        url = `${apiBaseUrl}/metrics${queryParams.toString() ? `?${queryParams.toString()}` : ""}`;
      }

      const res = await fetch(url, { headers });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        const msg =
          errData.detail ||
          (res.status === 401
            ? "Authentication required. Please sign in."
            : res.status === 403
            ? "Access forbidden: You do not have permission for the requested client or batch."
            : res.status === 404
            ? "Batch metrics not found."
            : `Failed to load metrics: HTTP ${res.status}`);
        throw new Error(msg);
      }

      const data = await res.json();
      setMetrics(data);
      setLastRefreshed(new Date().toLocaleTimeString());
      setLoading(false);
    } catch (err) {
      setError(err.message);
      setLoading(false);
    }
  }, [apiBaseUrl, token, viewMode, activeBatchId, selectedClient]);

  // Re-fetch when view mode, batch ID, or client filter changes
  useEffect(() => {
    fetchMetrics();
  }, [fetchMetrics]);

  // Server-Sent Events (SSE) Live Status Stream (Needs: 50)
  // When viewing a specific batch, subscribe to real-time events.
  useEffect(() => {
    if (
      viewMode !== "batch" ||
      !activeBatchId ||
      typeof window === "undefined" ||
      typeof EventSource === "undefined"
    ) {
      setLiveStatus("disconnected");
      return;
    }

    const sseUrl = `${apiBaseUrl}/batches/${encodeURIComponent(activeBatchId)}/events${
      token ? `?token=${encodeURIComponent(token)}` : ""
    }`;

    let es;
    try {
      es = new EventSource(sseUrl);
      setLiveStatus("connecting");

      es.addEventListener("connected", () => {
        setLiveStatus("live");
      });

      es.addEventListener("batch_status", () => {
        // Trigger live refresh of metrics
        fetchMetrics();
      });

      es.addEventListener("ad_status", () => {
        // Real-time ad check completed, refresh metrics
        fetchMetrics();
      });

      es.addEventListener("complete", () => {
        setLiveStatus("completed");
        fetchMetrics();
      });

      es.onerror = () => {
        setLiveStatus("error");
        es.close();
      };
    } catch (_) {
      setLiveStatus("error");
    }

    return () => {
      if (es) {
        es.close();
      }
    };
  }, [apiBaseUrl, token, viewMode, activeBatchId, fetchMetrics]);

  // Computed / Extracted metrics values with safe fallbacks
  const totals = metrics?.totals || {};
  const totalAds = totals.total_ads ?? metrics?.total_ads ?? 0;
  const rejectedRowsCount = totals.rejected_rows_count ?? 0;
  const totalSheetRows = totals.total_sheet_rows ?? (totalAds + rejectedRowsCount);

  const verdicts = metrics?.verdicts || {};
  const readyCount = verdicts.ready ?? 0;
  const fixNeededCount = verdicts.fix_needed ?? 0;
  const blockedCount = verdicts.blocked ?? 0;
  const needsPersonCount = verdicts.needs_person ?? 0;
  const pendingCount = verdicts.pending ?? 0;

  const adsBlocked = metrics?.ads_blocked || {};
  const totalBlocked = adsBlocked.total_blocked ?? (blockedCount + fixNeededCount);
  const blockedRate = adsBlocked.blocked_rate ?? metrics?.overall_blocked_rate ?? (totalAds > 0 ? totalBlocked / totalAds : 0.0);

  const problemsByType = metrics?.problems_by_type || {};
  const totalFindings = problemsByType.total_findings ?? 0;
  const dismissedFindings = problemsByType.dismissed_findings ?? 0;
  const byCheckType = problemsByType.by_check_type || (typeof problemsByType.mechanical === "number" ? problemsByType : {});
  const byField = problemsByType.by_field || {};
  const topRules = problemsByType.top_rules || [];
  const avgFindingsPerAd = problemsByType.avg_findings_per_ad ?? (totalAds > 0 ? (totalFindings / totalAds).toFixed(2) : "0.00");

  const falseAlarms = metrics?.false_alarms || {};
  const falseAlarmRate = falseAlarms.false_alarm_rate ?? metrics?.overall_false_alarm_rate ?? (totalFindings > 0 ? dismissedFindings / totalFindings : 0.0);
  const topDismissedRules = falseAlarms.top_dismissed_rules || [];
  const cleanedAdsCount = falseAlarms.cleaned_ads_count ?? 0;

  const cost = metrics?.cost || {};
  const totalCostUsd = cost.total_cost_usd ?? metrics?.total_cost_usd ?? 0.0;
  const avgCostPerAdUsd = cost.avg_cost_per_ad_usd ?? metrics?.avg_cost_per_ad_usd ?? 0.0;
  const totalSteps = cost.total_steps ?? 0;
  const avgStepsPerAd = cost.avg_steps_per_ad ?? 0.0;
  const tracesCount = cost.traces_count ?? 0;
  const stepCappedAdsCount = cost.step_capped_ads_count ?? 0;

  const reviewerTime = metrics?.reviewer_time || {};
  const reviewerActions = metrics?.reviewer_actions || {};
  const approvedCount = reviewerActions.approved_count ?? 0;
  const rejectedCount = reviewerActions.rejected_count ?? 0;
  const rejectionReasons = reviewerActions.rejection_reasons || {};

  const simulatedRejections = metrics?.simulated_rejection_rates || {};
  const withoutGateRate = simulatedRejections.without_gate_rejection_rate ?? 0.0;
  const withGateRate = simulatedRejections.with_gate_rejection_rate ?? 0.0;
  const rejectionsAvoidedCount = simulatedRejections.rejections_avoided_count ?? 0;
  const protectionEffectiveness = simulatedRejections.protection_effectiveness_percent ?? 100.0;

  const platformSends = metrics?.platform_sends || {};
  const totalSends = platformSends.total_sends ?? 0;
  const platformAccepted = platformSends.platform_accepted ?? 0;
  const platformRejected = platformSends.platform_rejected ?? 0;
  const sendAcceptanceRate = platformSends.acceptance_rate ?? (totalSends > 0 ? platformAccepted / totalSends : 0.0);
  const sendAttempts = platformSends.total_attempts ?? totalSends;
  const retriedAdsCount = platformSends.retried_ads_count ?? 0;
  const retryRate = platformSends.retry_rate ?? 0.0;
  const deadLetterCount = platformSends.dead_letter_count ?? metrics?.dead_letter?.count ?? 0;
  const deadLetterRate = platformSends.dead_letter_rate ?? metrics?.dead_letter?.rate ?? 0.0;

  const ciGate = metrics?.ci_release_gate || {
    status: falseAlarmRate <= 0.05 && deadLetterRate <= 0.05 ? "passed" : "failed",
    passed: falseAlarmRate <= 0.05 && deadLetterRate <= 0.05,
    checks: [
      {
        name: "False Alarm Rate <= 5%",
        target: "<= 5.0%",
        actual: `${(falseAlarmRate * 100).toFixed(2)}%`,
        passed: falseAlarmRate <= 0.05,
      },
      {
        name: "Dead-Letter Rate <= 5%",
        target: "<= 5.0%",
        actual: `${(deadLetterRate * 100).toFixed(2)}%`,
        passed: deadLetterRate <= 0.05,
      },
    ],
  };

  // Export JSON helper
  const handleExportJson = () => {
    if (!metrics) return;
    const blob = new Blob([JSON.stringify(metrics, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `launch-gate-metrics-${viewMode}-${activeBatchId || "global"}-${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="metrics-screen-container" data-testid="metrics-screen">
      {/* Top Header & Navigation Bar */}
      <header className="metrics-screen-header">
        <div className="metrics-header-main">
          <div className="metrics-badge-row">
            <span className="metrics-badge">Launch Gate Analytics</span>
            {viewMode === "batch" && (
              <span
                className={`live-status-pill live-status-${liveStatus}`}
                data-testid="metrics-live-status-pill"
                title={`SSE Live Status: ${liveStatus}`}
              >
                <span className="live-status-dot"></span>
                <span className="live-status-label">
                  {liveStatus === "live" && "Live Stream Active"}
                  {liveStatus === "connecting" && "Connecting Stream..."}
                  {liveStatus === "completed" && "Checks Complete"}
                  {liveStatus === "error" && "Stream Offline"}
                  {liveStatus === "disconnected" && "Offline"}
                </span>
              </span>
            )}
          </div>
          <h1 className="metrics-screen-title" data-testid="metrics-screen-title">
            {viewMode === "batch" && metrics?.filename
              ? `Batch Metrics: ${metrics.filename}`
              : "Quality & Operational Metrics"}
          </h1>
          <p className="metrics-screen-subtitle">
            Automated compliance auditing, false-alarm tracking, financial cost ledger, and CI release gate verification.
          </p>
        </div>

        <div className="metrics-header-actions">
          {viewMode === "batch" && activeBatchId ? (
            <a
              href={`/batches/${activeBatchId}`}
              className="btn-metrics-action btn-nav-review"
              data-testid="link-back-to-review"
            >
              ← Back to Review Screen
            </a>
          ) : (
            <a
              href="/review"
              className="btn-metrics-action btn-nav-review"
              data-testid="link-to-review-index"
            >
              ← Review Batches
            </a>
          )}
          <button
            type="button"
            className="btn-metrics-action btn-refresh"
            onClick={fetchMetrics}
            disabled={loading}
            data-testid="btn-metrics-refresh"
            title="Refresh metrics data"
          >
            🔄 Refresh
          </button>
          <button
            type="button"
            className="btn-metrics-action btn-export"
            onClick={handleExportJson}
            disabled={!metrics}
            data-testid="btn-metrics-export"
            title="Export full metrics as JSON"
          >
            ⬇️ Export JSON
          </button>
        </div>
      </header>

      {/* View Mode & Client Filtering Controls */}
      <section className="metrics-controls-bar" data-testid="metrics-controls-bar">
        <div className="control-group view-mode-toggle">
          <span className="control-label">View Scope:</span>
          <button
            type="button"
            className={`btn-mode-pill ${viewMode === "global" ? "active" : ""}`}
            onClick={() => {
              setViewMode("global");
              setActiveBatchId(null);
            }}
            data-testid="btn-mode-global"
          >
            🌐 Global (Cross-Batch)
          </button>
          <button
            type="button"
            className={`btn-mode-pill ${viewMode === "batch" ? "active" : ""}`}
            onClick={() => {
              setViewMode("batch");
              if (!activeBatchId && batchesList.length > 0) {
                setActiveBatchId(batchesList[0].batch_id);
              }
            }}
            data-testid="btn-mode-batch"
          >
            📦 Single Batch
          </button>
        </div>

        {viewMode === "batch" ? (
          <div className="control-group batch-select-group">
            <label htmlFor="batch-select" className="control-label">
              Select Batch:
            </label>
            <select
              id="batch-select"
              className="select-control"
              value={activeBatchId || ""}
              onChange={(e) => setActiveBatchId(e.target.value)}
              data-testid="select-batch-id"
            >
              <option value="" disabled>
                -- Choose a batch --
              </option>
              {batchesList.map((b) => (
                <option key={b.batch_id} value={b.batch_id}>
                  {b.filename || b.batch_id} ({b.ad_count ?? "?"} ads)
                </option>
              ))}
            </select>
          </div>
        ) : (
          <div className="control-group client-select-group">
            <label htmlFor="client-select" className="control-label">
              Filter by Client:
            </label>
            <select
              id="client-select"
              className="select-control"
              value={selectedClient}
              onChange={(e) => setSelectedClient(e.target.value)}
              data-testid="select-client-filter"
            >
              <option value="all">All Assigned Clients</option>
              {metrics?.clients?.map((cid) => (
                <option key={cid} value={cid}>
                  {cid}
                </option>
              ))}
            </select>
          </div>
        )}

        {lastRefreshed && (
          <span className="last-refreshed-text" data-testid="metrics-last-refreshed">
            Updated: {lastRefreshed}
          </span>
        )}
      </section>

      {/* Error & Loading States */}
      {error && (
        <div className="metrics-error-banner" data-testid="metrics-error-banner">
          <span className="error-icon">⚠️</span>
          <div className="error-content">
            <strong>Error Loading Metrics</strong>
            <p>{error}</p>
          </div>
        </div>
      )}

      {loading && !metrics && (
        <div className="metrics-loading-state" data-testid="metrics-loading-state">
          <p>Loading metrics computation...</p>
        </div>
      )}

      {/* Main Metrics Content */}
      {metrics && (
        <>
          {/* CI Release Gate Compliance Banner (PRD §4.7 / Line 27) */}
          <section
            className={`ci-release-gate-card ${
              ciGate.passed || ciGate.status === "passed" ? "gate-passed" : "gate-failed"
            }`}
            data-testid="ci-release-gate-card"
          >
            <div className="gate-header">
              <div className="gate-title-area">
                <span className="gate-icon">
                  {ciGate.passed || ciGate.status === "passed" ? "🛡️" : "🚨"}
                </span>
                <div>
                  <h2 className="gate-heading" data-testid="ci-gate-status">
                    CI Release Gate:{" "}
                    <span className="status-label">
                      {ciGate.passed || ciGate.status === "passed" ? "PASSED" : "FAILED"}
                    </span>
                  </h2>
                  <p className="gate-description">
                    Automated production deployment gate. Merges and launches are blocked if false-alarm or dead-letter rates exceed 5%.
                  </p>
                </div>
              </div>
              <div className="gate-badge-pill" data-testid="ci-gate-badge">
                {ciGate.passed || ciGate.status === "passed" ? "✅ READY FOR RELEASE" : "❌ RELEASE BLOCKED"}
              </div>
            </div>

            <div className="gate-checks-grid" data-testid="ci-gate-checks">
              {ciGate.checks?.map((check, idx) => (
                <div
                  key={idx}
                  className={`gate-check-box ${check.passed ? "check-pass" : "check-fail"}`}
                  data-testid={`gate-check-${idx}`}
                >
                  <div className="check-status-icon">{check.passed ? "✓" : "✗"}</div>
                  <div className="check-details">
                    <span className="check-name">{check.name}</span>
                    <div className="check-values">
                      <span>Target: {check.target}</span>
                      <strong className="actual-val">Actual: {check.actual}</strong>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </section>

          {/* High-Level Primary KPIs */}
          <section className="primary-kpi-grid" data-testid="primary-kpi-grid">
            {/* Total Ads / Rows */}
            <div className="kpi-card" data-testid="kpi-total-ads">
              <span className="kpi-label">Ads Evaluated</span>
              <div className="kpi-number">{totalAds}</div>
              <span className="kpi-subtext">
                {rejectedRowsCount > 0
                  ? `+ ${rejectedRowsCount} malformed rows isolated`
                  : "All ingest rows valid"}
              </span>
            </div>

            {/* Protection Effectiveness */}
            <div className="kpi-card kpi-highlight-success" data-testid="kpi-protection-effectiveness">
              <span className="kpi-label">Protection Effectiveness</span>
              <div className="kpi-number">{protectionEffectiveness.toFixed(1)}%</div>
              <span className="kpi-subtext">
                {rejectionsAvoidedCount} bad ads prevented from reaching platform
              </span>
            </div>

            {/* False Alarm Rate */}
            <div
              className={`kpi-card ${falseAlarmRate > 0.05 ? "kpi-highlight-warning" : ""}`}
              data-testid="kpi-false-alarm-rate"
            >
              <span className="kpi-label">False Alarm Rate</span>
              <div className="kpi-number">{(falseAlarmRate * 100).toFixed(1)}%</div>
              <span className="kpi-subtext">
                {dismissedFindings} dismissed of {totalFindings} findings (Target: ≤5%)
              </span>
            </div>

            {/* Financial Spend */}
            <div className="kpi-card" data-testid="kpi-total-cost">
              <span className="kpi-label">Worker Spend (USD)</span>
              <div className="kpi-number">${totalCostUsd.toFixed(4)}</div>
              <span className="kpi-subtext">
                Avg ${(avgCostPerAdUsd * 100).toFixed(4)} / 100 ads (Target: &lt;$0.25)
              </span>
            </div>

            {/* Reviewer Speed */}
            <div className="kpi-card" data-testid="kpi-reviewer-time">
              <span className="kpi-label">Avg Reviewer Time</span>
              <div className="kpi-number">{reviewerTime.formatted_avg || "0s"}</div>
              <span className="kpi-subtext">
                {reviewerTime.reviewed_count ?? 0} ads reviewed (Median: {reviewerTime.median_seconds ?? 0}s)
              </span>
            </div>

            {/* Dead-Letter Count */}
            <div
              className={`kpi-card ${deadLetterCount > 0 ? "kpi-highlight-warning" : ""}`}
              data-testid="kpi-dead-letter"
            >
              <span className="kpi-label">Dead-Letter Ads</span>
              <div className="kpi-number">{deadLetterCount}</div>
              <span className="kpi-subtext">
                Rate: {(deadLetterRate * 100).toFixed(1)}% (Target: ≤5%)
              </span>
            </div>
          </section>

          {/* Simulated Rejection Rates Comparison (PRD §4.7) */}
          <section className="metrics-section-panel" data-testid="section-simulated-rejections">
            <h3 className="section-title">Platform Protection & Simulated Rejection Rates</h3>
            <p className="section-description">
              Comparison of predicted platform failure rate without Launch Gate vs. actual protected platform rejection rate.
            </p>

            <div className="rates-comparison-grid">
              <div className="rate-box without-gate-box" data-testid="rate-without-gate">
                <span className="rate-title">Without Launch Gate (Simulated Raw Failure)</span>
                <div className="rate-value">{(withoutGateRate * 100).toFixed(1)}%</div>
                <p className="rate-explanation">
                  Percentage of sheet ads and malformed rows that would have failed on the live platform.
                </p>
                <div className="rate-bar-container">
                  <div
                    className="rate-bar bar-danger"
                    style={{ width: `${Math.min(100, withoutGateRate * 100)}%` }}
                  ></div>
                </div>
              </div>

              <div className="rate-box with-gate-box" data-testid="rate-with-gate">
                <span className="rate-title">With Launch Gate (Protected Sends)</span>
                <div className="rate-value">{(withGateRate * 100).toFixed(1)}%</div>
                <p className="rate-explanation">
                  Actual platform rejection rate after gate verification, reviewer approval, and dry-run protection.
                </p>
                <div className="rate-bar-container">
                  <div
                    className="rate-bar bar-success"
                    style={{ width: `${Math.min(100, Math.max(2, withGateRate * 100))}%` }}
                  ></div>
                </div>
              </div>
            </div>
          </section>

          {/* Detailed Two-Column Section: Findings Breakdown & Reviewer Decisions */}
          <div className="metrics-two-col-layout">
            {/* Left Column: Problems by Type & Field (Needs 19) */}
            <section className="metrics-section-panel" data-testid="section-problems-by-type">
              <h3 className="section-title">Problems by Check Type & Field</h3>
              <p className="section-description">
                Total Findings: <strong>{totalFindings}</strong> (Avg {avgFindingsPerAd} per ad)
              </p>

              {/* By Check Type */}
              <div className="breakdown-group">
                <h4 className="breakdown-subtitle">By Verification Stage</h4>
                <div className="stat-bars-list">
                  <div className="stat-bar-item">
                    <div className="stat-bar-header">
                      <span>⚙️ Mechanical Checks (limits, UTM, ratios)</span>
                      <strong>{byCheckType.mechanical ?? 0}</strong>
                    </div>
                    <div className="progress-track">
                      <div
                        className="progress-fill fill-mechanical"
                        style={{
                          width: `${
                            totalFindings > 0
                              ? ((byCheckType.mechanical ?? 0) / totalFindings) * 100
                              : 0
                          }%`,
                        }}
                      ></div>
                    </div>
                  </div>

                  <div className="stat-bar-item">
                    <div className="stat-bar-header">
                      <span>🌐 Page Checks (HTTP, redirects, offer)</span>
                      <strong>{byCheckType.page ?? 0}</strong>
                    </div>
                    <div className="progress-track">
                      <div
                        className="progress-fill fill-page"
                        style={{
                          width: `${
                            totalFindings > 0
                              ? ((byCheckType.page ?? 0) / totalFindings) * 100
                              : 0
                          }%`,
                        }}
                      ></div>
                    </div>
                  </div>

                  <div className="stat-bar-item">
                    <div className="stat-bar-header">
                      <span>📝 Wording & Policy Checks (claims, citations)</span>
                      <strong>{byCheckType.wording ?? 0}</strong>
                    </div>
                    <div className="progress-track">
                      <div
                        className="progress-fill fill-wording"
                        style={{
                          width: `${
                            totalFindings > 0
                              ? ((byCheckType.wording ?? 0) / totalFindings) * 100
                              : 0
                          }%`,
                        }}
                      ></div>
                    </div>
                  </div>
                </div>
              </div>

              {/* By Field */}
              {Object.keys(byField).length > 0 && (
                <div className="breakdown-group">
                  <h4 className="breakdown-subtitle">By Ad Field</h4>
                  <div className="field-pills-row" data-testid="problems-by-field-row">
                    {Object.entries(byField).map(([fld, cnt]) => (
                      <span key={fld} className="field-pill">
                        <code>{fld}</code>: <strong>{cnt}</strong>
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Top Violated Rules */}
              <div className="breakdown-group">
                <h4 className="breakdown-subtitle">Top Violated Rules</h4>
                {topRules.length === 0 ? (
                  <p className="text-muted">No rule violations detected.</p>
                ) : (
                  <table className="mini-data-table" data-testid="table-top-rules">
                    <thead>
                      <tr>
                        <th>Rule ID</th>
                        <th>Violations</th>
                        <th>Share</th>
                      </tr>
                    </thead>
                    <tbody>
                      {topRules.slice(0, 5).map((r) => (
                        <tr key={r.rule_id}>
                          <td>
                            <code>{r.rule_id}</code>
                          </td>
                          <td>
                            <strong>{r.count}</strong>
                          </td>
                          <td>
                            {totalFindings > 0
                              ? `${((r.count / totalFindings) * 100).toFixed(1)}%`
                              : "0%"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </section>

            {/* Right Column: Reviewer Actions & False Alarms (Needs 43) */}
            <section className="metrics-section-panel" data-testid="section-reviewer-actions">
              <h3 className="section-title">Reviewer Decisions & False Alarms</h3>
              <p className="section-description">
                Human reviewer audit history, false-alarm dismissals, and rejection patterns.
              </p>

              {/* Actions Grid */}
              <div className="reviewer-kpi-subgrid">
                <div className="reviewer-box approved-box">
                  <span className="box-label">Approved</span>
                  <span className="box-val" data-testid="reviewer-approved-count">
                    {approvedCount}
                  </span>
                </div>
                <div className="reviewer-box rejected-box">
                  <span className="box-label">Rejected</span>
                  <span className="box-val" data-testid="reviewer-rejected-count">
                    {rejectedCount}
                  </span>
                </div>
                <div className="reviewer-box false-alarm-box">
                  <span className="box-label">Dismissed Findings</span>
                  <span className="box-val" data-testid="reviewer-dismissed-count">
                    {dismissedFindings}
                  </span>
                </div>
                <div className="reviewer-box cleaned-box">
                  <span className="box-label">Cleaned Ads</span>
                  <span className="box-val" data-testid="reviewer-cleaned-count">
                    {cleanedAdsCount}
                  </span>
                </div>
              </div>

              {/* Top Dismissed Rules (False Alarms) */}
              <div className="breakdown-group">
                <h4 className="breakdown-subtitle">Top Dismissed Rules (False Alarms)</h4>
                {topDismissedRules.length === 0 ? (
                  <p className="text-muted">No findings dismissed as false alarms.</p>
                ) : (
                  <table className="mini-data-table" data-testid="table-top-dismissed-rules">
                    <thead>
                      <tr>
                        <th>Rule ID</th>
                        <th>Dismissals</th>
                      </tr>
                    </thead>
                    <tbody>
                      {topDismissedRules.slice(0, 5).map((r) => (
                        <tr key={r.rule_id}>
                          <td>
                            <code>{r.rule_id}</code>
                          </td>
                          <td>
                            <strong className="text-amber">{r.count}</strong>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>

              {/* Rejection Reasons */}
              <div className="breakdown-group">
                <h4 className="breakdown-subtitle">Rejection Reasons Frequency</h4>
                {Object.keys(rejectionReasons).length === 0 ? (
                  <p className="text-muted">No rejection reasons recorded.</p>
                ) : (
                  <ul className="reasons-list" data-testid="list-rejection-reasons">
                    {Object.entries(rejectionReasons).map(([reason, count]) => (
                      <li key={reason} className="reason-item">
                        <span className="reason-text">{reason}</span>
                        <span className="reason-badge">{count}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </section>
          </div>

          {/* Secondary Grid: Cost Ledger & Platform Delivery Resilience */}
          <div className="metrics-two-col-layout">
            {/* Left: Cost Ledger & Step Caps (Needs 36) */}
            <section className="metrics-section-panel" data-testid="section-cost-ledger">
              <h3 className="section-title">Worker Performance & Cost Ledger</h3>
              <p className="section-description">
                Model spend tracking and LangGraph step verification per PRD §4.5 / §4.7.
              </p>

              <div className="cost-details-list">
                <div className="cost-row">
                  <span>Total Execution Cost:</span>
                  <strong data-testid="cost-total-val">${totalCostUsd.toFixed(6)} USD</strong>
                </div>
                <div className="cost-row">
                  <span>Average Cost per Ad:</span>
                  <strong data-testid="cost-avg-ad-val">${avgCostPerAdUsd.toFixed(6)} USD</strong>
                </div>
                <div className="cost-row">
                  <span>Target Benchmark (100 ads):</span>
                  <span>
                    <strong>${(avgCostPerAdUsd * 100).toFixed(4)}</strong> vs &lt;$0.25 target
                  </span>
                </div>
                <div className="cost-row">
                  <span>Total Worker Steps:</span>
                  <strong data-testid="cost-total-steps">{totalSteps}</strong>
                </div>
                <div className="cost-row">
                  <span>Average Steps per Ad:</span>
                  <strong>{avgStepsPerAd}</strong>
                </div>
                <div className="cost-row">
                  <span>Recorded Traces:</span>
                  <strong>{tracesCount}</strong>
                </div>
                <div className="cost-row">
                  <span>Step-Capped Ads (≥5 steps):</span>
                  <strong
                    className={stepCappedAdsCount > 0 ? "text-danger" : "text-success"}
                    data-testid="cost-step-capped-count"
                  >
                    {stepCappedAdsCount}
                  </strong>
                </div>
              </div>
            </section>

            {/* Right: Platform Delivery & Retries (Needs 47) */}
            <section className="metrics-section-panel" data-testid="section-platform-sends">
              <h3 className="section-title">Platform Sends & Delivery Resilience</h3>
              <p className="section-description">
                Stand-in dispatch results, retry backoffs, timeouts, and dead-letter queueing.
              </p>

              <div className="cost-details-list">
                <div className="cost-row">
                  <span>Total Sent to Platform:</span>
                  <strong data-testid="sends-total">{totalSends}</strong>
                </div>
                <div className="cost-row">
                  <span>Platform Accepted:</span>
                  <strong className="text-success" data-testid="sends-accepted">
                    {platformAccepted}
                  </strong>
                </div>
                <div className="cost-row">
                  <span>Platform Rejected:</span>
                  <strong className="text-danger" data-testid="sends-rejected">
                    {platformRejected}
                  </strong>
                </div>
                <div className="cost-row">
                  <span>Platform Acceptance Rate:</span>
                  <strong>{(sendAcceptanceRate * 100).toFixed(1)}%</strong>
                </div>
                <div className="cost-row">
                  <span>Total Send Attempts (Retries):</span>
                  <strong data-testid="sends-attempts">{sendAttempts}</strong>
                </div>
                <div className="cost-row">
                  <span>Retried Ads Count:</span>
                  <strong>{retriedAdsCount}</strong>
                </div>
                <div className="cost-row">
                  <span>Retry Rate:</span>
                  <strong>{(retryRate * 100).toFixed(1)}%</strong>
                </div>
                <div className="cost-row">
                  <span>Dead-Letter Count:</span>
                  <strong
                    className={deadLetterCount > 0 ? "text-danger" : "text-muted"}
                    data-testid="sends-dead-letter-count"
                  >
                    {deadLetterCount}
                  </strong>
                </div>
              </div>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
