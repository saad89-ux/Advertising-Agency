"use client";

import React, { useState, useEffect } from "react";

export default function DashboardPage({ apiBaseUrl = "http://localhost:8000" }) {
  const [analytics, setAnalytics] = useState(null);
  const [loading, setLoading] = useState(true);
  const [timeframe, setTimeframe] = useState("daily"); // 'daily' | 'weekly' | 'monthly'
  const [selectedClient, setSelectedClient] = useState("all");

  const fetchAnalytics = async () => {
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await fetch(`${apiBaseUrl}/campaign-flow/revenue-analytics`, { headers });
      if (res.ok) {
        const data = await res.json();
        setAnalytics(data);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAnalytics();
  }, [apiBaseUrl]);

  const activeSeries = analytics ? (timeframe === "daily" ? analytics.daily : (timeframe === "weekly" ? analytics.weekly : analytics.monthly)) : [];
  const maxSeriesSales = Math.max(...activeSeries.map((s) => s.sales || 1), 10000);

  const clients = analytics?.clients || [];
  const filteredClients = selectedClient === "all" ? clients : clients.filter((c) => c.client_id === selectedClient);

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
              <span style={{ fontSize: "24px" }}>💎</span>
              <h1 style={{
                fontSize: "28px",
                fontWeight: "800",
                letterSpacing: "-0.02em",
                margin: 0,
                background: "linear-gradient(180deg, #ffffff 0%, #94a3b8 100%)",
                WebkitBackgroundClip: "text",
                WebkitTextFillColor: "transparent",
              }}>
                Executive Revenue & Multi-Platform Analytics
              </h1>
            </div>
            <p style={{ fontSize: "14px", color: "#94a3b8", margin: 0 }}>
              Live customer revenue across all social platforms: daily, weekly, and monthly growth trends by client.
            </p>
          </div>

          {/* Timeframe Toggle */}
          <div style={{
            display: "flex",
            background: "rgba(15, 23, 42, 0.8)",
            padding: "4px",
            borderRadius: "10px",
            border: "1px solid rgba(255, 255, 255, 0.08)",
          }}>
            {[
              { id: "daily", label: "Daily (7 Days)" },
              { id: "weekly", label: "Weekly (4 Weeks)" },
              { id: "monthly", label: "Monthly (MTD/Q3)" },
            ].map((t) => (
              <button
                key={t.id}
                onClick={() => setTimeframe(t.id)}
                style={{
                  padding: "6px 14px",
                  borderRadius: "8px",
                  fontSize: "12px",
                  fontWeight: "600",
                  background: timeframe === t.id ? "rgba(99, 102, 241, 0.25)" : "transparent",
                  color: timeframe === t.id ? "#818cf8" : "#94a3b8",
                  border: "none",
                  cursor: "pointer",
                }}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>

        {/* 1. Client Filter Selector */}
        <div style={{ display: "flex", gap: "10px", marginBottom: "24px", alignItems: "center" }}>
          <span style={{ fontSize: "12px", color: "#64748b", fontWeight: "600", textTransform: "uppercase" }}>Filter Client:</span>
          {[
            { id: "all", label: "All Clients" },
            { id: "brightleaf", label: "BrightLeaf Wellness" },
            { id: "calmleaf", label: "CalmLeaf Extracts" },
            { id: "oakandhome", label: "Oak & Home Design" },
          ].map((c) => (
            <button
              key={c.id}
              onClick={() => setSelectedClient(c.id)}
              style={{
                padding: "6px 14px",
                borderRadius: "8px",
                fontSize: "12px",
                fontWeight: "600",
                background: selectedClient === c.id ? "rgba(16, 185, 129, 0.15)" : "rgba(15, 23, 42, 0.6)",
                color: selectedClient === c.id ? "#34d399" : "#94a3b8",
                border: selectedClient === c.id ? "1px solid rgba(16, 185, 129, 0.3)" : "1px solid rgba(255, 255, 255, 0.06)",
                cursor: "pointer",
              }}
            >
              {c.label}
            </button>
          ))}
        </div>

        {/* 2. Revenue Trend Graph (SVG Visual) */}
        <div style={{
          background: "rgba(15, 23, 42, 0.75)",
          backdropFilter: "blur(20px)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "18px",
          padding: "26px",
          marginBottom: "32px",
          boxShadow: "0 15px 35px rgba(0, 0, 0, 0.3)",
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
            <div>
              <h3 style={{ margin: 0, fontSize: "16px", fontWeight: "700", color: "#f8fafc" }}>
                Multi-Platform Attributed Revenue Trend ({timeframe.toUpperCase()})
              </h3>
              <span style={{ fontSize: "12px", color: "#64748b" }}>
                Tracking actual inbound customer purchases attributed across connected social handles
              </span>
            </div>

            <div style={{ display: "flex", gap: "16px", fontSize: "12px" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                <span style={{ width: "10px", height: "10px", borderRadius: "2px", background: "#10b981" }}></span>
                <span style={{ color: "#cbd5e1" }}>Inbound Revenue</span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                <span style={{ width: "10px", height: "10px", borderRadius: "2px", background: "#6366f1" }}></span>
                <span style={{ color: "#cbd5e1" }}>Ad Spend</span>
              </div>
            </div>
          </div>

          {/* SVG Bar Chart Visualization */}
          {activeSeries.length === 0 ? (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "220px", color: "#64748b", fontSize: "14px", borderBottom: "1px solid rgba(255, 255, 255, 0.08)" }}>
              No revenue tracking series recorded yet. Attributed customer revenue will graph here when platform connectors stream events.
            </div>
          ) : (
            <div style={{ display: "flex", alignItems: "flex-end", height: "220px", gap: "20px", padding: "10px 0", borderBottom: "1px solid rgba(255, 255, 255, 0.08)" }}>
              {activeSeries.map((item, idx) => {
                const salesHeight = Math.min(190, Math.max(20, (item.sales / maxSeriesSales) * 190));
                const spendHeight = Math.min(190, Math.max(10, (item.spend / maxSeriesSales) * 190));
                const label = item.date || item.week || item.month;

                return (
                  <div key={idx} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", height: "100%", justifyContent: "flex-end" }}>
                    <div style={{ fontSize: "11px", fontWeight: "700", color: "#10b981", marginBottom: "6px" }}>
                      ${Math.round(item.sales / 1000)}k
                    </div>

                    <div style={{ display: "flex", alignItems: "flex-end", gap: "6px" }}>
                      {/* Sales Bar */}
                      <div
                        title={`Sales: $${item.sales.toLocaleString()}`}
                        style={{
                          width: "24px",
                          height: `${salesHeight}px`,
                          background: "linear-gradient(180deg, #10b981 0%, #059669 100%)",
                          borderRadius: "4px 4px 0 0",
                          transition: "height 0.4s ease",
                        }}
                      />
                      {/* Spend Bar */}
                      <div
                        title={`Spend: $${item.spend.toLocaleString()}`}
                        style={{
                          width: "16px",
                          height: `${spendHeight}px`,
                          background: "linear-gradient(180deg, #818cf8 0%, #4f46e5 100%)",
                          borderRadius: "4px 4px 0 0",
                          transition: "height 0.4s ease",
                        }}
                      />
                    </div>

                    <div style={{ fontSize: "12px", color: "#94a3b8", marginTop: "10px", fontWeight: "600" }}>
                      {label}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* 3. Client Breakdown Cards: Daily, Weekly, Monthly Stats */}
        <h2 style={{ fontSize: "20px", fontWeight: "800", color: "#f8fafc", margin: "0 0 16px 0" }}>
          Client-by-Client Growth Matrix
        </h2>

        {filteredClients.length === 0 ? (
          <div style={{ padding: "48px 24px", textAlign: "center", background: "rgba(15, 23, 42, 0.4)", borderRadius: "16px", border: "1px dashed rgba(255, 255, 255, 0.1)" }}>
            <span style={{ fontSize: "32px", display: "block", marginBottom: "12px" }}>📊</span>
            <h3 style={{ fontSize: "16px", fontWeight: "700", color: "#f8fafc", margin: "0 0 6px 0" }}>No Client Performance Data</h3>
            <p style={{ fontSize: "13px", color: "#94a3b8", margin: 0 }}>Client growth matrix will populate when ad spend and orders are recorded.</p>
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: "20px" }}>
          {filteredClients.map((c) => (
            <div
              key={c.client_id}
              style={{
                background: "rgba(15, 23, 42, 0.75)",
                backdropFilter: "blur(20px)",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderRadius: "16px",
                padding: "24px",
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px", borderBottom: "1px solid rgba(255, 255, 255, 0.06)", paddingBottom: "12px" }}>
                <div>
                  <h3 style={{ margin: 0, fontSize: "17px", fontWeight: "700", color: "#ffffff" }}>
                    {c.client_name}
                  </h3>
                  <span style={{ fontSize: "11px", color: "#818cf8" }}>
                    Top Engine: {c.top_platform}
                  </span>
                </div>
                <div style={{
                  padding: "4px 10px",
                  background: "rgba(16, 185, 129, 0.15)",
                  color: "#34d399",
                  borderRadius: "8px",
                  fontSize: "12px",
                  fontWeight: "700",
                }}>
                  {c.blended_roas}x ROAS
                </div>
              </div>

              {/* Stats Grid: Daily, Weekly, Monthly */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "12px", textAlign: "center" }}>
                <div style={{ background: "rgba(8, 12, 20, 0.6)", padding: "14px 10px", borderRadius: "10px" }}>
                  <div style={{ fontSize: "11px", color: "#94a3b8", marginBottom: "4px" }}>Daily Revenue</div>
                  <div style={{ fontSize: "18px", fontWeight: "800", color: "#10b981" }}>
                    ${c.daily_sales.toLocaleString()}
                  </div>
                </div>

                <div style={{ background: "rgba(8, 12, 20, 0.6)", padding: "14px 10px", borderRadius: "10px" }}>
                  <div style={{ fontSize: "11px", color: "#94a3b8", marginBottom: "4px" }}>Weekly Revenue</div>
                  <div style={{ fontSize: "18px", fontWeight: "800", color: "#38bdf8" }}>
                    ${c.weekly_sales.toLocaleString()}
                  </div>
                </div>

                <div style={{ background: "rgba(8, 12, 20, 0.6)", padding: "14px 10px", borderRadius: "10px" }}>
                  <div style={{ fontSize: "11px", color: "#94a3b8", marginBottom: "4px" }}>Monthly Revenue</div>
                  <div style={{ fontSize: "18px", fontWeight: "800", color: "#a78bfa" }}>
                    ${c.monthly_sales.toLocaleString()}
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
