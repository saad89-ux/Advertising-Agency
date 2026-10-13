"use client";

import React, { useState, useEffect, useMemo } from "react";
import BatchSummary from "./BatchSummary";
import AdCard from "./AdCard";
import RejectedRowsList from "./RejectedRowsList";

/**
 * BatchReviewScreen Component
 * Full review screen showing batch summary, filter/search controls,
 * isolated rejected rows, and each ad beside its findings.
 */
export default function BatchReviewScreen({
  batchId,
  initialBatch = null,
  apiBaseUrl = "http://localhost:8000",
  authToken = null,
  onApproveAd,
  onEditAd,
  onRejectAd,
  onRecheckAd,
  onDismissFinding,
  onAttachEvidence,
  onSendAd,
  onSendBatch,
  onRetryAd,
  onReturnToReviewAd,
}) {
  const [batch, setBatch] = useState(initialBatch);
  const [loading, setLoading] = useState(!initialBatch && !!batchId);
  const [error, setError] = useState(null);
  const [statusCode, setStatusCode] = useState(null);
  const [liveStatus, setLiveStatus] = useState("disconnected");

  // Filters & Search
  const [verdictFilter, setVerdictFilter] = useState("all");
  const [clientFilter, setClientFilter] = useState("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [showRejected, setShowRejected] = useState(false);

  // Action modals state
  const [activeModal, setActiveModal] = useState(null); // 'edit' | 'reject' | 'approve' | 'trace' | 'evidence' | 'dismiss'
  const [selectedAd, setSelectedAd] = useState(null);
  const [selectedFinding, setSelectedFinding] = useState(null);
  const [modalInputReason, setModalInputReason] = useState("");
  const [evidenceFile, setEvidenceFile] = useState(null);
  const [editFields, setEditFields] = useState({ headline: "", ad_text: "" });
  const [actionSuccessMsg, setActionSuccessMsg] = useState("");

  // Fetch batch details if not provided via props
  useEffect(() => {
    if (initialBatch) {
      setBatch(initialBatch);
      setLoading(false);
      return;
    }

    if (!batchId) return;

    let isMounted = true;
    setLoading(true);
    setError(null);
    setStatusCode(null);

    const token =
      authToken ||
      (typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null);

    const headers = {
      Accept: "application/json",
    };
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    fetch(`${apiBaseUrl}/batches/${batchId}`, { headers, credentials: "include" })
      .then(async (res) => {
        if (!isMounted) return;
        setStatusCode(res.status);
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          const msg =
            errData.detail ||
            (res.status === 401
              ? "Authentication required. Please sign in."
              : res.status === 403
              ? "Access forbidden: No ads in this batch belong to your assigned clients."
              : res.status === 404
              ? "Batch not found."
              : `Failed to load batch: HTTP ${res.status}`);
          throw new Error(msg);
        }
        return res.json();
      })
      .then((data) => {
        if (isMounted) {
          setBatch(data);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (isMounted) {
          setError(err.message);
          setLoading(false);
        }
      });

    return () => {
      isMounted = false;
    };
  }, [batchId, initialBatch, authToken, apiBaseUrl]);

  // SSE Live Status Stream
  useEffect(() => {
    if (!batchId || typeof window === "undefined" || typeof EventSource === "undefined") {
      return;
    }

    const token =
      authToken ||
      (typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null);

    const sseUrl = `${apiBaseUrl}/batches/${batchId}/events${
      token ? `?token=${encodeURIComponent(token)}` : ""
    }`;

    let es;
    try {
      es = new EventSource(sseUrl, { withCredentials: true });
      setLiveStatus("connecting");

      es.addEventListener("connected", () => {
        setLiveStatus("live");
      });

      es.addEventListener("batch_status", (e) => {
        try {
          const payload = JSON.parse(e.data);
          setBatch((prev) => {
            if (!prev) return prev;
            return {
              ...prev,
              summary: {
                ...prev.summary,
                verdict_counts: payload.verdict_counts || prev.summary?.verdict_counts,
                total_findings: payload.total_findings ?? prev.summary?.total_findings,
              },
            };
          });
          if (payload.status === "completed") {
            setLiveStatus("completed");
          } else {
            setLiveStatus("live");
          }
        } catch (_) {}
      });

      es.addEventListener("ad_status", (e) => {
        try {
          const payload = JSON.parse(e.data);
          setBatch((prev) => {
            if (!prev || !Array.isArray(prev.ads)) return prev;
            const updatedAds = prev.ads.map((ad) => {
              if (ad.ad_id === payload.ad_id) {
                return {
                  ...ad,
                  verdict: payload.verdict || ad.verdict,
                  approval_state: payload.approval_state || ad.approval_state,
                };
              }
              return ad;
            });
            return { ...prev, ads: updatedAds };
          });
        } catch (_) {}
      });

      es.addEventListener("complete", () => {
        setLiveStatus("completed");
      });

      es.onerror = () => {
        setLiveStatus((prev) => (prev === "completed" ? "completed" : "error"));
      };
    } catch (_) {
      setLiveStatus("error");
    }

    return () => {
      if (es) {
        es.close();
      }
    };
  }, [batchId, apiBaseUrl, authToken]);

  // Count unresolved person alerts across ads
  const unresolvedAlertsCount = useMemo(() => {
    if (!batch || !Array.isArray(batch.ads)) return 0;
    let count = 0;
    for (const ad of batch.ads) {
      if (Array.isArray(ad.alerts)) {
        count += ad.alerts.filter((a) => !a.resolved).length;
      }
    }
    return count;
  }, [batch]);

  // Derived filtered ads
  const filteredAds = useMemo(() => {
    if (!batch || !Array.isArray(batch.ads)) return [];

    return batch.ads.filter((ad) => {
      // Verdict / Alerts filter
      if (verdictFilter === "alerts") {
        const hasUnresolved = (ad.alerts || []).some((al) => !al.resolved);
        if (!hasUnresolved) return false;
      } else if (verdictFilter === "dead_letter") {
        if (ad.approval_state !== "dead_letter") return false;
      } else if (verdictFilter !== "all") {
        const v = (ad.verdict || "pending").toLowerCase();
        const vf = verdictFilter.toLowerCase();
        if (v !== vf && v.replace(/[\s-]+/g, "_") !== vf && vf.replace(/[\s-]+/g, "_") !== v) return false;
      }

      // Client filter
      if (clientFilter !== "all" && ad.client_id !== clientFilter) {
        return false;
      }

      // Search query filter (matches headline, text, row number, destination url)
      if (searchQuery.trim()) {
        const query = searchQuery.toLowerCase().trim();
        const matchesHeadline = ad.headline && ad.headline.toLowerCase().includes(query);
        const matchesText = ad.ad_text && ad.ad_text.toLowerCase().includes(query);
        const matchesRow = String(ad.row_number) === query;
        const matchesUrl = ad.destination_url && ad.destination_url.toLowerCase().includes(query);
        const matchesPlatform = ad.platform && ad.platform.toLowerCase().includes(query);
        if (!matchesHeadline && !matchesText && !matchesRow && !matchesUrl && !matchesPlatform) {
          return false;
        }
      }

      return true;
    });
  }, [batch, verdictFilter, clientFilter, searchQuery]);

  // Unique client IDs present in the ads list
  const availableClients = useMemo(() => {
    if (!batch || !Array.isArray(batch.ads)) return [];
    return Array.from(new Set(batch.ads.map((a) => a.client_id).filter(Boolean)));
  }, [batch]);

  // Action handlers
  const handleOpenApprove = (ad) => {
    setSelectedAd(ad);
    setActiveModal("approve");
    setModalInputReason("");
  };

  const handleOpenEdit = (ad) => {
    setSelectedAd(ad);
    setEditFields({
      headline: ad.headline || "",
      ad_text: ad.ad_text || "",
    });
    setModalInputReason("");
    setActiveModal("edit");
  };

  const handleOpenReject = (ad) => {
    setSelectedAd(ad);
    setModalInputReason("");
    setActiveModal("reject");
  };

  const handleOpenTrace = (ad) => {
    setSelectedAd(ad);
    setActiveModal("trace");
  };

  const handleOpenEvidence = (ad) => {
    setSelectedAd(ad);
    setModalInputReason(ad.evidence_note || "");
    setEvidenceFile(null);
    setActiveModal("evidence");
  };

  const handleOpenDismissFinding = (ad, finding) => {
    setSelectedAd(ad);
    setSelectedFinding(finding);
    setModalInputReason("");
    setActiveModal("dismiss");
  };

  const handleRecheck = (ad) => {
    if (onRecheckAd) {
      onRecheckAd(ad);
      return;
    }
    const token =
      authToken ||
      (typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null);
    const authHeaders = {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    };
    if (apiBaseUrl && ad.ad_id) {
      fetch(`${apiBaseUrl}/ads/${ad.ad_id}/recheck`, {
        method: "POST",
        headers: authHeaders,
      })
        .then((res) => res.json())
        .then((res) => {
          if (res && res.ad_id) {
            setBatch((prev) => {
              if (!prev || !prev.ads) return prev;
              return {
                ...prev,
                ads: prev.ads.map((a) =>
                  a.ad_id === res.ad_id
                    ? {
                        ...a,
                        verdict: res.verdict,
                        findings: res.findings || [],
                        edit_iteration: res.edit_iteration,
                      }
                    : a
                ),
              };
            });
            setActionSuccessMsg(`Ad #${ad.row_number} re-check completed: verdict is ${res.verdict}.`);
          }
        })
        .catch(() => {});
    }
  };

  const handleSendAd = (ad) => {
    if (onSendAd) {
      onSendAd(ad);
      return;
    }
    const token =
      authToken ||
      (typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null);
    const authHeaders = {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    };
    if (apiBaseUrl && ad.ad_id) {
      fetch(`${apiBaseUrl}/ads/${ad.ad_id}/send`, {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ dry_run: true }),
      })
        .then((res) => res.json())
        .then((data) => {
          if (data && data.status) {
            setBatch((prev) => {
              if (!prev || !prev.ads) return prev;
              return {
                ...prev,
                ads: prev.ads.map((a) =>
                  a.ad_id === ad.ad_id
                    ? {
                        ...a,
                        approval_state: data.status,
                        is_sent: data.status === "sent",
                        sent_at: data.sent_at || (data.status === "sent" ? new Date().toISOString() : null),
                      }
                    : a
                ),
              };
            });
            if (data.status === "sent") {
              setActionSuccessMsg(`Ad #${ad.row_number} sent to stand-in platform (dry run).`);
            } else if (data.status === "rejected") {
              setActionSuccessMsg(`Ad #${ad.row_number} rejected by platform: ${data.reason}.`);
            } else if (data.status === "dead_letter") {
              setActionSuccessMsg(`Ad #${ad.row_number} send retries exhausted (moved to dead letter queue).`);
            }
          }
        })
        .catch(() => {});
    }
  };

  const handleRetryAd = (ad) => {
    if (onRetryAd) {
      onRetryAd(ad);
      return;
    }
    const token =
      authToken ||
      (typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null);
    const authHeaders = {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    };
    if (apiBaseUrl && ad.ad_id) {
      fetch(`${apiBaseUrl}/ads/${ad.ad_id}/retry`, {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ dry_run: true }),
      })
        .then((res) => res.json())
        .then((data) => {
          if (data && data.status) {
            setBatch((prev) => {
              if (!prev || !prev.ads) return prev;
              return {
                ...prev,
                ads: prev.ads.map((a) =>
                  a.ad_id === ad.ad_id
                    ? {
                        ...a,
                        approval_state: data.status,
                        is_sent: data.status === "sent",
                        sent_at: data.sent_at || (data.status === "sent" ? new Date().toISOString() : null),
                      }
                    : a
                ),
              };
            });
            if (data.status === "sent") {
              setActionSuccessMsg(`Ad #${ad.row_number} successfully sent after retry.`);
            } else if (data.status === "dead_letter") {
              setActionSuccessMsg(`Ad #${ad.row_number} retry exhausted (dead letter).`);
            } else {
              setActionSuccessMsg(`Ad #${ad.row_number} retry status: ${data.status}.`);
            }
          }
        })
        .catch(() => {});
    }
  };

  const handleReturnToReview = (ad) => {
    if (onReturnToReviewAd) {
      onReturnToReviewAd(ad);
      return;
    }
    const token =
      authToken ||
      (typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null);
    const authHeaders = {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    };
    if (apiBaseUrl && ad.ad_id) {
      fetch(`${apiBaseUrl}/ads/${ad.ad_id}/return-to-review`, {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ reason: "Returned to review from dead letter" }),
      })
        .then((res) => res.json())
        .then((data) => {
          if (data && data.status === "pending_review") {
            setBatch((prev) => {
              if (!prev || !prev.ads) return prev;
              return {
                ...prev,
                ads: prev.ads.map((a) =>
                  a.ad_id === ad.ad_id
                    ? {
                        ...a,
                        approval_state: "pending_review",
                        is_approved: false,
                        is_sent: false,
                      }
                    : a
                ),
              };
            });
            setActionSuccessMsg(`Ad #${ad.row_number} returned to pending review.`);
          }
        })
        .catch(() => {});
    }
  };

  const handleResolveAlert = (alertId, ad) => {
    const token =
      authToken ||
      (typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null);
    const authHeaders = {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    };
    if (apiBaseUrl && alertId) {
      fetch(`${apiBaseUrl}/alerts/${alertId}/resolve`, {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ resolution_note: "Resolved by reviewer" }),
      })
        .then((res) => res.json())
        .then((data) => {
          if (data && data.resolved) {
            setBatch((prev) => {
              if (!prev || !prev.ads) return prev;
              return {
                ...prev,
                ads: prev.ads.map((a) => {
                  if (a.ad_id !== ad.ad_id) return a;
                  const updatedAlerts = (a.alerts || []).map((al) =>
                    al.alert_id === alertId ? { ...al, resolved: true } : al
                  );
                  return {
                    ...a,
                    alerts: updatedAlerts,
                    has_alerts: updatedAlerts.some((al) => !al.resolved),
                  };
                }),
              };
            });
            setActionSuccessMsg(`Alert resolved for Ad #${ad.row_number}.`);
          }
        })
        .catch(() => {});
    }
  };

  const handleSendBatch = () => {
    if (onSendBatch) {
      onSendBatch(batchId);
      return;
    }
    const token =
      authToken ||
      (typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null);
    const authHeaders = {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    };
    if (apiBaseUrl && batchId) {
      fetch(`${apiBaseUrl}/batches/${batchId}/send-approved`, {
        method: "POST",
        headers: authHeaders,
      })
        .then((res) => res.json())
        .then((data) => {
          if (data) {
            setActionSuccessMsg(`Batch send complete: ${data.sent} sent, ${data.rejected} rejected, ${data.dead_letter} dead letter.`);
          }
        })
        .catch(() => {});
    }
  };

  const handleConfirmAction = () => {
    if (!selectedAd) return;

    const token =
      authToken ||
      (typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null);
    const authHeaders = {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    };

    if (activeModal === "approve") {
      if (onApproveAd) {
        onApproveAd(selectedAd);
      } else if (apiBaseUrl && selectedAd.ad_id) {
        fetch(`${apiBaseUrl}/ads/${selectedAd.ad_id}/approve`, {
          method: "POST",
          headers: authHeaders,
          body: JSON.stringify({ content_hash: selectedAd.row_hash }),
        }).catch(() => {});
      }
      setActionSuccessMsg(`Ad #${selectedAd.row_number} approved.`);
    } else if (activeModal === "edit") {
      const editPayload = {
        headline: editFields.headline,
        ad_text: editFields.ad_text,
        reason: modalInputReason,
      };
      if (onEditAd) {
        onEditAd(selectedAd, editPayload);
      }
      if (apiBaseUrl && selectedAd.ad_id) {
        fetch(`${apiBaseUrl}/ads/${selectedAd.ad_id}/edit`, {
          method: "POST",
          headers: authHeaders,
          body: JSON.stringify(editPayload),
        })
          .then((res) => res.json())
          .then((updated) => {
            if (updated && updated.ad_id) {
              setBatch((prev) => {
                if (!prev || !prev.ads) return prev;
                return {
                  ...prev,
                  ads: prev.ads.map((a) =>
                    a.ad_id === updated.ad_id
                      ? {
                          ...a,
                          headline: updated.headline,
                          ad_text: updated.ad_text,
                          row_hash: updated.new_row_hash,
                          verdict: "pending",
                        }
                      : a
                  ),
                };
              });
            }
          })
          .catch(() => {});
      }
      setActionSuccessMsg(`Edits and reason for Ad #${selectedAd.row_number} saved.`);
    } else if (activeModal === "reject") {
      const rejectPayload = { reason: modalInputReason };
      if (onRejectAd) {
        onRejectAd(selectedAd, rejectPayload);
      } else if (apiBaseUrl && selectedAd.ad_id) {
        fetch(`${apiBaseUrl}/ads/${selectedAd.ad_id}/reject`, {
          method: "POST",
          headers: authHeaders,
          body: JSON.stringify(rejectPayload),
        }).catch(() => {});
      }
      setActionSuccessMsg(`Ad #${selectedAd.row_number} rejected with reason.`);
    } else if (activeModal === "dismiss" && selectedFinding) {
      const dismissPayload = { reason: modalInputReason };
      if (onDismissFinding) {
        onDismissFinding(selectedAd, selectedFinding, dismissPayload);
      }
      if (apiBaseUrl && selectedAd.ad_id && selectedFinding.finding_id) {
        fetch(
          `${apiBaseUrl}/ads/${selectedAd.ad_id}/findings/${selectedFinding.finding_id}/dismiss`,
          {
            method: "POST",
            headers: authHeaders,
            body: JSON.stringify(dismissPayload),
          }
        )
          .then((res) => res.json())
          .then((data) => {
            if (data && data.finding_id) {
              setBatch((prev) => {
                if (!prev || !prev.ads) return prev;
                const newAds = prev.ads.map((a) => {
                  if (a.ad_id !== selectedAd.ad_id) return a;
                  const updatedFindings = (a.findings || []).map((f) =>
                    f.finding_id === data.finding_id
                      ? {
                          ...f,
                          is_dismissed: true,
                          dismissal_reason: data.reason,
                          dismissed_by: data.dismissed_by,
                          dismissed_at: data.created_at,
                        }
                      : f
                  );
                  return {
                    ...a,
                    findings: updatedFindings,
                    verdict: data.new_verdict || a.verdict,
                  };
                });
                return {
                  ...prev,
                  ads: newAds,
                  summary: prev.summary
                    ? {
                        ...prev.summary,
                        false_alarms_count: (prev.summary.false_alarms_count || 0) + 1,
                      }
                    : prev.summary,
                };
              });
            }
          })
          .catch(() => {});
      }
      setActionSuccessMsg(`Finding dismissed as false alarm for Ad #${selectedAd.row_number}.`);
    } else if (activeModal === "evidence") {
      const cleanNote = modalInputReason.trim();
      const payload = {
        evidence_note: cleanNote || null,
        content_hash: selectedAd.row_hash,
      };

      if (onAttachEvidence) {
        onAttachEvidence(selectedAd, { note: cleanNote, file: evidenceFile });
      }

      if (apiBaseUrl && selectedAd.ad_id) {
        if (evidenceFile) {
          const formData = new FormData();
          formData.append("file", evidenceFile);
          if (cleanNote) formData.append("note", cleanNote);
          if (selectedAd.row_hash) formData.append("content_hash", selectedAd.row_hash);

          const uploadHeaders = token ? { Authorization: `Bearer ${token}` } : {};

          fetch(`${apiBaseUrl}/ads/${selectedAd.ad_id}/evidence/upload`, {
            method: "POST",
            headers: uploadHeaders,
            body: formData,
          })
            .then((res) => res.json())
            .then((data) => {
              if (data && data.evidence_id) {
                setBatch((prev) => {
                  if (!prev || !prev.ads) return prev;
                  return {
                    ...prev,
                    ads: prev.ads.map((a) =>
                      a.ad_id === selectedAd.ad_id
                        ? {
                            ...a,
                            evidence_note: data.evidence_note,
                            evidence_file: data.evidence_file,
                            has_evidence: true,
                          }
                        : a
                    ),
                  };
                });
              }
            })
            .catch(() => {});
        } else if (cleanNote) {
          fetch(`${apiBaseUrl}/ads/${selectedAd.ad_id}/evidence`, {
            method: "POST",
            headers: authHeaders,
            body: JSON.stringify(payload),
          })
            .then((res) => res.json())
            .then((data) => {
              if (data && data.evidence_id) {
                setBatch((prev) => {
                  if (!prev || !prev.ads) return prev;
                  return {
                    ...prev,
                    ads: prev.ads.map((a) =>
                      a.ad_id === selectedAd.ad_id
                        ? {
                            ...a,
                            evidence_note: data.evidence_note,
                            evidence_file: data.evidence_file,
                            has_evidence: true,
                          }
                        : a
                    ),
                  };
                });
              }
            })
            .catch(() => {});
        }
      }
      setActionSuccessMsg(`Client approval evidence saved for Ad #${selectedAd.row_number}.`);
    }

    // Close modal
    setActiveModal(null);
    setSelectedAd(null);
    setSelectedFinding(null);
    setEvidenceFile(null);
    setModalInputReason("");
    setTimeout(() => setActionSuccessMsg(""), 4000);
  };

  // 1. Loading State
  if (loading) {
    return (
      <div className="review-screen-container loading-state" data-testid="review-loading">
        <div className="spinner" />
        <h2>Loading Batch Review Screen...</h2>
        <p>Fetching ads, findings, and isolated rows...</p>
      </div>
    );
  }

  // 2. Error States (401, 403, 404, or network error)
  if (error || !batch) {
    return (
      <div
        className={`review-screen-container error-state error-${statusCode || "unknown"}`}
        data-testid="review-error"
        data-status-code={statusCode}
      >
        <div className="error-card">
          <span className="error-icon">
            {statusCode === 403 ? "🔒" : statusCode === 401 ? "🔑" : "⚠️"}
          </span>
          <h2 data-testid="error-title">
            {statusCode === 401
              ? "Authentication Required (401)"
              : statusCode === 403
              ? "Access Forbidden (403)"
              : statusCode === 404
              ? "Batch Not Found (404)"
              : "Unable to Load Batch"}
          </h2>
          <p className="error-message" data-testid="error-message">
            {error || "An unknown error occurred while loading batch details."}
          </p>
          <div className="error-actions">
            <button
              type="button"
              className="btn-retry"
              onClick={() => {
                setError(null);
                setLoading(true);
              }}
              data-testid="btn-retry"
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  // 3. Render Batch Review Screen
  return (
    <div className="review-screen-container" data-testid="batch-review-screen">
      {/* Toast Notification for reviewer actions */}
      {actionSuccessMsg && (
        <div className="toast-notification" data-testid="action-toast">
          ✓ {actionSuccessMsg}
        </div>
      )}

      {/* Batch Summary Header */}
      <BatchSummary
        batch={batch}
        showRejected={showRejected}
        onToggleRejected={() => setShowRejected((prev) => !prev)}
        liveStatus={liveStatus}
      />

      {/* Isolated Rejected Rows Panel */}
      {showRejected && batch.rejected_rows && batch.rejected_rows.length > 0 && (
        <RejectedRowsList rejectedRows={batch.rejected_rows} />
      )}

      {/* Person Review Alerts Banner */}
      {unresolvedAlertsCount > 0 && (
        <div className="person-alerts-banner" data-testid="person-alerts-banner">
          <span className="alerts-banner-icon">🚨</span>
          <div className="alerts-banner-content">
            <strong>Person Review Required:</strong> {unresolvedAlertsCount} ad{unresolvedAlertsCount === 1 ? "" : "s"} flagged with platform rejections or uncertain send states. No automatic relaunch is permitted until a reviewer reviews each item.
          </div>
          <button
            type="button"
            className={`btn-filter-alerts ${verdictFilter === "alerts" ? "active" : ""}`}
            onClick={() => setVerdictFilter(verdictFilter === "alerts" ? "all" : "alerts")}
            data-testid="btn-filter-alerts"
          >
            {verdictFilter === "alerts" ? "Show All Ads" : `View ${unresolvedAlertsCount} Alert${unresolvedAlertsCount === 1 ? "" : "s"}`}
          </button>
        </div>
      )}

      {/* Reviewer Toolbar: Filters & Search */}
      <section className="reviewer-toolbar" data-testid="reviewer-toolbar">
        <div className="toolbar-row">
          {/* Verdict Filter Buttons */}
          <div className="verdict-filter-group" data-testid="verdict-filter-group">
            <span className="filter-label">Filter Verdict:</span>
            {[
              { id: "all", label: "All" },
              { id: "ready", label: "Ready" },
              { id: "fix_needed", label: "Fix Needed" },
              { id: "blocked", label: "Blocked" },
              { id: "needs_person", label: "Needs Person" },
              { id: "model_unavailable", label: "⏳ Model Unavail" },
              { id: "pending", label: "Pending" },
              { id: "dead_letter", label: "⚠️ Dead Letter" },
              ...(unresolvedAlertsCount > 0
                ? [{ id: "alerts", label: `🚨 Alerts (${unresolvedAlertsCount})` }]
                : []),
            ].map((v) => (
              <button
                key={v.id}
                type="button"
                className={`btn-filter-pill ${verdictFilter === v.id ? "active" : ""}`}
                onClick={() => setVerdictFilter(v.id)}
                data-testid={`filter-verdict-${v.id}`}
              >
                {v.label}
              </button>
            ))}
          </div>

          {/* Client Filter Dropdown */}
          {availableClients.length > 1 && (
            <div className="client-filter-group" data-testid="client-filter-group">
              <label htmlFor="client-select" className="filter-label">
                Client:
              </label>
              <select
                id="client-select"
                className="select-client"
                value={clientFilter}
                onChange={(e) => setClientFilter(e.target.value)}
                data-testid="client-filter-select"
              >
                <option value="all">All Clients ({availableClients.length})</option>
                {availableClients.map((cid) => (
                  <option key={cid} value={cid}>
                    {cid}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Search Box */}
          <div className="search-group" data-testid="search-group">
            <input
              type="text"
              className="input-search"
              placeholder="Search headline, copy, row #, URL..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              data-testid="ad-search-input"
            />
            {searchQuery && (
              <button
                type="button"
                className="btn-clear-search"
                onClick={() => setSearchQuery("")}
                title="Clear search"
                data-testid="btn-clear-search"
              >
                ✕
              </button>
            )}
          </div>
        </div>

        <div className="toolbar-meta">
          <span className="visible-count-label" data-testid="visible-count-label">
            Showing <strong>{filteredAds.length}</strong> of{" "}
            <strong>{batch.ads ? batch.ads.length : 0}</strong> ads
          </span>
          {batch.ads && batch.ads.some((a) => a.approval_state === "approved" || a.is_approved) && (
            <button
              type="button"
              className="btn-send-batch"
              onClick={handleSendBatch}
              data-testid="btn-send-batch"
              title="Send all approved ads in batch to stand-in platform (dry run)"
            >
              🚀 Send Approved Ads (Dry Run)
            </button>
          )}
        </div>
      </section>

      {/* Ads List: Side-by-side Ad beside Findings */}
      <main className="ads-review-list" data-testid="ads-review-list">
        {filteredAds.length === 0 ? (
          <div className="no-ads-found" data-testid="no-ads-found">
            <span className="empty-icon">🔍</span>
            <h3>No ads match the current filter</h3>
            <p>Try resetting the verdict filter or clearing the search query.</p>
            <button
              type="button"
              className="btn-reset-filters"
              onClick={() => {
                setVerdictFilter("all");
                setClientFilter("all");
                setSearchQuery("");
              }}
              data-testid="btn-reset-filters"
            >
              Reset Filters
            </button>
          </div>
        ) : (
          filteredAds.map((ad) => (
            <AdCard
              key={ad.ad_id}
              ad={ad}
              onApprove={handleOpenApprove}
              onEdit={handleOpenEdit}
              onReject={handleOpenReject}
              onSend={handleSendAd}
              onRetry={handleRetryAd}
              onReturnToReview={handleReturnToReview}
              onResolveAlert={handleResolveAlert}
              onViewTrace={handleOpenTrace}
              onAttachEvidence={handleOpenEvidence}
              onRecheck={handleRecheck}
              onDismissFinding={handleOpenDismissFinding}
            />
          ))
        )}
      </main>

      {/* Action Modals */}
      {activeModal && selectedAd && (
        <div className="modal-backdrop" data-testid="action-modal">
          <div className="modal-content" data-testid={`modal-${activeModal}`}>
            <div className="modal-header">
              <h3>
                {activeModal === "approve" && `Approve Ad #${selectedAd.row_number}`}
                {activeModal === "edit" && `Edit Ad #${selectedAd.row_number}`}
                {activeModal === "reject" && `Reject Ad #${selectedAd.row_number}`}
                {activeModal === "dismiss" && `Dismiss Finding (False Alarm) — Ad #${selectedAd.row_number}`}
                {activeModal === "trace" && `Verdict Trace — Ad #${selectedAd.row_number}`}
                {activeModal === "evidence" && `Attach Client Approval — Ad #${selectedAd.row_number}`}
              </h3>
              <button
                type="button"
                className="btn-modal-close"
                onClick={() => setActiveModal(null)}
                data-testid="btn-modal-close"
              >
                ✕
              </button>
            </div>

            <div className="modal-body">
              {/* Approve Confirmation */}
              {activeModal === "approve" && (
                <div className="approve-body">
                  <p>
                    Are you sure you want to approve this ad for stand-in platform dry-run send?
                  </p>
                  <div className="content-hash-info">
                    <strong>Approved Content Hash:</strong>{" "}
                    <code>{selectedAd.row_hash || "N/A"}</code>
                  </div>
                  <p className="note-text">
                    Per PRD D13, only the exact content version matching this hash can be sent.
                  </p>
                </div>
              )}

              {/* Edit Modal (PRD 4.6: edits and reasons kept, re-checked in bounded loop) */}
              {activeModal === "edit" && (
                <div className="edit-body">
                  <div className="form-group">
                    <label htmlFor="edit-headline">Headline:</label>
                    <input
                      id="edit-headline"
                      type="text"
                      className="modal-input"
                      value={editFields.headline}
                      onChange={(e) =>
                        setEditFields((prev) => ({ ...prev, headline: e.target.value }))
                      }
                      data-testid="input-edit-headline"
                    />
                  </div>

                  <div className="form-group">
                    <label htmlFor="edit-ad-text">Ad Text / Copy:</label>
                    <textarea
                      id="edit-ad-text"
                      rows={4}
                      className="modal-textarea"
                      value={editFields.ad_text}
                      onChange={(e) =>
                        setEditFields((prev) => ({ ...prev, ad_text: e.target.value }))
                      }
                      data-testid="textarea-edit-ad-text"
                    />
                  </div>

                  <div className="form-group">
                    <label htmlFor="edit-reason">
                      Reason for Edit <span className="required-star">*</span>:
                    </label>
                    <input
                      id="edit-reason"
                      type="text"
                      placeholder="e.g. Corrected typo; adjusted to 30-char headline limit"
                      className="modal-input"
                      value={modalInputReason}
                      onChange={(e) => setModalInputReason(e.target.value)}
                      data-testid="input-edit-reason"
                    />
                  </div>
                </div>
              )}

              {/* Reject Modal */}
              {activeModal === "reject" && (
                <div className="reject-body">
                  <p>Please specify why this ad is being rejected:</p>
                  <div className="form-group">
                    <label htmlFor="reject-reason">
                      Rejection Reason <span className="required-star">*</span>:
                    </label>
                    <textarea
                      id="reject-reason"
                      rows={3}
                      placeholder="e.g. Incompatible offer date; unsubstantiated medical claim."
                      className="modal-textarea"
                      value={modalInputReason}
                      onChange={(e) => setModalInputReason(e.target.value)}
                      data-testid="textarea-reject-reason"
                    />
                  </div>
                </div>
              )}

              {/* Dismiss Finding Modal (PRD 4.6, 4.7: false-alarm record with mandatory reason) */}
              {activeModal === "dismiss" && selectedFinding && (
                <div className="dismiss-body" data-testid="modal-dismiss-body">
                  <p>
                    Dismiss finding as a false alarm for Ad #{selectedAd.row_number}:
                  </p>
                  <div
                    className="finding-preview-meta"
                    style={{
                      padding: "8px 12px",
                      background: "#f8f9fa",
                      borderRadius: 4,
                      marginBottom: 12,
                      border: "1px solid #e2e8f0",
                    }}
                  >
                    <div>
                      <strong>Check:</strong> <code>{selectedFinding.check_type}</code>
                    </div>
                    <div>
                      <strong>Message:</strong> {selectedFinding.message}
                    </div>
                    {selectedFinding.field && (
                      <div>
                        <strong>Field:</strong> {selectedFinding.field}
                      </div>
                    )}
                  </div>
                  <div className="form-group">
                    <label htmlFor="dismiss-reason">
                      False-Alarm Reason <span className="required-star">*</span>:
                    </label>
                    <textarea
                      id="dismiss-reason"
                      rows={3}
                      placeholder="e.g. Brand guideline exception verified; landing page DNS redirect confirmed manually."
                      className="modal-textarea"
                      value={modalInputReason}
                      onChange={(e) => setModalInputReason(e.target.value)}
                      data-testid="textarea-dismiss-reason"
                    />
                  </div>
                </div>
              )}

              {/* Trace Modal */}
              {activeModal === "trace" && (
                <div className="trace-body">
                  <p>
                    Langfuse trace and workflow execution details for Ad <code>{selectedAd.ad_id}</code>.
                  </p>
                  <div className="trace-meta-box">
                    <div>
                      <strong>Batch ID:</strong> <code>{selectedAd.batch_id}</code>
                    </div>
                    <div>
                      <strong>Ad ID:</strong> <code>{selectedAd.ad_id}</code>
                    </div>
                    <div>
                      <strong>Thread ID:</strong>{" "}
                      <code>{`${selectedAd.batch_id}:${selectedAd.ad_id}`}</code>
                    </div>
                    <div>
                      <strong>Resolved Verdict:</strong>{" "}
                      <span className="verdict-tag">{selectedAd.verdict || "pending"}</span>
                    </div>
                  </div>
                </div>
              )}

              {/* Evidence Modal (PRD D3: note or screenshot linked to content hash) */}
              {activeModal === "evidence" && (
                <div className="evidence-body" data-testid="modal-evidence-body">
                  <p>
                    Attach reviewer note or screenshot evidencing client approval (PRD D3).
                  </p>
                  <div
                    className="content-hash-info"
                    style={{
                      marginBottom: 12,
                      padding: "8px 12px",
                      background: "#f8fafc",
                      borderRadius: 4,
                      border: "1px solid #e2e8f0",
                    }}
                  >
                    <strong>Linked Content Hash:</strong>{" "}
                    <code data-testid="evidence-hash">{selectedAd.row_hash || "N/A"}</code>
                  </div>
                  <div className="form-group">
                    <label htmlFor="evidence-note">Reviewer Note / Context:</label>
                    <textarea
                      id="evidence-note"
                      rows={3}
                      placeholder="e.g. Approved via Slack call with Tobias at 14:30."
                      className="modal-textarea"
                      value={modalInputReason}
                      onChange={(e) => setModalInputReason(e.target.value)}
                      data-testid="textarea-evidence-note"
                    />
                  </div>
                  <div className="form-group">
                    <label htmlFor="evidence-file-input">
                      Screenshot / Document File (PNG, JPEG, WEBP, PDF):
                    </label>
                    <input
                      id="evidence-file-input"
                      type="file"
                      accept=".png,.jpg,.jpeg,.webp,.pdf,image/png,image/jpeg,image/webp,application/pdf"
                      className="modal-file-input"
                      onChange={(e) =>
                        setEvidenceFile(e.target.files ? e.target.files[0] : null)
                      }
                      data-testid="input-evidence-file"
                    />
                    {evidenceFile && (
                      <small className="selected-filename" style={{ display: "block", marginTop: 4, color: "#065f46" }}>
                        Selected: {evidenceFile.name} ({(evidenceFile.size / 1024).toFixed(1)} KB)
                      </small>
                    )}
                  </div>
                </div>
              )}
            </div>

            <div className="modal-footer">
              <button
                type="button"
                className="btn-modal-cancel"
                onClick={() => setActiveModal(null)}
                data-testid="btn-modal-cancel"
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn-modal-confirm"
                onClick={handleConfirmAction}
                disabled={
                  ((activeModal === "edit" || activeModal === "reject" || activeModal === "dismiss") &&
                    !modalInputReason.trim()) ||
                  (activeModal === "evidence" && !modalInputReason.trim() && !evidenceFile)
                }
                data-testid="btn-modal-confirm"
              >
                {activeModal === "approve"
                  ? "Confirm Approval"
                  : activeModal === "edit"
                  ? "Save & Re-Check"
                  : activeModal === "reject"
                  ? "Confirm Rejection"
                  : activeModal === "dismiss"
                  ? "Confirm Dismissal"
                  : activeModal === "evidence"
                  ? "Save Evidence"
                  : "Done"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
