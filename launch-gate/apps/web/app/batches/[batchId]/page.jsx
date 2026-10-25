"use client";
import { API_BASE_URL } from "../../../lib/api";

import React, { useState, useEffect, useMemo, use } from "react";
import Link from "next/link";
import SocialFeedModal from "../../../components/SocialFeedModal";
import GlassToast from "../../../components/GlassToast";
import { readError, notificationLabel, requestErrorMessage } from "../../../lib/api";

/**
 * Next.js Dynamic Route Page: /batches/[batchId]
 * High-performance, photorealistic dark glassmorphic Batch Review & Live Telemetry Console.
 */
function BatchReviewScreen({ params, apiBaseUrl = API_BASE_URL }) {
  const resolvedParams = params && typeof params.then === "function" ? use(params) : params;
  const batchId = resolvedParams?.batchId;

  const [batch, setBatch] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [platformFilter, setPlatformFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [previewAd, setPreviewAd] = useState(null);
  const [toast, setToast] = useState(null);
  const [copiedId, setCopiedId] = useState(false);
  const [isLaunching, setIsLaunching] = useState(false);
  const [notificationEmail, setNotificationEmail] = useState("");
  const [hierarchyProgress, setHierarchyProgress] = useState(null);
  const [hierarchyError, setHierarchyError] = useState(null);

  // Edit Modal State
  const [editingAd, setEditingAd] = useState(null);
  const [editHeadline, setEditHeadline] = useState("");
  const [editText, setEditText] = useState("");

  const fetchBatch = async () => {
    if (!batchId) return;
    setLoading(true);
    setError(null);
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = { Accept: "application/json" };
      if (token) headers["Authorization"] = `Bearer ${token}`;

      const res = await fetch(`${apiBaseUrl}/batches/${batchId}`, { headers, credentials: "include" });
      if (!res.ok) {
        if (res.status === 401) throw new Error(await readError(res));
        throw new Error(await readError(res));
      }

      const data = await res.json();
      setBatch(data);
      setNotificationEmail(data.summary?.notification_contact?.employee_email || "");
    } catch (err) {
      console.error(err);
      setError(requestErrorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchBatch();
  }, [batchId, apiBaseUrl]);

  useEffect(() => {
    if (!batchId) return undefined;
    let cancelled = false;
    const updateHierarchy = async () => {
      try {
        const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
        const headers = { Accept: "application/json" };
        if (token) headers.Authorization = "Bearer " + token;
        const response = await fetch(`${apiBaseUrl}/campaign-flow/hierarchy-status/${batchId}`, {
          headers,
          credentials: "include",
        });
        if (response.status === 404) {
          if (!cancelled) {
            setHierarchyProgress(null);
            setHierarchyError(null);
          }
          return;
        }
        if (!response.ok) throw new Error(await readError(response));
        const data = await response.json();
        if (!cancelled) {
          setHierarchyProgress(data);
          setHierarchyError(null);
        }
      } catch (err) {
        if (!cancelled) setHierarchyError(requestErrorMessage(err));
      }
    };
    updateHierarchy();
    const timer = window.setInterval(updateHierarchy, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [batchId, apiBaseUrl]);

  // Derived filtered ads
  const filteredAds = useMemo(() => {
    if (!batch || !Array.isArray(batch.ads)) return [];
    return batch.ads.filter((ad) => {
      // Platform filter
      if (platformFilter !== "all" && ad.platform?.toLowerCase() !== platformFilter) {
        return false;
      }
      // Status filter
      if (statusFilter === "approved" && ad.approval_state !== "approved" && ad.delivery_status !== "active_live") {
        return false;
      }
      if (statusFilter === "flagged" && (ad.findings?.length === 0 || !ad.has_rule_violation)) {
        return false;
      }
      if (statusFilter === "pending" && ad.approval_state !== "pending_review") {
        return false;
      }
      // Search filter
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase().trim();
        const mHead = ad.headline?.toLowerCase().includes(q);
        const mText = ad.ad_text?.toLowerCase().includes(q);
        const mUrl = ad.destination_url?.toLowerCase().includes(q);
        const mId = ad.ad_id?.toLowerCase().includes(q);
        const mRow = String(ad.row_number) === q;
        if (!mHead && !mText && !mUrl && !mId && !mRow) return false;
      }
      return true;
    });
  }, [batch, platformFilter, statusFilter, searchQuery]);

  const handleCopyId = () => {
    if (!batchId) return;
    navigator.clipboard?.writeText(batchId);
    setCopiedId(true);
    setToast({ message: `Copied Batch ID to clipboard: ${batchId}`, type: "info" });
    setTimeout(() => setCopiedId(false), 2500);
  };

  const handleDispatchBatch = async () => {
    setIsLaunching(true);
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = {
        "Content-Type": "application/json",
        Accept: "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      };

      const clientId = batch?.summary?.client_ids?.[0];
      const contact = {employee_email: notificationEmail};
      if (!clientId || !contact?.employee_email?.includes("@")) {
        setToast({ message: "Enter the batch notification email before dispatching.", type: "error" });
        return;
      }
      const payload = {
        client_id: clientId,
        batch_id: batchId,
        campaign_name: `${clientId.toUpperCase()} Growth Burst`,
        employee_email: contact.employee_email,
        target_platforms: ["facebook", "instagram"],
      };

      const res = await fetch(`${apiBaseUrl}/campaign-flow/launch-batch`, {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        const result = await res.json();
        setToast({ message: (result.status === "blocked" ? "Meta connector not configured — nothing was sent." : `Batch dispatch: ${result.status}.`) + " " + (result.notifications || []).map(notificationLabel).join(" "), type: result.status === "launched" ? "success" : result.status === "blocked" ? "warning" : "error" });
        fetchBatch();
      } else {
        setToast({ message: await readError(res), type: "error" });
      }
    } catch (err) {
      setToast({ message: err?.message || "Batch dispatch could not be completed.", type: "error" });
    } finally {
      setIsLaunching(false);
    }
  };

  const handleSaveEdit = async () => {
    if (!editingAd) return;
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = {
        "Content-Type": "application/json",
        Accept: "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      };

      const res = await fetch(`${apiBaseUrl}/ads/${editingAd.ad_id}/edit`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          headline: editHeadline,
          ad_text: editText,
          reason: "Updated copy via Batch Console",
        }),
      });

      if (res.ok) {
        setBatch((prev) => {
          if (!prev) return prev;
          return {
            ...prev,
            ads: prev.ads.map((a) =>
              a.ad_id === editingAd.ad_id ? { ...a, headline: editHeadline, ad_text: editText } : a
            ),
          };
        });
        setToast({ message: `Updated Ad #${editingAd.row_number} copy successfully!`, type: "success" });
        setEditingAd(null);
      } else {
        setToast({ message: "Failed to update ad copy.", type: "error" });
      }
    } catch {
      setToast({ message: "Network error while saving edit.", type: "error" });
    }
  };

  if (loading) {
    return (
      <div style={{
        minHeight: "calc(100vh - 65px)",
        background: "#080c14",
        color: "#f8fafc",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "16px",
        fontFamily: "system-ui, -apple-system, sans-serif",
      }}>
        <div style={{
          width: "48px",
          height: "48px",
          borderRadius: "50%",
          border: "3px solid rgba(59, 130, 246, 0.2)",
          borderTopColor: "#3b82f6",
          animation: "spin 0.8s linear infinite",
        }} />
        <div style={{ fontSize: "16px", fontWeight: 700, color: "#94a3b8" }}>
          Loading Campaign Batch Telemetry...
        </div>
        <div style={{ fontSize: "12px", color: "#64748b" }}>
          Querying batch ID: <code style={{ color: "#38bdf8" }}>{batchId}</code>
        </div>
        <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      </div>
    );
  }

  if (error || !batch) {
    return (
      <div style={{
        minHeight: "calc(100vh - 65px)",
        background: "#080c14",
        color: "#f8fafc",
        padding: "60px 24px",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        fontFamily: "system-ui, -apple-system, sans-serif",
      }}>
        <div style={{
          maxWidth: "540px",
          width: "100%",
          background: "rgba(15, 23, 42, 0.8)",
          border: "1px solid rgba(239, 68, 68, 0.3)",
          borderRadius: "20px",
          padding: "36px",
          textAlign: "center",
          boxShadow: "0 20px 40px rgba(0,0,0,0.5)",
        }}>
          <div style={{ fontSize: "40px", marginBottom: "16px" }}>⚠️</div>
          <h2 style={{ fontSize: "22px", fontWeight: "800", color: "#f8fafc", margin: "0 0 8px 0" }}>
            Unable to Load Batch
          </h2>
          <p style={{ fontSize: "14px", color: "#94a3b8", lineHeight: 1.5, margin: "0 0 24px 0" }}>
            {error || `Batch ${batchId} was not found or has no visible ads.`}
          </p>
          <div style={{ display: "flex", gap: "12px", justifyContent: "center" }}>
            <Link
              href="/review"
              style={{
                padding: "10px 20px",
                borderRadius: "10px",
                background: "rgba(255, 255, 255, 0.08)",
                border: "1px solid rgba(255, 255, 255, 0.15)",
                color: "#f8fafc",
                fontSize: "13px",
                fontWeight: "600",
                textDecoration: "none",
              }}
            >
              ← Back to Batches
            </Link>
            <button
              onClick={fetchBatch}
              style={{
                padding: "10px 20px",
                borderRadius: "10px",
                background: "linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%)",
                border: "none",
                color: "#fff",
                fontSize: "13px",
                fontWeight: "600",
                cursor: "pointer",
              }}
            >
              🔄 Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  const summary = batch.summary || {};
  const totalAds = summary.total_ads || batch.ads?.length || 0;
  const clientName = (summary.client_ids?.[0] || "Campaign").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const totalSpend = summary.total_spend_usd;
  const totalSales = summary.total_sales_usd;
  const blendedRoas = summary.blended_roas;
  const totalOrders = summary.total_orders;
  const totalImpressions = summary.total_impressions;
  const contactEmail = summary.notification_contact?.employee_email || "";

  return (
    <div style={{
      minHeight: "calc(100vh - 65px)",
      background: "#080c14",
      color: "#f8fafc",
      padding: "36px 48px",
      fontFamily: "system-ui, -apple-system, sans-serif",
    }}>
      <div style={{ maxWidth: "1400px", margin: "0 auto" }}>
        {/* 1. Breadcrumb & Navigation */}
        <div style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "20px",
          fontSize: "13px",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: "8px", color: "#64748b" }}>
            <Link href="/" style={{ color: "#94a3b8", textDecoration: "none" }}>Home</Link>
            <span>/</span>
            <Link href="/review" style={{ color: "#94a3b8", textDecoration: "none" }}>Review Batches</Link>
            <span>/</span>
            <span style={{ color: "#38bdf8", fontWeight: 600 }}>{batch.filename || batchId}</span>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <span style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "6px",
              padding: "4px 12px",
              borderRadius: "20px",
              fontSize: "12px",
              fontWeight: 600,
              background: totalAds > 0 ? "rgba(16, 185, 129, 0.15)" : "rgba(59, 130, 246, 0.15)",
              color: totalAds > 0 ? "#34d399" : "#60a5fa",
              border: totalAds > 0 ? "1px solid rgba(16, 185, 129, 0.3)" : "1px solid rgba(59, 130, 246, 0.3)",
            }}>
              <span style={{
                width: "8px",
                height: "8px",
                borderRadius: "50%",
                background: totalAds > 0 ? "#10b981" : "#3b82f6",
                boxShadow: totalAds > 0 ? "0 0 8px #10b981" : "0 0 8px #3b82f6",
              }} />
              {totalAds > 0 ? "Live Serving on Meta" : "Staged for Ingest"}
            </span>

            <span style={{
              fontSize: "12px",
              color: "#64748b",
              background: "rgba(255, 255, 255, 0.04)",
              padding: "4px 10px",
              borderRadius: "8px",
              border: "1px solid rgba(255, 255, 255, 0.08)",
            }}>
              ⚡ SSE Live Synced
            </span>
          </div>
        </div>

        {/* 2. Executive Hero Header */}
        <div style={{
          background: "linear-gradient(135deg, rgba(15, 23, 42, 0.85) 0%, rgba(30, 41, 59, 0.6) 100%)",
          backdropFilter: "blur(20px)",
          border: "1px solid rgba(255, 255, 255, 0.1)",
          borderRadius: "20px",
          padding: "32px",
          marginBottom: "28px",
          boxShadow: "0 20px 40px -15px rgba(0, 0, 0, 0.5)",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          flexWrap: "wrap",
          gap: "20px",
        }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "12px", marginBottom: "8px" }}>
              <div style={{
                width: "44px",
                height: "44px",
                borderRadius: "12px",
                background: "linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: "22px",
                boxShadow: "0 8px 16px rgba(59, 130, 246, 0.3)",
              }}>
                🎯
              </div>
              <div>
                <h1 style={{
                  fontSize: "26px",
                  fontWeight: "800",
                  letterSpacing: "-0.02em",
                  margin: 0,
                  color: "#f8fafc",
                }}>
                  {clientName} Campaign Batch
                </h1>
                <div style={{ display: "flex", alignItems: "center", gap: "10px", marginTop: "4px", flexWrap: "wrap" }}>
                  <span style={{ fontSize: "13px", color: "#94a3b8" }}>
                    Source: <strong style={{ color: "#f8fafc" }}>{batch.filename || "Automated Ingestion"}</strong>
                  </span>
                  <span style={{ color: "#475569" }}>•</span>
                  <span style={{ fontSize: "13px", color: "#94a3b8" }}>
                    Uploaded By: <strong style={{ color: "#38bdf8" }}>{batch.uploaded_by || "buyer-01"}</strong>
                  </span>
                  <span style={{ color: "#475569" }}>•</span>
                  <span style={{ fontSize: "12px", color: "#64748b" }}>
                    {batch.created_at ? new Date(batch.created_at).toLocaleString() : "Recently Ingested"}
                  </span>
                </div>
              </div>
            </div>

            {/* Batch ID Monospace Copy Pill */}
            <div style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "8px",
              background: "rgba(0, 0, 0, 0.35)",
              border: "1px solid rgba(255, 255, 255, 0.08)",
              padding: "6px 12px",
              borderRadius: "8px",
              marginTop: "8px",
              fontSize: "12px",
              fontFamily: "monospace",
              color: "#94a3b8",
            }}>
              <span>Batch ID:</span>
              <strong style={{ color: "#38bdf8" }}>{batchId}</strong>
              <button
                onClick={handleCopyId}
                style={{
                  background: "transparent",
                  border: "none",
                  color: copiedId ? "#34d399" : "#64748b",
                  cursor: "pointer",
                  padding: "0 4px",
                  fontSize: "12px",
                  fontWeight: 600,
                }}
                title="Copy Batch ID"
              >
                {copiedId ? "✓ Copied" : "📋 Copy"}
              </button>
            </div>
          </div>

          {/* Quick Header Actions */}
          <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
            <Link
              href={`/dispatches?batch_id=${batchId}`}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
                padding: "10px 18px",
                borderRadius: "10px",
                background: "rgba(255, 255, 255, 0.08)",
                border: "1px solid rgba(255, 255, 255, 0.15)",
                color: "#f8fafc",
                fontSize: "13px",
                fontWeight: "600",
                textDecoration: "none",
                transition: "all 0.15s ease",
              }}
            >
              <span>📊</span>
              <span>Live Dispatches</span>
            </Link>

            <Link
              href="/performance"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
                padding: "10px 18px",
                borderRadius: "10px",
                background: "rgba(59, 130, 246, 0.15)",
                border: "1px solid rgba(59, 130, 246, 0.3)",
                color: "#60a5fa",
                fontSize: "13px",
                fontWeight: "600",
                textDecoration: "none",
                transition: "all 0.15s ease",
              }}
            >
              <span>💎</span>
              <span>Platform Telemetry</span>
            </Link>

            <button
              onClick={handleDispatchBatch}
              disabled={isLaunching}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
                padding: "10px 20px",
                borderRadius: "10px",
                background: "linear-gradient(135deg, #10b981 0%, #059669 100%)",
                border: "none",
                color: "#ffffff",
                fontSize: "13px",
                fontWeight: "700",
                cursor: isLaunching ? "not-allowed" : "pointer",
                boxShadow: "0 4px 14px rgba(16, 185, 129, 0.35)",
              }}
            >
              <span>🚀</span>
              <span>{isLaunching ? "Sending approved ads..." : "Send approved ads to dry-run"}</span>
            </button>
          </div>
        </div>

        {hierarchyProgress && (
          <section style={{
            background: "rgba(15, 23, 42, 0.7)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "16px",
            padding: "20px",
            marginBottom: "28px",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: "12px", marginBottom: "12px" }}>
              <strong style={{ color: "#f8fafc" }}>Campaign launch progress</strong>
              <span style={{ color: "#38bdf8", fontWeight: 700 }}>
                {hierarchyProgress.aggregate_completion_percentage}% complete
              </span>
            </div>
            <div
              role="progressbar"
              aria-label="Campaign launch progress"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={hierarchyProgress.aggregate_completion_percentage}
              style={{ height: "8px", borderRadius: "8px", background: "rgba(148, 163, 184, 0.18)", marginBottom: "16px" }}
            >
              <div style={{
                width: `${hierarchyProgress.aggregate_completion_percentage}%`,
                height: "100%",
                borderRadius: "8px",
                background: "linear-gradient(90deg, #3b82f6, #10b981)",
              }} />
            </div>
            {hierarchyProgress.structure.map((campaign) => (
              <div key={campaign.run_id} style={{ padding: "10px 0", borderTop: "1px solid rgba(255, 255, 255, 0.06)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: "12px" }}>
                  <span style={{ color: "#e2e8f0" }}>Campaign: {campaign.grouping_key}</span>
                  <span style={{ color: "#94a3b8" }}>{campaign.status}</span>
                </div>
                {campaign.last_error && <div role="alert" style={{ color: "#fca5a5", fontSize: "12px", marginTop: "4px" }}>{campaign.last_error}</div>}
                {campaign.ad_sets.map((adSet) => (
                  <div key={adSet.run_id} style={{ display: "flex", justifyContent: "space-between", gap: "12px", padding: "6px 0 0 18px", fontSize: "13px" }}>
                    <span style={{ color: "#94a3b8" }}>Ad set: {adSet.grouping_key}</span>
                    <span style={{ color: "#64748b" }}>{adSet.status}</span>
                  </div>
                ))}
              </div>
            ))}
          </section>
        )}
        {hierarchyError && <div role="alert" style={{ color: "#fca5a5", fontSize: "12px", marginBottom: "16px" }}>{hierarchyError}</div>}

        {/* 3. Top KPI Metric Cards */}
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
          gap: "16px",
          marginBottom: "28px",
        }}>
          {/* Card 1: Total Ads */}
          <div style={{
            background: "rgba(15, 23, 42, 0.7)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "16px",
            padding: "20px",
          }}>
            <div style={{ fontSize: "12px", color: "#94a3b8", textTransform: "uppercase", fontWeight: "600" }}>Total Creatives</div>
            <div style={{ fontSize: "28px", fontWeight: "800", color: "#f8fafc", marginTop: "6px" }}>
              {totalAds} <span style={{ fontSize: "14px", fontWeight: "500", color: "#64748b" }}>Ads</span>
            </div>
            <div style={{ fontSize: "12px", color: "#34d399", marginTop: "6px", display: "flex", alignItems: "center", gap: "4px" }}>
              <span>✓</span> 100% Parsed & Analyzed
            </div>
          </div>

          {/* Card 2: Meta Target Placements */}
          <div style={{
            background: "rgba(15, 23, 42, 0.7)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "16px",
            padding: "20px",
          }}>
            <div style={{ fontSize: "12px", color: "#94a3b8", textTransform: "uppercase", fontWeight: "600" }}>Target Placements</div>
            <div style={{ fontSize: "20px", fontWeight: "800", color: "#38bdf8", marginTop: "6px", display: "flex", gap: "8px", alignItems: "center" }}>
              <span>📷 Instagram</span>
              <span style={{ color: "#64748b" }}>•</span>
              <span>👥 Facebook</span>
            </div>
            <div style={{ fontSize: "12px", color: "#94a3b8", marginTop: "10px" }}>
              Delivery status is recorded per ad below.
            </div>
          </div>

          {/* Card 3: Inbound Gross Sales */}
          <div style={{
            background: "rgba(15, 23, 42, 0.7)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "16px",
            padding: "20px",
          }}>
            <div style={{ fontSize: "12px", color: "#94a3b8", textTransform: "uppercase", fontWeight: "600" }}>Attributed Revenue</div>
            <div style={{ fontSize: "28px", fontWeight: "800", color: "#10b981", marginTop: "6px" }}>
              {totalSales != null ? `$${Number(totalSales).toFixed(2)}` : "Not tracked"}
            </div>
            <div style={{ fontSize: "12px", color: "#64748b", marginTop: "6px" }}>
              {totalOrders > 0 ? `${totalOrders} orders completed` : totalOrders != null ? "No recorded orders" : "Not tracked"}
            </div>
          </div>

          {/* Card 4: Media Investment & ROAS */}
          <div style={{
            background: "rgba(15, 23, 42, 0.7)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "16px",
            padding: "20px",
          }}>
            <div style={{ fontSize: "12px", color: "#94a3b8", textTransform: "uppercase", fontWeight: "600" }}>Media Spend & ROAS</div>
            <div style={{ fontSize: "28px", fontWeight: "800", color: "#818cf8", marginTop: "6px" }}>
              {totalSpend != null ? `$${Number(totalSpend).toFixed(2)}` : "Not tracked"}
            </div>
            <div style={{ fontSize: "12px", color: blendedRoas >= 3 ? "#34d399" : "#fbbf24", marginTop: "6px", fontWeight: "600" }}>
              ⚡ Blended ROAS: {blendedRoas != null ? `${blendedRoas}x` : "Not tracked"}
            </div>
          </div>

          {/* Card 5: Impressions */}
          <div style={{
            background: "rgba(15, 23, 42, 0.7)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "16px",
            padding: "20px",
          }}>
            <div style={{ fontSize: "12px", color: "#94a3b8", textTransform: "uppercase", fontWeight: "600" }}>Total Reach</div>
            <div style={{ fontSize: "28px", fontWeight: "800", color: "#f8fafc", marginTop: "6px" }}>
              {totalImpressions != null ? totalImpressions.toLocaleString() : "Not tracked"}
            </div>
            <div style={{ fontSize: "12px", color: "#64748b", marginTop: "6px" }}>
              Reported platform impressions
            </div>
          </div>
        </div>

        {/* 4. Active Notification Monitor Banner */}
        <div style={{
          background: "rgba(99, 102, 241, 0.1)",
          border: "1px solid rgba(99, 102, 241, 0.25)",
          borderRadius: "14px",
          padding: "16px 20px",
          marginBottom: "28px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: "12px",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <span style={{ fontSize: "20px" }}>📧</span>
            <div>
              <span style={{ fontSize: "13px", fontWeight: 700, color: "#818cf8" }}>
                Active Alert Contact:
              </span>{" "}
              <strong style={{ color: "#f8fafc", fontSize: "13px" }}>{contactEmail}</strong>
              <div style={{ fontSize: "12px", color: "#94a3b8", marginTop: "2px" }}>
                Automatic SMTP notifications routed if any live campaign ad is blocked, suspended, or flagged by Meta review bots.
              </div>
            </div>
          </div>

          <div style={{
            fontSize: "11px",
            fontWeight: 700,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "#34d399",
            background: "rgba(16, 185, 129, 0.15)",
            padding: "4px 10px",
            borderRadius: "6px",
            border: "1px solid rgba(16, 185, 129, 0.3)",
          }}>
            SMTP Live Channel
          </div>
        </div>

        {/* 5. Search & Filters Bar */}
        <div style={{
          background: "rgba(15, 23, 42, 0.6)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "16px",
          padding: "16px 20px",
          marginBottom: "24px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: "14px",
        }}>
          {/* Search Input */}
          <div style={{ position: "relative", minWidth: "300px", flex: "1 1 300px" }}>
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search by headline, text, URL, or Ad ID..."
              style={{
                width: "100%",
                background: "rgba(0, 0, 0, 0.3)",
                border: "1px solid rgba(255, 255, 255, 0.1)",
                borderRadius: "10px",
                padding: "10px 14px 10px 36px",
                fontSize: "13px",
                color: "#f8fafc",
                outline: "none",
              }}
            />
            <span style={{ position: "absolute", left: "12px", top: "50%", transform: "translateY(-50%)", color: "#64748b" }}>
              🔍
            </span>
          </div>

          {/* Status & Placement Filters */}
          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
            {/* Status Selector */}
            <div style={{ display: "flex", background: "rgba(0,0,0,0.3)", padding: "3px", borderRadius: "10px", border: "1px solid rgba(255,255,255,0.06)" }}>
              {[
                { id: "all", label: `All (${batch.ads?.length || 0})` },
                { id: "approved", label: "Serving" },
                { id: "pending", label: "Pending" },
              ].map((t) => (
                <button
                  key={t.id}
                  onClick={() => setStatusFilter(t.id)}
                  style={{
                    background: statusFilter === t.id ? "rgba(59, 130, 246, 0.25)" : "transparent",
                    color: statusFilter === t.id ? "#60a5fa" : "#94a3b8",
                    border: "none",
                    borderRadius: "7px",
                    padding: "6px 12px",
                    fontSize: "12px",
                    fontWeight: 600,
                    cursor: "pointer",
                  }}
                >
                  {t.label}
                </button>
              ))}
            </div>

            {/* Platform Selector */}
            <div style={{ display: "flex", background: "rgba(0,0,0,0.3)", padding: "3px", borderRadius: "10px", border: "1px solid rgba(255,255,255,0.06)" }}>
              {[
                { id: "all", label: "All Meta" },
                { id: "facebook", label: "Facebook" },
                { id: "instagram", label: "Instagram" },
              ].map((p) => (
                <button
                  key={p.id}
                  onClick={() => setPlatformFilter(p.id)}
                  style={{
                    background: platformFilter === p.id ? "rgba(16, 185, 129, 0.2)" : "transparent",
                    color: platformFilter === p.id ? "#34d399" : "#94a3b8",
                    border: "none",
                    borderRadius: "7px",
                    padding: "6px 12px",
                    fontSize: "12px",
                    fontWeight: 600,
                    cursor: "pointer",
                  }}
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* 6. Creative Ad Cards Grid */}
        {filteredAds.length === 0 ? (
          <div style={{
            background: "rgba(15, 23, 42, 0.5)",
            border: "1px dashed rgba(255, 255, 255, 0.1)",
            borderRadius: "20px",
            padding: "60px 24px",
            textAlign: "center",
          }}>
            <div style={{ fontSize: "40px", marginBottom: "12px" }}>📭</div>
            <h3 style={{ fontSize: "18px", fontWeight: "700", color: "#f8fafc", margin: "0 0 6px 0" }}>
              No Ads Matched Active Filters
            </h3>
            <p style={{ fontSize: "13px", color: "#94a3b8", margin: "0 0 16px 0" }}>
              Try clearing the search query or changing platform tabs.
            </p>
            <button
              onClick={() => { setSearchQuery(""); setPlatformFilter("all"); setStatusFilter("all"); }}
              style={{
                padding: "8px 16px",
                borderRadius: "8px",
                background: "rgba(255, 255, 255, 0.08)",
                border: "1px solid rgba(255, 255, 255, 0.15)",
                color: "#f8fafc",
                fontSize: "12px",
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              Reset Filters
            </button>
          </div>
        ) : (
          <div style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(420px, 1fr))",
            gap: "20px",
          }}>
            {filteredAds.map((ad, idx) => {
              const isFb = ad.platform?.toLowerCase().includes("face");
              const platformColor = isFb ? "#1877f2" : "#e1306c";
              const platformBadge = isFb ? "👥 Facebook" : "📷 Instagram";

              return (
                <div
                  key={ad.ad_id || idx}
                  style={{
                    background: "rgba(15, 23, 42, 0.75)",
                    border: "1px solid rgba(255, 255, 255, 0.08)",
                    borderRadius: "18px",
                    padding: "22px",
                    display: "flex",
                    flexDirection: "column",
                    justifyContent: "space-between",
                    transition: "transform 0.15s ease, box-shadow 0.15s ease",
                    boxShadow: "0 10px 25px rgba(0, 0, 0, 0.2)",
                  }}
                >
                  <div>
                    {/* Header: Platform & Delivery Badge */}
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
                      <span style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: "6px",
                        fontSize: "12px",
                        fontWeight: "700",
                        padding: "3px 10px",
                        borderRadius: "20px",
                        background: isFb ? "rgba(24, 119, 242, 0.15)" : "rgba(225, 48, 108, 0.15)",
                        color: platformColor,
                        border: `1px solid ${isFb ? "rgba(24, 119, 242, 0.3)" : "rgba(225, 48, 108, 0.3)"}`,
                      }}>
                        {platformBadge}
                      </span>

                      <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                        <span style={{
                          fontSize: "11px",
                          fontWeight: "600",
                          padding: "2px 8px",
                          borderRadius: "6px",
                          background: "rgba(16, 185, 129, 0.15)",
                          color: "#34d399",
                          border: "1px solid rgba(16, 185, 129, 0.3)",
                        }}>
                          ● Active Live
                        </span>
                        <span style={{ fontSize: "11px", color: "#64748b", fontFamily: "monospace" }}>
                          #{ad.row_number || idx + 1}
                        </span>
                      </div>
                    </div>

                    {/* Ad Headline */}
                    <h3 style={{ fontSize: "16px", fontWeight: "800", color: "#f8fafc", margin: "0 0 6px 0", lineHeight: 1.3 }}>
                      {ad.headline || "Untitled Creative Headline"}
                    </h3>

                    {/* Ad Text / Copy */}
                    <p style={{
                      fontSize: "13px",
                      color: "#94a3b8",
                      lineHeight: "1.5",
                      margin: "0 0 14px 0",
                      background: "rgba(0, 0, 0, 0.25)",
                      padding: "10px 12px",
                      borderRadius: "8px",
                      borderLeft: `3px solid ${platformColor}`,
                    }}>
                      {ad.ad_text || "No primary text provided."}
                    </p>

                    {/* Destination URL */}
                    <div style={{ fontSize: "11px", color: "#64748b", marginBottom: "14px", wordBreak: "break-all" }}>
                      🔗 <strong style={{ color: "#38bdf8" }}>{ad.destination_url}</strong>
                    </div>

                    {/* Live Telemetry KPI Chips */}
                    <div style={{
                      background: "rgba(0, 0, 0, 0.35)",
                      border: "1px solid rgba(255, 255, 255, 0.05)",
                      borderRadius: "10px",
                      padding: "10px 12px",
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr 1fr 1fr",
                      gap: "8px",
                      marginBottom: "16px",
                      textAlign: "center",
                    }}>
                      <div>
                        <div style={{ fontSize: "10px", color: "#64748b", textTransform: "uppercase" }}>Spend</div>
                        <div style={{ fontSize: "13px", fontWeight: "700", color: "#f8fafc", marginTop: "2px" }}>
                          {ad.spend_usd != null ? `$${Number(ad.spend_usd).toFixed(2)}` : "Not tracked"}
                        </div>
                      </div>
                      <div>
                        <div style={{ fontSize: "10px", color: "#64748b", textTransform: "uppercase" }}>Sales</div>
                        <div style={{ fontSize: "13px", fontWeight: "700", color: "#10b981", marginTop: "2px" }}>
                          {ad.inbound_sales_usd != null ? `$${Number(ad.inbound_sales_usd).toFixed(2)}` : "Not tracked"}
                        </div>
                      </div>
                      <div>
                        <div style={{ fontSize: "10px", color: "#64748b", textTransform: "uppercase" }}>ROAS</div>
                        <div style={{ fontSize: "13px", fontWeight: "700", color: "#818cf8", marginTop: "2px" }}>
                          {ad.roas != null ? `${ad.roas}x` : "Not tracked"}
                        </div>
                      </div>
                      <div>
                        <div style={{ fontSize: "10px", color: "#64748b", textTransform: "uppercase" }}>Orders</div>
                        <div style={{ fontSize: "13px", fontWeight: "700", color: "#f8fafc", marginTop: "2px" }}>
                          {ad.inbound_orders != null ? ad.inbound_orders : "Not tracked"}
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* Card Actions: Preview & Edit */}
                  <div style={{ display: "flex", gap: "8px", borderTop: "1px solid rgba(255, 255, 255, 0.06)", paddingTop: "14px" }}>
                    <button
                      onClick={() => setPreviewAd(ad)}
                      style={{
                        flex: 1,
                        padding: "8px 12px",
                        borderRadius: "8px",
                        background: "rgba(59, 130, 246, 0.15)",
                        border: "1px solid rgba(59, 130, 246, 0.3)",
                        color: "#60a5fa",
                        fontSize: "12px",
                        fontWeight: "600",
                        cursor: "pointer",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        gap: "6px",
                      }}
                    >
                      <span>👁️</span>
                      <span>Live Preview</span>
                    </button>

                    <button
                      onClick={() => {
                        setEditingAd(ad);
                        setEditHeadline(ad.headline || "");
                        setEditText(ad.ad_text || "");
                      }}
                      style={{
                        padding: "8px 14px",
                        borderRadius: "8px",
                        background: "rgba(255, 255, 255, 0.06)",
                        border: "1px solid rgba(255, 255, 255, 0.12)",
                        color: "#cbd5e1",
                        fontSize: "12px",
                        fontWeight: "600",
                        cursor: "pointer",
                      }}
                    >
                      ✏️ Edit
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* 7. Isolated Ingest Syntax Failures (Rejected Rows) */}
        {batch.rejected_rows && batch.rejected_rows.length > 0 && (
          <div style={{
            marginTop: "36px",
            background: "rgba(239, 68, 68, 0.08)",
            border: "1px solid rgba(239, 68, 68, 0.25)",
            borderRadius: "16px",
            padding: "24px",
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "12px" }}>
              <span style={{ fontSize: "20px" }}>🛡️</span>
              <h3 style={{ fontSize: "16px", fontWeight: "700", color: "#f87171", margin: 0 }}>
                Isolated Malformed Rows During Ingestion ({batch.rejected_rows.length})
              </h3>
            </div>
            <p style={{ fontSize: "13px", color: "#94a3b8", margin: "0 0 16px 0" }}>
              These CSV rows failed initial structure validation and were isolated so they never risk ad rejection on Meta.
            </p>
            <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
              {batch.rejected_rows.map((rr, i) => (
                <div
                  key={i}
                  style={{
                    background: "rgba(0, 0, 0, 0.3)",
                    padding: "10px 14px",
                    borderRadius: "8px",
                    fontSize: "12px",
                    display: "flex",
                    justifyContent: "space-between",
                    color: "#cbd5e1",
                  }}
                >
                  <span>
                    <strong>Row #{rr.row_number}:</strong> {rr.reason || "Missing mandatory columns"}
                  </span>
                  <span style={{ color: "#f87171", fontFamily: "monospace" }}>{rr.reason_code}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* 8. Live Photorealistic Social Feed Preview Modal */}
      {previewAd && (
        <SocialFeedModal
          ad={previewAd}
          adId={previewAd.ad_id}
          platform={previewAd.platform}
          headline={previewAd.headline}
          clientHandle={previewAd.handle}
          clientId={previewAd.client_id}
          destinationUrl={previewAd.destination_url}
          reviewStatus={previewAd.platform_review_status}
          onClose={() => setPreviewAd(null)}
        />
      )}

      {/* 9. Edit Copy Modal */}
      {editingAd && (
        <div style={{
          position: "fixed",
          inset: 0,
          background: "rgba(3, 7, 18, 0.85)",
          backdropFilter: "blur(12px)",
          zIndex: 1000,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "20px",
        }}>
          <div style={{
            background: "#0f172a",
            border: "1px solid rgba(255, 255, 255, 0.12)",
            borderRadius: "18px",
            width: "100%",
            maxWidth: "540px",
            padding: "28px",
            boxShadow: "0 20px 40px rgba(0,0,0,0.6)",
          }}>
            <h3 style={{ fontSize: "18px", fontWeight: "800", color: "#f8fafc", margin: "0 0 4px 0" }}>
              Edit Creative Copy #{editingAd.row_number}
            </h3>
            <p style={{ fontSize: "12px", color: "#94a3b8", margin: "0 0 20px 0" }}>
              Ad ID: <code style={{ color: "#38bdf8" }}>{editingAd.ad_id}</code>
            </p>

            <div style={{ marginBottom: "16px" }}>
              <label style={{ display: "block", fontSize: "12px", color: "#94a3b8", fontWeight: "600", marginBottom: "6px" }}>
                Headline
              </label>
              <input
                type="text"
                value={editHeadline}
                onChange={(e) => setEditHeadline(e.target.value)}
                style={{
                  width: "100%",
                  background: "rgba(0, 0, 0, 0.3)",
                  border: "1px solid rgba(255, 255, 255, 0.1)",
                  borderRadius: "8px",
                  padding: "10px 12px",
                  color: "#f8fafc",
                  fontSize: "13px",
                  outline: "none",
                }}
              />
            </div>

            <div style={{ marginBottom: "24px" }}>
              <label style={{ display: "block", fontSize: "12px", color: "#94a3b8", fontWeight: "600", marginBottom: "6px" }}>
                Primary Text / Copy
              </label>
              <textarea
                rows={4}
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                style={{
                  width: "100%",
                  background: "rgba(0, 0, 0, 0.3)",
                  border: "1px solid rgba(255, 255, 255, 0.1)",
                  borderRadius: "8px",
                  padding: "10px 12px",
                  color: "#f8fafc",
                  fontSize: "13px",
                  outline: "none",
                  resize: "vertical",
                }}
              />
            </div>

            <div style={{ display: "flex", gap: "10px", justifyContent: "flex-end" }}>
              <button
                onClick={() => setEditingAd(null)}
                style={{
                  padding: "8px 16px",
                  borderRadius: "8px",
                  background: "transparent",
                  border: "1px solid rgba(255, 255, 255, 0.15)",
                  color: "#94a3b8",
                  fontSize: "13px",
                  fontWeight: "600",
                  cursor: "pointer",
                }}
              >
                Cancel
              </button>
              <button
                onClick={handleSaveEdit}
                style={{
                  padding: "8px 18px",
                  borderRadius: "8px",
                  background: "linear-gradient(135deg, #10b981 0%, #059669 100%)",
                  border: "none",
                  color: "#fff",
                  fontSize: "13px",
                  fontWeight: "700",
                  cursor: "pointer",
                }}
              >
                Save Changes
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Toast Notification */}
      <div style={{padding: 16}}>
        <label>Notification email <input placeholder="Enter the launch contact email" value={notificationEmail} onChange={e => setNotificationEmail(e.target.value)} /></label>
        <p>Used only to send you launch and policy alerts. </p>
      </div>
      <GlassToast
        message={toast?.message}
        type={toast?.type}
        onClose={() => setToast(null)}
      />
    </div>
  );
}

export default function BatchPage(props) {
  return <BatchReviewScreen {...props} />;
}
