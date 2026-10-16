"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import GlassToast from "../../components/GlassToast";

export default function DeadLetterPage({ apiBaseUrl = "http://localhost:8000" }) {
  const [ads, setAds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [actionMsg, setActionMsg] = useState("");
  const [toast, setToast] = useState(null);

  const fetchDeadLetterAds = () => {
    setLoading(true);
    const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
    const headers = { Accept: "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;

    fetch(`${apiBaseUrl}/dead-letter`, { headers })
      .then(async (res) => {
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        return res.json();
      })
      .then((data) => {
        setAds(data.dead_letter_ads || []);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  };

  useEffect(() => {
    fetchDeadLetterAds();
  }, [apiBaseUrl]);

  const handleReturnToReview = async (adId) => {
    const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json",
    };
    if (token) headers["Authorization"] = `Bearer ${token}`;

    try {
      const res = await fetch(`${apiBaseUrl}/ads/${adId}/return-to-review`, {
        method: "POST",
        headers,
        body: JSON.stringify({ reviewer_note: "Returned to review cockpit for copy/URL correction." }),
      });
      if (res.ok) {
        setActionMsg(`Ad ${adId} returned to review!`);
        setToast({ message: `Ad ${adId} returned to review cockpit.`, type: "success" });
        fetchDeadLetterAds();
      } else {
        setToast({ message: "Failed to return ad to review. Check permissions.", type: "error" });
      }
    } catch {
      setToast({ message: "Network error contacting API to return ad to review.", type: "error" });
    }
  };

  const handleRetryDeadLetter = async (adId) => {
    const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json",
    };
    if (token) headers["Authorization"] = `Bearer ${token}`;

    try {
      const res = await fetch(`${apiBaseUrl}/ads/${adId}/retry`, {
        method: "POST",
        headers,
      });
      if (res.ok) {
        setActionMsg(`Dispatch retry initiated for ad ${adId}!`);
        setToast({ message: `Dispatch retry initiated for ad ${adId}.`, type: "success" });
        fetchDeadLetterAds();
      } else {
        setToast({ message: "Failed to retry ad dispatch.", type: "error" });
      }
    } catch {
      setToast({ message: "Network error contacting API to retry dispatch.", type: "error" });
    }
  };

  return (
    <div style={{ maxWidth: "1400px", margin: "0 auto", padding: "32px 24px" }}>
      
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "28px", flexWrap: "wrap", gap: "16px" }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "11px", color: "#64748b", textTransform: "uppercase", fontWeight: "700", letterSpacing: "0.08em", marginBottom: "4px" }}>
            <span>Operations Center</span>
            <span>/</span>
            <span style={{ color: "#fbbf24" }}>Dead-Letter Queue</span>
          </div>
          <h1 style={{
            margin: 0,
            fontSize: "28px",
            fontWeight: "900",
            letterSpacing: "-0.02em",
            background: "linear-gradient(135deg, #ffffff 0%, #cbd5e1 100%)",
            WebkitBackgroundClip: "text",
            WebkitTextFillColor: "transparent",
          }}>
            Dead-Letter Queue & Quarantine
          </h1>
          <p style={{ margin: "6px 0 0 0", color: "#94a3b8", fontSize: "14px" }}>
            Quarantine area for ads that failed upstream platform submission after max retries or encountered non-retryable 4xx errors.
          </p>
        </div>

        <button
          type="button"
          onClick={fetchDeadLetterAds}
          style={{
            background: "rgba(255, 255, 255, 0.08)",
            border: "1px solid rgba(255, 255, 255, 0.15)",
            color: "#f8fafc",
            borderRadius: "8px",
            padding: "8px 16px",
            fontSize: "13px",
            fontWeight: "600",
            cursor: "pointer",
          }}
        >
          ↻ Refresh DLQ
        </button>
      </div>

      {actionMsg && (
        <div style={{ marginBottom: "20px", padding: "12px 18px", background: "rgba(16, 185, 129, 0.12)", border: "1px solid rgba(16, 185, 129, 0.3)", color: "#34d399", borderRadius: "10px", fontSize: "13px" }}>
          ✓ {actionMsg}
        </div>
      )}

      {loading ? (
        <div style={{ padding: "40px", textAlign: "center", color: "#94a3b8" }}>Scanning dead-letter quarantine...</div>
      ) : error ? (
        <div style={{ padding: "20px", background: "rgba(239, 68, 68, 0.12)", border: "1px solid rgba(239, 68, 68, 0.3)", color: "#f87171", borderRadius: "10px" }}>
          Failed to load dead letter ads: {error}
        </div>
      ) : ads.length === 0 ? (
        <div style={{
          background: "rgba(15, 23, 42, 0.65)",
          backdropFilter: "blur(20px)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "14px",
          padding: "50px 20px",
          textAlign: "center",
        }}>
          <div style={{ fontSize: "28px", marginBottom: "8px", color: "#34d399" }}>✓</div>
          <h3 style={{ margin: "0 0 4px 0", color: "#f8fafc", fontSize: "18px" }}>Dead-Letter Queue is Clear</h3>
          <p style={{ color: "#94a3b8", margin: 0, fontSize: "13px" }}>
            Zero ads are quarantined. All approved campaigns have dispatched or completed clean delivery.
          </p>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          {ads.map((ad) => (
            <div
              key={ad.ad_id}
              style={{
                background: "rgba(15, 23, 42, 0.65)",
                backdropFilter: "blur(20px)",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderLeft: "4px solid #f59e0b",
                borderRadius: "12px",
                padding: "20px",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "flex-start",
                gap: "20px",
                boxShadow: "0 10px 25px -5px rgba(0, 0, 0, 0.5)",
              }}
            >
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "8px", flexWrap: "wrap" }}>
                  <span style={{
                    fontSize: "10px",
                    fontWeight: "800",
                    textTransform: "uppercase",
                    padding: "3px 8px",
                    borderRadius: "4px",
                    background: "rgba(245, 158, 11, 0.15)",
                    color: "#fbbf24",
                    border: "1px solid rgba(245, 158, 11, 0.3)",
                  }}>
                    DEAD-LETTER
                  </span>
                  <span style={{ fontSize: "12px", color: "#94a3b8" }}>
                    Platform: <strong style={{ color: "#cbd5e1" }}>{ad.platform}</strong> | Row #{ad.row_number}
                  </span>
                  <span style={{ fontSize: "12px", color: "#64748b" }}>
                    Ad ID: {ad.ad_id}
                  </span>
                </div>

                <div style={{ fontSize: "15px", fontWeight: "700", color: "#f8fafc", marginBottom: "4px" }}>
                  "{ad.headline || "Untitled Headline"}"
                </div>

                <div style={{ fontSize: "13px", color: "#cbd5e1", marginBottom: "8px" }}>
                  {ad.ad_text}
                </div>

                <div style={{
                  fontSize: "12px",
                  color: "#fca5a5",
                  background: "rgba(239, 68, 68, 0.1)",
                  border: "1px solid rgba(239, 68, 68, 0.2)",
                  padding: "8px 12px",
                  borderRadius: "6px",
                  display: "inline-block",
                }}>
                  <strong>Failure Reason:</strong> {ad.dead_letter_reason || "Upstream destination 500 error / Max retries exhausted"}
                </div>
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                <button
                  type="button"
                  onClick={() => handleReturnToReview(ad.ad_id)}
                  style={{
                    background: "rgba(255, 255, 255, 0.08)",
                    border: "1px solid rgba(255, 255, 255, 0.15)",
                    color: "#f8fafc",
                    padding: "7px 14px",
                    borderRadius: "6px",
                    fontSize: "12px",
                    fontWeight: "600",
                    cursor: "pointer",
                    whiteSpace: "nowrap",
                  }}
                >
                  Return to Review Cockpit ➔
                </button>
                <button
                  type="button"
                  onClick={() => handleRetryDeadLetter(ad.ad_id)}
                  style={{
                    background: "linear-gradient(135deg, #f59e0b 0%, #d97706 100%)",
                    border: "none",
                    color: "#ffffff",
                    padding: "7px 14px",
                    borderRadius: "6px",
                    fontSize: "12px",
                    fontWeight: "700",
                    cursor: "pointer",
                    whiteSpace: "nowrap",
                  }}
                >
                  Force Immediate Retry
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      <GlassToast toast={toast} onClose={() => setToast(null)} />
    </div>
  );
}
