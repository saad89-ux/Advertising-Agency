"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import GlassToast from "../../components/GlassToast";

export default function AlertsPage({ apiBaseUrl = "http://localhost:8000" }) {
  const [alerts, setAlerts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filterSeverity, setFilterSeverity] = useState("all");
  const [resolvingId, setResolvingId] = useState(null);
  const [resolveNote, setResolveNote] = useState("");
  const [toast, setToast] = useState(null);

  const fetchAlerts = () => {
    setLoading(true);
    const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
    const headers = { Accept: "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;

    fetch(`${apiBaseUrl}/alerts`, { headers })
      .then(async (res) => {
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        return res.json();
      })
      .then((data) => {
        setAlerts(data.alerts || []);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  };

  useEffect(() => {
    fetchAlerts();
  }, [apiBaseUrl]);

  const handleResolve = async (alertId) => {
    const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json",
    };
    if (token) headers["Authorization"] = `Bearer ${token}`;

    try {
      const res = await fetch(`${apiBaseUrl}/alerts/${alertId}/resolve`, {
        method: "POST",
        headers,
        body: JSON.stringify({ resolution_note: resolveNote || "Resolved via operations console" }),
      });
      if (res.ok) {
        setResolvingId(null);
        setResolveNote("");
        setToast({ message: `Alert ${alertId} marked as resolved.`, type: "success" });
        fetchAlerts();
      } else {
        setToast({ message: "Failed to resolve alert. Please check your credentials.", type: "error" });
      }
    } catch {
      setToast({ message: "Network error contacting API to resolve alert.", type: "error" });
    }
  };

  const filteredAlerts = alerts.filter((a) => {
    if (filterSeverity === "all") return true;
    return a.severity === filterSeverity;
  });

  return (
    <div style={{ maxWidth: "1400px", margin: "0 auto", padding: "32px 24px" }}>
      
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "28px", flexWrap: "wrap", gap: "16px" }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "11px", color: "#64748b", textTransform: "uppercase", fontWeight: "700", letterSpacing: "0.08em", marginBottom: "4px" }}>
            <span>Operations Center</span>
            <span>/</span>
            <span style={{ color: "#f43f5e" }}>System Alerts</span>
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
            Platform Dispatch & Quality Alerts
          </h1>
          <p style={{ margin: "6px 0 0 0", color: "#94a3b8", fontSize: "14px" }}>
            Live alerts triggered by upstream platform rate limits, network timeouts, or non-retryable execution errors.
          </p>
        </div>

        <button
          type="button"
          onClick={fetchAlerts}
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
          ↻ Refresh Alerts
        </button>
      </div>

      {/* Filter Tabs */}
      <div style={{ display: "flex", gap: "8px", marginBottom: "24px", flexWrap: "wrap" }}>
        {["all", "high", "warning", "info"].map((sev) => (
          <button
            key={sev}
            type="button"
            onClick={() => setFilterSeverity(sev)}
            style={{
              padding: "6px 14px",
              borderRadius: "8px",
              border: filterSeverity === sev ? "1px solid rgba(255, 255, 255, 0.25)" : "1px solid rgba(255, 255, 255, 0.08)",
              fontSize: "12px",
              fontWeight: "700",
              cursor: "pointer",
              background: filterSeverity === sev ? "rgba(59, 130, 246, 0.25)" : "rgba(15, 23, 42, 0.6)",
              color: filterSeverity === sev ? "#ffffff" : "#94a3b8",
              textTransform: "capitalize",
            }}
          >
            {sev === "all" ? `All Alerts (${alerts.length})` : sev}
          </button>
        ))}
      </div>

      {/* Alerts List */}
      {loading ? (
        <div style={{ padding: "40px", textAlign: "center", color: "#94a3b8" }}>Loading alerts...</div>
      ) : error ? (
        <div style={{ padding: "20px", background: "rgba(239, 68, 68, 0.12)", border: "1px solid rgba(239, 68, 68, 0.3)", color: "#f87171", borderRadius: "10px" }}>
          Failed to load alerts: {error}
        </div>
      ) : filteredAlerts.length === 0 ? (
        <div style={{
          background: "rgba(15, 23, 42, 0.65)",
          backdropFilter: "blur(20px)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "14px",
          padding: "50px 20px",
          textAlign: "center",
        }}>
          <div style={{ fontSize: "28px", marginBottom: "8px", color: "#34d399" }}>✓</div>
          <h3 style={{ margin: "0 0 4px 0", color: "#f8fafc", fontSize: "18px" }}>No Active Alerts</h3>
          <p style={{ color: "#94a3b8", margin: 0, fontSize: "13px" }}>
            All platform dispatch operations and verification checks are healthy.
          </p>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          {filteredAlerts.map((a) => {
            const isHigh = a.severity === "high";
            const isWarning = a.severity === "warning";
            const accentColor = isHigh ? "#f43f5e" : isWarning ? "#f59e0b" : "#38bdf8";

            return (
              <div
                key={a.alert_id}
                style={{
                  background: "rgba(15, 23, 42, 0.65)",
                  backdropFilter: "blur(20px)",
                  border: "1px solid rgba(255, 255, 255, 0.08)",
                  borderLeft: `4px solid ${accentColor}`,
                  borderRadius: "12px",
                  padding: "18px 20px",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "flex-start",
                  gap: "20px",
                  boxShadow: "0 10px 25px -5px rgba(0, 0, 0, 0.5)",
                }}
              >
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "6px", flexWrap: "wrap" }}>
                    <span style={{
                      fontSize: "10px",
                      fontWeight: "800",
                      textTransform: "uppercase",
                      padding: "2px 7px",
                      borderRadius: "4px",
                      background: isHigh ? "rgba(244, 63, 94, 0.15)" : "rgba(245, 158, 11, 0.15)",
                      color: isHigh ? "#f43f5e" : "#fbbf24",
                      border: `1px solid ${isHigh ? "rgba(244, 63, 94, 0.3)" : "rgba(245, 158, 11, 0.3)"}`,
                    }}>
                      {a.severity}
                    </span>
                    <span style={{ fontSize: "12px", color: "#94a3b8" }}>
                      Client: <strong style={{ color: "#cbd5e1" }}>{a.client_id}</strong> | Batch:{" "}
                      <Link href={`/batches/${a.batch_id}`} style={{ color: "#38bdf8", textDecoration: "none" }}>
                        #{a.batch_id.slice(0, 10)}
                      </Link>
                    </span>
                    {a.resolved && (
                      <span style={{ fontSize: "10px", color: "#34d399", background: "rgba(16, 185, 129, 0.15)", border: "1px solid rgba(16, 185, 129, 0.3)", padding: "2px 6px", borderRadius: "4px", fontWeight: "700" }}>
                        RESOLVED ✓
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: "14px", fontWeight: "700", color: "#f8fafc", marginBottom: "4px" }}>
                    {a.message}
                  </div>
                  {a.headline && (
                    <div style={{ fontSize: "12px", color: "#cbd5e1", background: "rgba(0, 0, 0, 0.3)", border: "1px solid rgba(255, 255, 255, 0.05)", padding: "6px 10px", borderRadius: "6px", marginTop: "8px" }}>
                      Ad Headline: "{a.headline}" | Platform: {a.platform}
                    </div>
                  )}
                </div>

                {!a.resolved && (
                  <div>
                    {resolvingId === a.alert_id ? (
                      <div style={{ display: "flex", flexDirection: "column", gap: "8px", minWidth: "220px" }}>
                        <input
                          type="text"
                          placeholder="Resolution note..."
                          value={resolveNote}
                          onChange={(e) => setResolveNote(e.target.value)}
                          style={{
                            padding: "6px 10px",
                            borderRadius: "6px",
                            background: "rgba(30, 41, 59, 0.6)",
                            border: "1px solid rgba(255, 255, 255, 0.15)",
                            color: "#ffffff",
                            fontSize: "12px",
                          }}
                        />
                        <div style={{ display: "flex", gap: "6px" }}>
                          <button
                            type="button"
                            onClick={() => handleResolve(a.alert_id)}
                            style={{
                              flex: 1,
                              background: "#10b981",
                              color: "#ffffff",
                              border: "none",
                              padding: "5px",
                              borderRadius: "6px",
                              fontSize: "11px",
                              fontWeight: "700",
                              cursor: "pointer",
                            }}
                          >
                            Confirm ✓
                          </button>
                          <button
                            type="button"
                            onClick={() => setResolvingId(null)}
                            style={{
                              background: "transparent",
                              border: "1px solid rgba(255, 255, 255, 0.2)",
                              color: "#cbd5e1",
                              padding: "5px 8px",
                              borderRadius: "6px",
                              fontSize: "11px",
                              cursor: "pointer",
                            }}
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setResolvingId(a.alert_id)}
                        style={{
                          background: "rgba(255, 255, 255, 0.08)",
                          border: "1px solid rgba(255, 255, 255, 0.15)",
                          color: "#cbd5e1",
                          borderRadius: "6px",
                          padding: "6px 12px",
                          fontSize: "12px",
                          fontWeight: "600",
                          cursor: "pointer",
                          whiteSpace: "nowrap",
                        }}
                      >
                        Resolve Alert ➔
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
      <GlassToast toast={toast} onClose={() => setToast(null)} />
    </div>
  );
}
