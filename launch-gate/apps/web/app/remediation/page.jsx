"use client";
import { scopedFetch as fetch } from "../../lib/campaign-scope";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import GlassToast from "../../components/GlassToast";
import { readError, notificationLabel } from "../../lib/api";

export default function RemediationPage({ apiBaseUrl = "http://localhost:8000" }) {
  const [violations, setViolations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [remediatingId, setRemediatingId] = useState(null);
  const [successNotif, setSuccessNotif] = useState(null);
  const [toast, setToast] = useState(null);

  // Form edit states
  const [editFields, setEditFields] = useState({});
  const [launcherEmail, setLauncherEmail] = useState("");

  const fetchIssues = async () => {
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await fetch(`${apiBaseUrl}/campaign-flow/violations`, { headers });
      if (!res.ok) throw new Error(await readError(res));
      if (res.ok) {
        const data = await res.json();
        setViolations(data);

        // Show the recorded suggestions for human editing.
        const initialEdits = {};
        data.forEach((v) => {
          initialEdits[v.ad_id] = {
            headline: v.proposed_ai_fix.revised_headline,
            text: v.proposed_ai_fix.revised_text,
          };
        });
        setEditFields(initialEdits);
      }
    } catch (err) {
      console.error(err);
      setToast({message: err.message, type: "error"});
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchIssues();
  }, [apiBaseUrl]);

  const handleApplyHitlRemediation = async (adId) => {
    setRemediatingId(adId);
    setSuccessNotif(null);

    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      };

      const payload = {
        ad_id: adId,
        revised_headline: editFields[adId]?.headline,
        revised_text: editFields[adId]?.text,
        confirm_employee_notification: true,
        notification_email: launcherEmail || violations.find(v => v.ad_id === adId)?.notification_contact?.employee_email || undefined,
      };

      const res = await fetch(`${apiBaseUrl}/campaign-flow/hitl-remediate`, {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        const data = await res.json();
        setSuccessNotif({
          ad_id: adId,
          message: data.message,
          notif: data.notification_dispatched,
        });

        // Remove from list
        setViolations((prev) => prev.filter((v) => v.ad_id !== adId));
        setToast({
          message: data.message || `Ad ${adId} saved for checks and human approval.`,
          type: data.notification_result?.email_status === "sent" ? "success" : "warning",
        });
      } else {
        setToast({ message: await readError(res), type: "error" });
      }
    } catch (err) {
      console.error(err);
      setToast({message: err.message, type: "error"});
      setToast({ message: "Network error occurred while applying remediation.", type: "error" });
    } finally {
      setRemediatingId(null);
    }
  };

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
              <span style={{ fontSize: "24px" }}>🛠️</span>
              <h1 style={{
                fontSize: "28px",
                fontWeight: "800",
                letterSpacing: "-0.02em",
                margin: 0,
                background: "linear-gradient(180deg, #ffffff 0%, #94a3b8 100%)",
                WebkitBackgroundClip: "text",
                WebkitTextFillColor: "transparent",
              }}>
                Issue Inspection, HITL Notification & AI Auto-Fix
              </h1>
            </div>
            <p style={{ fontSize: "14px", color: "#94a3b8", margin: 0 }}>
              Inspect flagged issues, notify designated employees by SMTP email, and execute human-in-the-loop remediation.
            </p>
          </div>

          <div style={{
            background: "rgba(99, 102, 241, 0.12)",
            border: "1px solid rgba(99, 102, 241, 0.3)",
            padding: "8px 16px",
            borderRadius: "12px",
            fontSize: "12px",
            color: "#818cf8",
            display: "flex",
            alignItems: "center",
            gap: "8px",
          }}>
            <span>📧</span>
            <span>
              Notification contact: <input type="email" placeholder="Enter the launch contact email" value={launcherEmail} onChange={e => setLauncherEmail(e.target.value)} />
            </span>
          </div>
        </div>

        {/* Success Alert Banner */}
        {successNotif && (
          <div style={{
            background: "rgba(16, 185, 129, 0.12)",
            border: "1px solid rgba(16, 185, 129, 0.4)",
            borderRadius: "14px",
            padding: "18px 24px",
            marginBottom: "24px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}>
            <div>
              <div style={{ fontSize: "14px", fontWeight: "700", color: "#6ee7b7", marginBottom: "4px" }}>
                ✓ {successNotif.message}
              </div>
              <div style={{ fontSize: "12px", color: "#94a3b8" }}>
                {notificationLabel(successNotif.notif.dispatch_result)} (phone retained in the campaign contact record).
              </div>
            </div>
            <Link
              href="/dispatches"
              style={{
                padding: "8px 16px",
                background: "#10b981",
                borderRadius: "8px",
                color: "#ffffff",
                fontSize: "12px",
                fontWeight: "700",
                textDecoration: "none",
              }}
            >
              View Dispatch Records ➔
            </Link>
          </div>
        )}

        {/* Issues List */}
        {violations.length === 0 ? (
          <div style={{
            background: "rgba(15, 23, 42, 0.6)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "16px",
            padding: "60px 20px",
            textAlign: "center",
          }}>
            <div style={{ fontSize: "40px", marginBottom: "12px" }}>🎉</div>
            <h3 style={{ fontSize: "18px", fontWeight: "700", color: "#f8fafc", margin: "0 0 6px 0" }}>
              Zero Active Issues Pending Remediation
            </h3>
            <p style={{ fontSize: "14px", color: "#94a3b8", margin: 0 }}>
              All approved Meta campaigns are monitored for policy compliance and live delivery status.
            </p>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "24px" }}>
            {violations.map((v) => (
              <div
                key={v.ad_id}
                style={{
                  background: "rgba(15, 23, 42, 0.8)",
                  backdropFilter: "blur(24px)",
                  border: "1px solid rgba(255, 255, 255, 0.1)",
                  borderRadius: "18px",
                  padding: "26px",
                  boxShadow: "0 15px 35px rgba(0, 0, 0, 0.4)",
                }}
              >
                {/* Top Bar */}
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "18px", borderBottom: "1px solid rgba(255, 255, 255, 0.06)", paddingBottom: "14px" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                    <span style={{ fontSize: "16px", fontWeight: "700", color: "#f8fafc" }}>
                      {v.client_id.toUpperCase()} Campaign Issue
                    </span>
                    <span style={{ fontSize: "12px", padding: "2px 8px", background: "rgba(239, 68, 68, 0.2)", color: "#f87171", borderRadius: "6px", fontWeight: "700" }}>
                      {v.platform_rule_code}
                    </span>
                  </div>

                  <span style={{ fontSize: "12px", color: "#94a3b8" }}>
                    Target: <strong style={{ color: "#e2e8f0" }}>{v.platform.toUpperCase()} ({v.handle})</strong> • Ad ID: <code>{v.ad_id}</code>
                  </span>
                </div>

                {/* Left/Right Inspection Columns */}
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "24px", marginBottom: "22px" }}>
                  {/* Problem Inspection */}
                  <div style={{ background: "rgba(8, 12, 20, 0.6)", padding: "18px", borderRadius: "12px", border: "1px solid rgba(239, 68, 68, 0.2)" }}>
                    <div style={{ fontSize: "12px", fontWeight: "700", color: "#f87171", textTransform: "uppercase", marginBottom: "8px" }}>
                      Flagged Violation Details
                    </div>
                    <div style={{ fontSize: "14px", fontWeight: "700", color: "#ffffff", marginBottom: "6px" }}>
                      "{v.headline}"
                    </div>
                    <div style={{ fontSize: "13px", color: "#94a3b8", lineHeight: "1.5", marginBottom: "12px" }}>
                      {v.ad_text}
                    </div>
                    <div style={{ fontSize: "12px", color: "#cbd5e1", background: "rgba(0, 0, 0, 0.3)", padding: "10px", borderRadius: "8px" }}>
                      <strong>LLM Diagnosis:</strong> {v.llm_root_cause}
                    </div>
                  </div>

                  {/* Editable AI Proposed Compliant Fix */}
                  <div style={{ background: "rgba(8, 12, 20, 0.6)", padding: "18px", borderRadius: "12px", border: "1px solid rgba(16, 185, 129, 0.25)" }}>
                    <div style={{ fontSize: "12px", fontWeight: "700", color: "#34d399", textTransform: "uppercase", marginBottom: "8px" }}>
                      AI Proposed Compliant Revision (Editable)
                    </div>

                    <div style={{ marginBottom: "10px" }}>
                      <label style={{ fontSize: "11px", color: "#94a3b8", display: "block", marginBottom: "4px" }}>Revised Headline:</label>
                      <input
                        type="text"
                        value={editFields[v.ad_id]?.headline || ""}
                        onChange={(e) => setEditFields({
                          ...editFields,
                          [v.ad_id]: { ...editFields[v.ad_id], headline: e.target.value },
                        })}
                        style={{
                          width: "100%",
                          padding: "8px 12px",
                          background: "rgba(15, 23, 42, 0.8)",
                          border: "1px solid rgba(255, 255, 255, 0.15)",
                          borderRadius: "8px",
                          color: "#ffffff",
                          fontSize: "13px",
                          fontWeight: "600",
                          boxSizing: "border-box",
                        }}
                      />
                    </div>

                    <div>
                      <label style={{ fontSize: "11px", color: "#94a3b8", display: "block", marginBottom: "4px" }}>Revised Ad Copy:</label>
                      <textarea
                        rows={3}
                        value={editFields[v.ad_id]?.text || ""}
                        onChange={(e) => setEditFields({
                          ...editFields,
                          [v.ad_id]: { ...editFields[v.ad_id], text: e.target.value },
                        })}
                        style={{
                          width: "100%",
                          padding: "8px 12px",
                          background: "rgba(15, 23, 42, 0.8)",
                          border: "1px solid rgba(255, 255, 255, 0.15)",
                          borderRadius: "8px",
                          color: "#cbd5e1",
                          fontSize: "13px",
                          lineHeight: "1.5",
                          boxSizing: "border-box",
                          resize: "vertical",
                        }}
                      />
                    </div>
                  </div>
                </div>

                {/* HITL Action Bar */}
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", borderTop: "1px solid rgba(255, 255, 255, 0.06)", paddingTop: "16px" }}>
                  <div style={{ fontSize: "12px", color: "#94a3b8" }}>
                    Notice: Approving will immediately notify <strong>{employeeContact.email}</strong> and re-dispatch via MCP server.
                  </div>

                  <button
                    onClick={() => handleApplyHitlRemediation(v.ad_id)}
                    disabled={remediatingId === v.ad_id}
                    style={{
                      padding: "11px 26px",
                      background: "linear-gradient(135deg, #10b981 0%, #059669 100%)",
                      border: "none",
                      borderRadius: "10px",
                      color: "#ffffff",
                      fontSize: "13px",
                      fontWeight: "700",
                      cursor: remediatingId === v.ad_id ? "not-allowed" : "pointer",
                      display: "flex",
                      alignItems: "center",
                      gap: "8px",
                      boxShadow: "0 4px 14px rgba(16, 185, 129, 0.3)",
                    }}
                  >
                    {remediatingId === v.ad_id ? (
                      "Notifying & Re-Dispatching..."
                    ) : (
                      "✓ 1-Click HITL Approve AI Auto-Fix & Re-Dispatch"
                    )}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      <GlassToast toast={toast} onClose={() => setToast(null)} />
    </div>
  );
}
