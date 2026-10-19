"use client";

import React from "react";

/**
 * AdCard Component
 * Implements the core "ad beside its findings" split view layout.
 * Left column: Ad details, copy, destination URL, creative reference, verdict, and action buttons.
 * Right column: Findings list with check type, targeted field, message, proposed fix, and citations.
 */
export default function AdCard({
  ad,
  onApprove,
  onEdit,
  onReject,
  onSend,
  onRetry,
  onReturnToReview,
  onResolveAlert,
  onViewTrace,
  onAttachEvidence,
  onRecheck,
  onDismissFinding,
}) {
  if (!ad) return null;

  const findings = ad.findings || [];
  const verdict = (ad.verdict || "pending").toLowerCase();

  // Verdict style and label mapping
  const verdictConfig = {
    ready: {
      label: "Ready",
      className: "verdict-ready",
      icon: "✓",
      desc: "Clean, approved for launch",
    },
    fix_needed: {
      label: "Fix Needed",
      className: "verdict-fix-needed",
      icon: "⚠️",
      desc: "Corrections or fixes required",
    },
    blocked: {
      label: "Blocked",
      className: "verdict-blocked",
      icon: "⛔",
      desc: "Critical policy or landing page blocker",
    },
    needs_person: {
      label: "Needs a Person",
      className: "verdict-needs-person",
      icon: "👤",
      desc: "Uncertain wording or claims requiring human review",
    },
    pending: {
      label: "Pending",
      className: "verdict-pending",
      icon: "⏳",
      desc: "Checks in progress or awaiting evaluation",
    },
  };

  const currentVerdict = verdictConfig[verdict] || verdictConfig.pending;

  return (
    <article
      className={`ad-card-row ${currentVerdict.className}-border`}
      data-testid={`ad-card-${ad.ad_id}`}
      data-verdict={verdict}
      data-row-number={ad.row_number}
    >
      <div className="ad-beside-findings-grid">
        {/* Left Column: The Ad */}
        <div className="ad-pane" data-testid={`ad-pane-${ad.ad_id}`}>
          <div className="ad-header-strip">
            <div className="ad-meta-tags">
              <span className="row-number-badge" title="Row index in source sheet">
                Row #{ad.row_number ?? "?"}
              </span>
              {(ad.is_sample || ad.ad_id?.startsWith("demo-")) && (
                <span
                  className="sample-badge"
                  data-testid={`sample-badge-${ad.ad_id}`}
                  style={{
                    background: "rgba(234, 179, 8, 0.2)",
                    color: "#facc15",
                    border: "1px solid rgba(234, 179, 8, 0.4)",
                    borderRadius: "4px",
                    padding: "2px 6px",
                    fontSize: "11px",
                    fontWeight: "bold",
                    letterSpacing: "0.05em",
                  }}
                  title="Sample / Demo Ad"
                >
                  [SAMPLE]
                </span>
              )}
              <span className="platform-badge" title={`Platform: ${ad.platform_raw || ad.platform}`}>
                {ad.platform}
              </span>
              {ad.client_id && (
                <span className="client-badge" title="Assigned Client">
                  {ad.client_id}
                </span>
              )}
              {(ad.has_evidence || ad.evidence_note || ad.evidence_file) && (
                <span
                  className="evidence-badge"
                  data-testid={`evidence-badge-${ad.ad_id}`}
                  title={ad.evidence_note || (ad.evidence_file ? `File: ${ad.evidence_file}` : "Client approval evidence attached")}
                >
                  📎 Evidence Attached
                </span>
              )}
              {ad.approval_state === "sent" && (
                <span
                  className="approval-badge badge-sent"
                  data-testid={`approval-badge-${ad.ad_id}`}
                  title={ad.sent_at ? `Sent to platform at ${ad.sent_at}` : "Sent to platform (dry run)"}
                >
                  🚀 Sent
                </span>
              )}
              {ad.approval_state === "sending" && (
                <span
                  className="approval-badge badge-sending"
                  data-testid={`approval-badge-${ad.ad_id}`}
                  title="In flight to stand-in platform"
                >
                  🛫 Sending...
                </span>
              )}
              {ad.approval_state === "approved" && (
                <span
                  className="approval-badge badge-approved"
                  data-testid={`approval-badge-${ad.ad_id}`}
                  title="Approved for platform send"
                >
                  ✓ Approved
                </span>
              )}
              {ad.approval_state === "rejected" && (
                <span
                  className="approval-badge badge-rejected"
                  data-testid={`approval-badge-${ad.ad_id}`}
                  title={ad.rejection_reason || "Ad rejected"}
                >
                  ✕ Rejected
                </span>
              )}
              {ad.approval_state === "dead_letter" && (
                <span
                  className="approval-badge badge-dead-letter"
                  data-testid={`approval-badge-${ad.ad_id}`}
                  title="Send retries exhausted — human attention required"
                >
                  ⚠️ Dead Letter
                </span>
              )}
            </div>

            <div
              className={`verdict-pill-badge ${currentVerdict.className}`}
              title={currentVerdict.desc}
              data-testid={`verdict-badge-${ad.ad_id}`}
            >
              <span className="verdict-icon">{currentVerdict.icon}</span>
              <span className="verdict-text">{currentVerdict.label}</span>
            </div>
          </div>

          <div className="ad-content-body">
            {ad.headline && (
              <div className="ad-field headline-field">
                <span className="field-label">Headline</span>
                <h3 className="ad-headline-text" data-testid={`ad-headline-${ad.ad_id}`}>
                  {ad.headline}
                </h3>
              </div>
            )}

            <div className="ad-field text-field">
              <span className="field-label">Ad Text / Copy</span>
              <p className="ad-text-content" data-testid={`ad-text-${ad.ad_id}`}>
                {ad.ad_text || <span className="empty-val">(Empty copy)</span>}
              </p>
            </div>

            <div className="ad-field link-field">
              <span className="field-label">Destination URL</span>
              <a
                href={ad.destination_url}
                target="_blank"
                rel="noopener noreferrer"
                className="destination-url-link"
                data-testid={`ad-url-${ad.ad_id}`}
                title={ad.destination_url}
              >
                {ad.destination_url}
              </a>
            </div>

            {ad.creative_file && (
              <div className="ad-field creative-field">
                <span className="field-label">Creative Asset</span>
                <span className="creative-filename" data-testid={`ad-creative-${ad.ad_id}`}>
                  🖼️ {ad.creative_file}
                </span>
              </div>
            )}

            {ad.row_hash && (
              <div className="ad-hash-meta">
                <span className="hash-label">Content Hash:</span>{" "}
                <code title={ad.row_hash} data-testid={`ad-hash-${ad.ad_id}`}>
                  {ad.row_hash.slice(0, 10)}...
                </code>
              </div>
            )}

            {(ad.evidence_note || ad.evidence_file) && (
              <div className="ad-evidence-preview" data-testid={`ad-evidence-preview-${ad.ad_id}`}>
                <span className="evidence-preview-label">Approval Evidence:</span>{" "}
                {ad.evidence_note && <span className="evidence-preview-note" data-testid="evidence-note-text">{ad.evidence_note}</span>}
                {ad.evidence_file && (
                  <span className="evidence-preview-file" data-testid="evidence-file-ref" title={`Stored reference: ${ad.evidence_file}`}>
                    📎 {ad.evidence_file.slice(0, 12)}...
                  </span>
                )}
              </div>
            )}

            {/* Person Alerts Panel */}
            {Array.isArray(ad.alerts) && ad.alerts.length > 0 && (
              <div className="ad-alerts-panel" data-testid={`ad-alerts-panel-${ad.ad_id}`}>
                {ad.alerts.map((al) => (
                  <div
                    key={al.alert_id}
                    className={`person-alert-item alert-severity-${al.severity || "warning"} ${al.resolved ? "resolved" : "unresolved"}`}
                    data-testid={`alert-item-${al.alert_id}`}
                  >
                    <div className="alert-header-row">
                      <span className="alert-type-badge">
                        {al.alert_type === "platform_rejection" ? "⛔ Platform Rejection" :
                         al.alert_type === "uncertain_send_state" ? "⚠️ Uncertain Send State" :
                         al.alert_type === "stuck_sending" ? "⏳ Stuck Sending" :
                         al.alert_type === "dead_letter" ? "📫 Dead Letter" :
                         al.alert_type === "uncertain_verdict" ? "👤 Uncertain Verdict" :
                         `⚠️ ${al.alert_type}`}
                      </span>
                      {al.resolved ? (
                        <span className="alert-resolved-tag">✓ Resolved</span>
                      ) : (
                        onResolveAlert && (
                          <button
                            type="button"
                            className="btn-resolve-alert"
                            onClick={() => onResolveAlert(al.alert_id, ad)}
                            data-testid={`btn-resolve-alert-${al.alert_id}`}
                            title="Acknowledge and mark alert resolved"
                          >
                            Resolve Alert
                          </button>
                        )
                      )}
                    </div>
                    <div className="alert-msg-text" data-testid={`alert-msg-${al.alert_id}`}>
                      {al.message}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Reviewer Action Bar */}
          <div className="ad-actions-bar" data-testid={`ad-actions-${ad.ad_id}`}>
            {(ad.approval_state === "approved" || ad.is_approved) && onSend && (
              <button
                type="button"
                className="btn-action btn-send"
                onClick={() => onSend(ad)}
                data-testid={`btn-send-${ad.ad_id}`}
                title="Send approved ad to stand-in platform (dry run)"
              >
                🚀 Send
              </button>
            )}
            {ad.approval_state === "dead_letter" && (onRetry || onSend) && (
              <button
                type="button"
                className="btn-action btn-retry-send"
                onClick={() => (onRetry ? onRetry(ad) : onSend(ad))}
                data-testid={`btn-retry-send-${ad.ad_id}`}
                title="Retry failed ad send with backoff and timeouts"
              >
                🔄 Retry Send
              </button>
            )}
            {ad.approval_state === "dead_letter" && onReturnToReview && (
              <button
                type="button"
                className="btn-action btn-return-review"
                onClick={() => onReturnToReview(ad)}
                data-testid={`btn-return-review-${ad.ad_id}`}
                title="Return dead-letter ad back to pending review for copy edit"
              >
                ↩️ Return to Review
              </button>
            )}
            <button
              type="button"
              className={`btn-action btn-approve ${ad.approval_state === "sent" || ad.approval_state === "sending" ? "disabled" : ""}`}
              onClick={() => {
                if (ad.approval_state !== "sent" && ad.approval_state !== "sending" && onApprove) {
                  onApprove(ad);
                }
              }}
              disabled={ad.approval_state === "sent" || ad.approval_state === "sending"}
              data-testid={`btn-approve-${ad.ad_id}`}
              title={ad.approval_state === "sent" ? "Ad already sent" : "Approve this ad for dry-run send"}
            >
              ✓ Approve
            </button>
            <button
              type="button"
              className={`btn-action btn-edit ${ad.approval_state === "sent" || ad.approval_state === "sending" ? "disabled" : ""}`}
              onClick={() => {
                if (ad.approval_state !== "sent" && ad.approval_state !== "sending" && onEdit) {
                  onEdit(ad);
                }
              }}
              disabled={ad.approval_state === "sent" || ad.approval_state === "sending"}
              data-testid={`btn-edit-${ad.ad_id}`}
              title={ad.approval_state === "sent" ? "Cannot edit sent ad" : "Edit ad copy or headline (triggers re-check)"}
            >
              ✏️ Edit
            </button>
            <button
              type="button"
              className={`btn-action btn-reject ${ad.approval_state === "sent" || ad.approval_state === "sending" ? "disabled" : ""}`}
              onClick={() => {
                if (ad.approval_state !== "sent" && ad.approval_state !== "sending" && onReject) {
                  onReject(ad);
                }
              }}
              disabled={ad.approval_state === "sent" || ad.approval_state === "sending"}
              data-testid={`btn-reject-${ad.ad_id}`}
              title={ad.approval_state === "sent" ? "Cannot reject sent ad" : "Reject this ad with reason"}
            >
              ✕ Reject
            </button>
            {onViewTrace && (
              <button
                type="button"
                className="btn-action btn-trace"
                onClick={() => onViewTrace(ad)}
                data-testid={`btn-trace-${ad.ad_id}`}
                title="View execution and verdict trace"
              >
                🔍 Trace
              </button>
            )}
            {onAttachEvidence && (
              <button
                type="button"
                className="btn-action btn-evidence"
                onClick={() => onAttachEvidence(ad)}
                data-testid={`btn-evidence-${ad.ad_id}`}
                title="Attach client approval note or screenshot"
              >
                📎 Evidence
              </button>
            )}
            {onRecheck && (
              <button
                type="button"
                className="btn-action btn-recheck"
                onClick={() => onRecheck(ad)}
                data-testid={`btn-recheck-${ad.ad_id}`}
                title="Re-check edited ad in bounded loop"
              >
                🔄 Re-check
              </button>
            )}
          </div>
        </div>

        {/* Right Column: Findings Beside the Ad */}
        <div className="findings-pane" data-testid={`findings-pane-${ad.ad_id}`}>
          <div className="findings-header">
            <h4>
              Findings & Diagnostics{" "}
              <span
                className={`findings-count-badge ${findings.length > 0 ? "has-findings" : "zero-findings"}`}
                data-testid={`findings-count-${ad.ad_id}`}
              >
                {findings.length}
              </span>
            </h4>
          </div>

          {findings.length === 0 ? (
            <div className="findings-clean-state" data-testid={`findings-clean-${ad.ad_id}`}>
              <span className="clean-icon">✓</span>
              <p>No issues detected.</p>
              <small>Ad passed UTM, platform limits, and landing page checks.</small>
            </div>
          ) : (
            <ul className="findings-list" data-testid={`findings-list-${ad.ad_id}`}>
              {findings.map((f, idx) => (
                <li
                  key={f.finding_id || `finding-${idx}`}
                  className={`finding-item ${f.is_dismissed ? "finding-dismissed" : ""}`}
                  data-testid={`finding-item-${f.finding_id || idx}`}
                  data-check-type={f.check_type}
                  data-is-dismissed={Boolean(f.is_dismissed)}
                >
                  <div className="finding-item-header">
                    <span className="finding-check-tag" data-testid="finding-check-type">
                      {f.check_type}
                    </span>
                    {f.field && (
                      <span className="finding-field-tag" data-testid="finding-field">
                        field: {f.field}
                      </span>
                    )}
                    {f.is_dismissed && (
                      <span
                        className="finding-dismissed-pill"
                        data-testid="finding-dismissed-badge"
                        title="Dismissed as false alarm"
                      >
                        ✓ False Alarm
                      </span>
                    )}
                  </div>

                  <p className="finding-message" data-testid="finding-message">
                    {f.message}
                  </p>

                  {f.proposed_fix && (
                    <div className="finding-proposed-fix" data-testid="finding-proposed-fix">
                      <span className="fix-label">Suggested fix:</span>{" "}
                      <code className="fix-code">{f.proposed_fix}</code>
                    </div>
                  )}

                  {(f.rule_id || f.file_ref) && (
                    <div className="finding-citation-bar" data-testid="finding-citation">
                      {f.rule_id && (
                        <span className="citation-item" title="Citing rule entry">
                          📖 <strong>Rule:</strong> {f.rule_id}
                        </span>
                      )}
                      {f.file_ref && (
                        <span className="citation-item" title="Brand book / claims register reference">
                          📁 <strong>Ref:</strong> {f.file_ref}
                        </span>
                      )}
                    </div>
                  )}

                  {f.is_dismissed ? (
                    <div
                      className="finding-dismissed-box"
                      data-testid={`finding-dismissed-${f.finding_id || idx}`}
                    >
                      <span className="dismissed-label">Dismissed Reason:</span>{" "}
                      <span className="dismissed-reason-text" data-testid="dismissal-reason">
                        {f.dismissal_reason || "False alarm"}
                      </span>
                      {f.dismissed_by && (
                        <div className="dismissed-by-tag" data-testid="dismissed-by">
                          Dismissed by: {f.dismissed_by}
                        </div>
                      )}
                    </div>
                  ) : (
                    onDismissFinding && (
                      <div className="finding-actions-bar">
                        <button
                          type="button"
                          className="btn-dismiss-finding"
                          onClick={() => onDismissFinding(ad, f)}
                          data-testid={`btn-dismiss-finding-${f.finding_id || idx}`}
                          title="Dismiss finding as false alarm with reason"
                        >
                          ✕ Dismiss (False Alarm)
                        </button>
                      </div>
                    )
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </article>
  );
}
