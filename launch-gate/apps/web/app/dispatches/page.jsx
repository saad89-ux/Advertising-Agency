"use client";

import React, { useState, useEffect } from "react";
import SocialFeedModal from "../../components/SocialFeedModal";

export default function DispatchesPage({ apiBaseUrl = "http://localhost:8000" }) {
  const [tracesData, setTracesData] = useState(null);
  const [campaigns, setCampaigns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [previewAd, setPreviewAd] = useState(null);

  const fetchData = async () => {
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      
      const [tracesRes, campaignsRes] = await Promise.all([
        fetch(`${apiBaseUrl}/campaign-flow/mcp-traces`, { headers }),
        fetch(`${apiBaseUrl}/campaign-flow/all-campaigns`, { headers })
      ]);

      if (tracesRes.ok) {
        const data = await tracesRes.json();
        setTracesData(data);
      }
      if (campaignsRes.ok) {
        const data = await campaignsRes.json();
        setCampaigns(data.clients || []);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, [apiBaseUrl]);

  const activeTraces = tracesData?.traces || [];
  const activeListenersCount = tracesData?.active_listeners?.length || 0;

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
              <span style={{ fontSize: "24px" }}>📡</span>
              <h1 style={{
                fontSize: "28px",
                fontWeight: "800",
                letterSpacing: "-0.02em",
                margin: 0,
                background: "linear-gradient(180deg, #ffffff 0%, #94a3b8 100%)",
                WebkitBackgroundClip: "text",
                WebkitTextFillColor: "transparent",
              }}>
                Live Campaign Dispatches & MCP Server Traces
              </h1>
            </div>
            <p style={{ fontSize: "14px", color: "#94a3b8", margin: 0 }}>
              Audit monitoring of automated campaign dispatches across connected platform connectors.
            </p>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
            <div style={{
              background: activeListenersCount > 0 ? "rgba(16, 185, 129, 0.12)" : "rgba(100, 116, 139, 0.12)",
              border: activeListenersCount > 0 ? "1px solid rgba(16, 185, 129, 0.3)" : "1px solid rgba(100, 116, 139, 0.3)",
              padding: "6px 14px",
              borderRadius: "999px",
              fontSize: "12px",
              fontWeight: "600",
              color: activeListenersCount > 0 ? "#34d399" : "#94a3b8",
              display: "flex",
              alignItems: "center",
              gap: "8px",
            }}>
              <span style={{
                display: "inline-block",
                width: "8px",
                height: "8px",
                borderRadius: "50%",
                background: activeListenersCount > 0 ? "#10b981" : "#64748b",
                boxShadow: activeListenersCount > 0 ? "0 0 10px #10b981" : "none"
              }}></span>
              {activeListenersCount > 0 ? `${activeListenersCount} MCP Servers Connected` : "0 Connectors Active (Local Development)"}
            </div>
          </div>
        </div>

        {/* 1. MCP Platform Dispatch Engine Status */}
        <div style={{
          background: "rgba(15, 23, 42, 0.75)",
          backdropFilter: "blur(20px)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "16px",
          padding: "20px 24px",
          marginBottom: "28px",
          boxShadow: "0 10px 30px rgba(0, 0, 0, 0.3)",
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <span style={{ fontSize: "16px" }}>⚡</span>
              <span style={{ fontSize: "14px", fontWeight: "700", color: "#f8fafc" }}>
                Multi-Platform Dispatch Pipeline Status
              </span>
            </div>
            <span style={{ fontSize: "13px", fontWeight: "600", color: activeTraces.length > 0 ? "#10b981" : "#94a3b8" }}>
              {activeTraces.length > 0 ? `Active (${activeTraces.length} recorded events)` : "Idle / Local Development"}
            </span>
          </div>
          <p style={{ fontSize: "13px", color: "#64748b", margin: 0 }}>
            {activeTraces.length > 0
              ? "Platform dispatch events recorded in audit log. External platform API execution is tracked below."
              : "No active platform dispatches. Real social platform MCP connectors are not configured in local development."}
          </p>
        </div>

        {/* 2. MCP Server Live Traces Terminal */}
        <div style={{
          background: "#050811",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "16px",
          padding: "20px",
          marginBottom: "36px",
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "14px", borderBottom: "1px solid rgba(255, 255, 255, 0.06)", paddingBottom: "10px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <span style={{ width: "10px", height: "10px", borderRadius: "50%", background: "#ef4444", display: "inline-block" }}></span>
              <span style={{ width: "10px", height: "10px", borderRadius: "50%", background: "#f59e0b", display: "inline-block" }}></span>
              <span style={{ width: "10px", height: "10px", borderRadius: "50%", background: "#10b981", display: "inline-block" }}></span>
              <span style={{ fontSize: "12px", color: "#94a3b8", marginLeft: "6px" }}>MCP Tool Calls & Platform Dispatches</span>
            </div>
            <span style={{ fontSize: "11px", color: "#64748b" }}>
              {activeTraces.length > 0 ? "Live Audit Stream" : "No Calls Recorded"}
            </span>
          </div>

          {activeTraces.length > 0 ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "8px", fontSize: "12px", lineHeight: "1.6" }}>
              {activeTraces.map((t, idx) => (
                <div key={idx} style={{ display: "flex", gap: "12px", padding: "6px 8px", background: "rgba(255, 255, 255, 0.02)", borderRadius: "6px" }}>
                  <span style={{ color: "#64748b" }}>[{t.timestamp}]</span>
                  <span style={{ color: "#38bdf8", fontWeight: "600" }}>{t.server}</span>
                  <span style={{ color: "#a78bfa" }}>→ {t.tool}</span>
                  <span style={{ color: t.status === "DISPATCH_FAILED" ? "#ef4444" : "#10b981" }}>{t.status}</span>
                  <span style={{ color: "#f59e0b" }}>({t.duration_ms}ms)</span>
                  <span style={{ color: "#94a3b8", flex: 1 }}>{t.payload_summary}</span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ padding: "24px 12px", textAlign: "center", color: "#64748b", fontSize: "13px" }}>
              No active platform dispatches. Real social platform MCP connectors are not configured in local development.
            </div>
          )}
        </div>

        {/* 3. Client Multi-Platform Active Delivery Matrix */}
        <div>
          <h2 style={{ fontSize: "20px", fontWeight: "800", color: "#f8fafc", margin: "0 0 16px 0" }}>
            Live Platform Deployments by Client
          </h2>

          {campaigns.length === 0 || !campaigns.some(c => (c.campaigns || []).length > 0) ? (
            <div style={{
              background: "rgba(15, 23, 42, 0.7)",
              border: "1px dashed rgba(255, 255, 255, 0.12)",
              borderRadius: "16px",
              padding: "40px 24px",
              textAlign: "center",
              color: "#94a3b8"
            }}>
              <div style={{ fontSize: "32px", marginBottom: "12px" }}>📦</div>
              <div style={{ fontSize: "15px", fontWeight: "600", color: "#e2e8f0", marginBottom: "6px" }}>
                No Active Live Deployments
              </div>
              <div style={{ fontSize: "13px", color: "#64748b", maxWidth: "480px", margin: "0 auto" }}>
                Approved campaigns will appear here once dispatched via verified MCP platform connectors.
              </div>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
              {campaigns.filter(c => (c.campaigns || []).length > 0).map((client) => (
                <div
                  key={client.client_id}
                  style={{
                    background: "rgba(15, 23, 42, 0.7)",
                    backdropFilter: "blur(20px)",
                    border: "1px solid rgba(255, 255, 255, 0.08)",
                    borderRadius: "16px",
                    padding: "24px",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px", borderBottom: "1px solid rgba(255, 255, 255, 0.06)", paddingBottom: "12px" }}>
                    <div>
                      <span style={{ fontSize: "18px", fontWeight: "700", color: "#ffffff", marginRight: "12px" }}>
                        {client.client_name}
                      </span>
                      <span style={{ fontSize: "12px", padding: "2px 8px", background: "rgba(99, 102, 241, 0.15)", color: "#818cf8", borderRadius: "6px", fontWeight: "600" }}>
                        Client ID: {client.client_id}
                      </span>
                    </div>
                    <span style={{ fontSize: "12px", color: "#64748b" }}>
                      Active: {client.active_campaigns || 0} / Total: {client.total_campaigns || 0}
                    </span>
                  </div>

                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: "16px" }}>
                    {(client.campaigns || []).map((camp, idx) => (
                      <div
                        key={idx}
                        style={{
                          background: "rgba(8, 12, 20, 0.6)",
                          border: "1px solid rgba(255, 255, 255, 0.06)",
                          borderRadius: "12px",
                          padding: "16px",
                          display: "flex",
                          flexDirection: "column",
                          justifyContent: "space-between",
                        }}
                      >
                        <div>
                          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "10px" }}>
                            <span style={{ fontSize: "14px", fontWeight: "700", color: "#e2e8f0" }}>
                              {camp.name}
                            </span>
                            <span style={{
                              fontSize: "11px",
                              padding: "2px 8px",
                              borderRadius: "999px",
                              background: camp.status === "ACTIVE" ? "rgba(16, 185, 129, 0.15)" : "rgba(245, 158, 11, 0.15)",
                              color: camp.status === "ACTIVE" ? "#10b981" : "#f59e0b",
                              fontWeight: "600",
                            }}>
                              ● {camp.status}
                            </span>
                          </div>

                          <div style={{ fontSize: "12px", color: "#64748b", marginBottom: "4px" }}>
                            Campaign ID: <span style={{ color: "#94a3b8", fontWeight: "600" }}>{camp.campaign_id}</span>
                          </div>
                          <div style={{ fontSize: "13px", fontWeight: "600", color: "#cbd5e1", marginBottom: "14px" }}>
                            {camp.platform ? `Platform: ${camp.platform}` : "Unconfigured Platform"}
                          </div>
                        </div>

                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", borderTop: "1px solid rgba(255, 255, 255, 0.06)", paddingTop: "12px" }}>
                          <div style={{ fontSize: "12px" }}>
                            <span style={{ color: "#64748b" }}>Spend: </span>
                            <span style={{ color: "#f8fafc", fontWeight: "600" }}>${Number(camp.spend || 0).toFixed(2)}</span>
                            <span style={{ color: "#64748b", marginLeft: "8px" }}>Orders: </span>
                            <span style={{ color: "#10b981", fontWeight: "600" }}>{camp.orders || 0}</span>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {previewAd && (
        <SocialFeedModal
          adId={previewAd.ad_id}
          platform={previewAd.platform}
          headline={previewAd.headline}
          clientHandle={previewAd.handle}
          onClose={() => setPreviewAd(null)}
        />
      )}
    </div>
  );
}
