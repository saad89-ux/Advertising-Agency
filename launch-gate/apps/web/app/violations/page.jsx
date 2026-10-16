"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

export default function ViolationsPage({ apiBaseUrl = "http://localhost:8000" }) {
  const router = useRouter();
  const [violations, setViolations] = useState([]);
  const [loading, setLoading] = useState(true);

  const fetchViolations = async () => {
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await fetch(`${apiBaseUrl}/campaign-flow/violations`, { headers });
      if (res.ok) {
        const data = await res.json();
        setViolations(data);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchViolations();
  }, [apiBaseUrl]);

  return (
    <div style={{
      minHeight: "calc(100vh - 65px)",
      background: "#080c14",
      color: "#f8fafc",
      padding: "36px 48px",
      fontFamily: "system-ui, -apple-system, sans-serif",
    }}>
      <div style={{ maxWidth: "1280px", margin: "0 auto" }}>
        {/* Header */}
        <div style={{ marginBottom: "28px", display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "8px" }}>
              <span style={{ fontSize: "24px" }}>⚠️</span>
              <h1 style={{
                fontSize: "28px",
                fontWeight: "800",
                letterSpacing: "-0.02em",
                margin: 0,
                background: "linear-gradient(180deg, #ffffff 0%, #94a3b8 100%)",
                WebkitBackgroundClip: "text",
                WebkitTextFillColor: "transparent",
              }}>
                Platform Policy Violations & Bot Review Feedback
              </h1>
            </div>
            <p style={{ fontSize: "14px", color: "#94a3b8", margin: 0 }}>
              Live algorithmic review monitoring: track why platform bots (Meta, TikTok, X, Snapchat) rejected or flagged ad campaigns.
            </p>
          </div>

          <Link
            href="/remediation"
            style={{
              padding: "10px 20px",
              background: "linear-gradient(135deg, #10b981 0%, #059669 100%)",
              borderRadius: "10px",
              color: "#ffffff",
              fontSize: "13px",
              fontWeight: "700",
              textDecoration: "none",
              display: "flex",
              alignItems: "center",
              gap: "8px",
              boxShadow: "0 4px 14px rgba(16, 185, 129, 0.3)",
            }}
          >
            🛠️ Go to HITL Remediation Engine
          </Link>
        </div>

        {/* Violations Stream */}
        <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
          {violations.map((v) => (
            <div
              key={v.violation_id}
              style={{
                background: "rgba(15, 23, 42, 0.75)",
                backdropFilter: "blur(20px)",
                border: "1px solid rgba(239, 68, 68, 0.25)",
                borderRadius: "16px",
                padding: "24px",
                boxShadow: "0 10px 30px rgba(0, 0, 0, 0.3)",
              }}
            >
              {/* Header Bar */}
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px", borderBottom: "1px solid rgba(255, 255, 255, 0.06)", paddingBottom: "14px" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
                  <span style={{
                    padding: "4px 10px",
                    borderRadius: "6px",
                    fontSize: "11px",
                    fontWeight: "700",
                    background: v.severity === "CRITICAL_BLOCKER" ? "rgba(239, 68, 68, 0.2)" : "rgba(245, 158, 11, 0.2)",
                    color: v.severity === "CRITICAL_BLOCKER" ? "#f87171" : "#fbbf24",
                  }}>
                    {v.severity.replace("_", " ")}
                  </span>

                  <span style={{ fontSize: "16px", fontWeight: "700", color: "#ffffff" }}>
                    {v.client_id.toUpperCase()} • {v.platform.toUpperCase()}
                  </span>

                  <span style={{ fontSize: "12px", color: "#94a3b8" }}>
                    Handle: <strong style={{ color: "#cbd5e1" }}>{v.handle}</strong> • Ad ID: <code>{v.ad_id}</code>
                  </span>
                </div>

                <span style={{ fontSize: "12px", color: "#64748b" }}>
                  Flagged {v.timestamp}
                </span>
              </div>

              {/* Grid: Problem Copy & Crawler Feedback */}
              <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr", gap: "20px", marginBottom: "18px" }}>
                {/* Left: Flagged Ad Content with Highlighted Tokens */}
                <div style={{ background: "rgba(8, 12, 20, 0.6)", padding: "16px", borderRadius: "12px", border: "1px solid rgba(255, 255, 255, 0.06)" }}>
                  <div style={{ fontSize: "11px", color: "#64748b", fontWeight: "600", textTransform: "uppercase", marginBottom: "6px" }}>
                    Original Copy with Flagged Tokens
                  </div>
                  <div style={{ fontSize: "15px", fontWeight: "700", color: "#f8fafc", marginBottom: "8px" }}>
                    "{v.headline}"
                  </div>
                  <div style={{ fontSize: "13px", color: "#cbd5e1", lineHeight: "1.5" }}>
                    {v.ad_text}
                  </div>

                  <div style={{ marginTop: "12px", display: "flex", alignItems: "center", gap: "6px" }}>
                    <span style={{ fontSize: "11px", color: "#f87171", fontWeight: "600" }}>Violating Tokens:</span>
                    {v.flagged_tokens.map((tok, i) => (
                      <span key={i} style={{ padding: "2px 8px", background: "rgba(239, 68, 68, 0.2)", color: "#fca5a5", borderRadius: "4px", fontSize: "11px", fontWeight: "700" }}>
                        {tok}
                      </span>
                    ))}
                  </div>
                </div>

                {/* Right: Platform Feedback & Cited Standard */}
                <div style={{ background: "rgba(8, 12, 20, 0.6)", padding: "16px", borderRadius: "12px", border: "1px solid rgba(255, 255, 255, 0.06)" }}>
                  <div style={{ fontSize: "11px", color: "#64748b", fontWeight: "600", textTransform: "uppercase", marginBottom: "6px" }}>
                    Platform Policy Citation & Crawler Notes
                  </div>
                  <div style={{ fontSize: "13px", fontWeight: "700", color: "#fbbf24", marginBottom: "6px" }}>
                    📜 {v.platform_rule_code}: {v.platform_rule_name}
                  </div>
                  <div style={{ fontSize: "12px", color: "#94a3b8", lineHeight: "1.5", marginBottom: "10px" }}>
                    {v.crawler_feedback}
                  </div>
                  <div style={{ fontSize: "11px", color: "#64748b" }}>
                    <strong>LLM Root Cause:</strong> {v.llm_root_cause}
                  </div>
                </div>
              </div>

              {/* Proposed AI Fix & Remediation Trigger */}
              <div style={{
                background: "rgba(16, 185, 129, 0.08)",
                border: "1px solid rgba(16, 185, 129, 0.25)",
                borderRadius: "12px",
                padding: "16px",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}>
                <div>
                  <div style={{ fontSize: "11px", color: "#34d399", fontWeight: "700", textTransform: "uppercase", marginBottom: "4px" }}>
                    ✓ Proposed AI Auto-Remediation (100% Policy Compliant)
                  </div>
                  <div style={{ fontSize: "13px", fontWeight: "600", color: "#f8fafc" }}>
                    Headline: "{v.proposed_ai_fix.revised_headline}"
                  </div>
                  <div style={{ fontSize: "12px", color: "#cbd5e1" }}>
                    Copy: {v.proposed_ai_fix.revised_text}
                  </div>
                </div>

                <button
                  onClick={() => router.push(`/remediation?ad_id=${v.ad_id}`)}
                  style={{
                    padding: "10px 20px",
                    background: "#10b981",
                    border: "none",
                    borderRadius: "8px",
                    color: "#ffffff",
                    fontSize: "13px",
                    fontWeight: "700",
                    cursor: "pointer",
                    whiteSpace: "nowrap",
                  }}
                >
                  Inspect & Auto-Fix ➔
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
