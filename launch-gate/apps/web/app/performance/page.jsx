"use client";

import React, { useState, useEffect } from "react";

export default function PerformancePage({ apiBaseUrl = "http://localhost:8000" }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState("sales"); // 'sales' | 'spend' | 'roas'

  const fetchPerformance = async () => {
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await fetch(`${apiBaseUrl}/campaign-flow/platform-performance`, { headers });
      if (res.ok) {
        const json = await res.json();
        setData(json);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchPerformance();
  }, [apiBaseUrl]);

  const liveOrders = [];

  const platforms = data?.platform_breakdown || [];
  const maxSales = Math.max(...platforms.map((p) => p.inbound_sales_usd || 0), 1000);

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
              <span style={{ fontSize: "24px" }}>📈</span>
              <h1 style={{
                fontSize: "28px",
                fontWeight: "800",
                letterSpacing: "-0.02em",
                margin: 0,
                background: "linear-gradient(180deg, #ffffff 0%, #94a3b8 100%)",
                WebkitBackgroundClip: "text",
                WebkitTextFillColor: "transparent",
              }}>
                Multi-Platform Live Performance & Inbound Sales
              </h1>
            </div>
            <p style={{ fontSize: "14px", color: "#94a3b8", margin: 0 }}>
              Continuous real-time attribution tracking, inbound conversions, spend efficiency, and ROAS across platforms.
            </p>
          </div>

          <button
            onClick={fetchPerformance}
            style={{
              padding: "8px 16px",
              background: "rgba(255, 255, 255, 0.08)",
              border: "1px solid rgba(255, 255, 255, 0.12)",
              borderRadius: "8px",
              color: "#ffffff",
              fontSize: "12px",
              fontWeight: "600",
              cursor: "pointer",
            }}
          >
            ↻ Refresh Metrics
          </button>
        </div>

        {/* 1. Summary KPI Cards */}
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
          gap: "16px",
          marginBottom: "28px",
        }}>
          {[
            { label: "Total Inbound Sales", value: `$${(data?.summary?.total_sales_usd ?? 0).toLocaleString()}`, color: "#10b981", change: (data?.summary?.total_sales_usd || 0) > 0 ? "Tracking live" : "No sales recorded" },
            { label: "Blended ROAS", value: `${data?.summary?.blended_roas ?? 0}x`, color: "#818cf8", change: (data?.summary?.blended_roas || 0) > 0 ? "Target active" : "Unconfigured" },
            { label: "Total Ad Spend", value: `$${(data?.summary?.total_spend_usd ?? 0).toLocaleString()}`, color: "#f59e0b", change: "Spend tracking active" },
            { label: "Attributed Orders", value: `${data?.summary?.total_orders ?? 0}`, color: "#38bdf8", change: "Verified via UTMs" },
            { label: "Total Impressions", value: `${(data?.summary?.total_impressions ?? 0).toLocaleString()}`, color: "#ec4899", change: "Live across networks" },
          ].map((kpi, idx) => (
            <div
              key={idx}
              style={{
                background: "rgba(15, 23, 42, 0.75)",
                backdropFilter: "blur(20px)",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderRadius: "14px",
                padding: "20px",
              }}
            >
              <div style={{ fontSize: "12px", color: "#94a3b8", fontWeight: "600", marginBottom: "8px" }}>
                {kpi.label}
              </div>
              <div style={{ fontSize: "28px", fontWeight: "800", color: kpi.color, marginBottom: "4px" }}>
                {kpi.value}
              </div>
              <div style={{ fontSize: "11px", color: "#64748b" }}>
                {kpi.change}
              </div>
            </div>
          ))}
        </div>

        {/* 2. Charts Section: Bar Chart & Donut Chart */}
        <div style={{
          display: "grid",
          gridTemplateColumns: "1.6fr 1fr",
          gap: "24px",
          marginBottom: "32px",
        }}>
          {/* Bar Chart: Performance by Platform */}
          <div style={{
            background: "rgba(15, 23, 42, 0.75)",
            backdropFilter: "blur(20px)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "16px",
            padding: "24px",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
              <h3 style={{ margin: 0, fontSize: "16px", fontWeight: "700", color: "#f8fafc" }}>
                Platform Inbound Sales Comparison (USD)
              </h3>
              <div style={{ display: "flex", gap: "6px" }}>
                {["sales", "spend", "roas"].map((tab) => (
                  <button
                    key={tab}
                    onClick={() => setActiveTab(tab)}
                    style={{
                      padding: "4px 10px",
                      borderRadius: "6px",
                      fontSize: "11px",
                      fontWeight: "600",
                      background: activeTab === tab ? "rgba(99, 102, 241, 0.25)" : "transparent",
                      color: activeTab === tab ? "#818cf8" : "#64748b",
                      border: "none",
                      cursor: "pointer",
                      textTransform: "uppercase",
                    }}
                  >
                    {tab}
                  </button>
                ))}
              </div>
            </div>

            {/* SVG Custom Horizontal Bar Graph */}
            <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
              {platforms.map((p, idx) => {
                const val = activeTab === "sales" ? p.inbound_sales_usd : (activeTab === "spend" ? p.spend_usd : p.roas * 1000);
                const percent = Math.min(100, Math.max(10, (val / maxSales) * 100));
                const colors = ["#6366f1", "#ec4899", "#10b981", "#38bdf8", "#f59e0b"];

                return (
                  <div key={idx}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: "13px", marginBottom: "6px" }}>
                      <span style={{ fontWeight: "600", color: "#e2e8f0" }}>{p.display_name}</span>
                      <span style={{ fontWeight: "700", color: colors[idx % colors.length] }}>
                        {activeTab === "roas" ? `${p.roas}x ROAS` : `$${val.toLocaleString()}`}
                      </span>
                    </div>

                    <div style={{ width: "100%", height: "12px", background: "rgba(255, 255, 255, 0.05)", borderRadius: "999px", overflow: "hidden" }}>
                      <div style={{
                        width: `${percent}%`,
                        height: "100%",
                        background: colors[idx % colors.length],
                        borderRadius: "999px",
                        transition: "width 0.6s cubic-bezier(0.16, 1, 0.3, 1)",
                      }} />
                    </div>

                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", color: "#64748b", marginTop: "4px" }}>
                      <span>Spend: ${p.spend_usd}</span>
                      <span>Orders: {p.orders}</span>
                      <span>CTR: {p.ctr_percent}%</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Donut Chart: Campaign Delivery Share */}
          <div style={{
            background: "rgba(15, 23, 42, 0.75)",
            backdropFilter: "blur(20px)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "16px",
            padding: "24px",
            display: "flex",
            flexDirection: "column",
          }}>
            <h3 style={{ margin: "0 0 16px 0", fontSize: "16px", fontWeight: "700", color: "#f8fafc" }}>
              Live Delivery & Review Share
            </h3>

            {/* SVG Donut */}
            <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "180px" }}>
              <svg width="160" height="160" viewBox="0 0 160 160">
                <circle cx="80" cy="80" r="60" fill="none" stroke="#1e293b" strokeWidth="16" />
                {/* Active 74% */}
                <circle cx="80" cy="80" r="60" fill="none" stroke="#10b981" strokeWidth="16"
                  strokeDasharray={`${(74 / 100) * 377} 377`}
                  strokeDashoffset="0"
                  transform="rotate(-90 80 80)"
                />
                {/* Review 14% */}
                <circle cx="80" cy="80" r="60" fill="none" stroke="#f59e0b" strokeWidth="16"
                  strokeDasharray={`${(14 / 100) * 377} 377`}
                  strokeDashoffset={`-${(74 / 100) * 377}`}
                  transform="rotate(-90 80 80)"
                />
                {/* Flagged 8% */}
                <circle cx="80" cy="80" r="60" fill="none" stroke="#f43f5e" strokeWidth="16"
                  strokeDasharray={`${(8 / 100) * 377} 377`}
                  strokeDashoffset={`-${((74 + 14) / 100) * 377}`}
                  transform="rotate(-90 80 80)"
                />
                <text x="80" y="76" textAnchor="middle" fill="#ffffff" fontSize="20" fontWeight="800">
                  74%
                </text>
                <text x="80" y="94" textAnchor="middle" fill="#94a3b8" fontSize="11" fontWeight="600">
                  Serving Live
                </text>
              </svg>
            </div>

            {/* Legend */}
            <div style={{ display: "flex", flexDirection: "column", gap: "8px", marginTop: "16px" }}>
              {(data?.donut_data && data.donut_data.length > 0) ? (
                data.donut_data.map((item, idx) => (
                  <div key={idx} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "12px" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                      <span style={{ width: "8px", height: "8px", borderRadius: "50%", background: item.color }} />
                      <span style={{ color: "#cbd5e1" }}>{item.label}</span>
                    </div>
                    <span style={{ fontWeight: "700", color: "#ffffff" }}>{item.value}</span>
                  </div>
                ))
              ) : (
                <div style={{ fontSize: "12px", color: "#64748b", textAlign: "center", padding: "12px 0" }}>
                  No active platform dispatches recorded.
                </div>
              )}
            </div>
          </div>
        </div>

        {/* 3. Live Attributed Inbound Conversions Stream */}
        <div style={{
          background: "rgba(15, 23, 42, 0.75)",
          backdropFilter: "blur(20px)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "16px",
          padding: "24px",
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
            <h3 style={{ margin: 0, fontSize: "16px", fontWeight: "700", color: "#f8fafc" }}>
              Continuous Inbound Conversion Stream (Live Attribution)
            </h3>
            <span style={{ fontSize: "12px", color: liveOrders.length > 0 ? "#10b981" : "#64748b", fontWeight: "600" }}>
              ● {liveOrders.length > 0 ? "Live Stream Active" : "Stream Idle (No Orders)"}
            </span>
          </div>

          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
              <thead>
                <tr style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.08)", color: "#64748b", textAlign: "left" }}>
                  <th style={{ padding: "10px 14px" }}>Order ID</th>
                  <th style={{ padding: "10px 14px" }}>Revenue</th>
                  <th style={{ padding: "10px 14px" }}>Client</th>
                  <th style={{ padding: "10px 14px" }}>Platform Source</th>
                  <th style={{ padding: "10px 14px" }}>Attributed Ad</th>
                  <th style={{ padding: "10px 14px" }}>Location</th>
                  <th style={{ padding: "10px 14px" }}>Time</th>
                </tr>
              </thead>
              <tbody>
                {liveOrders.length === 0 ? (
                  <tr>
                    <td colSpan={7} style={{ padding: "32px 14px", textAlign: "center", color: "#64748b" }}>
                      No live inbound conversions recorded yet. Attributed sales will appear here when campaigns are active.
                    </td>
                  </tr>
                ) : (
                  liveOrders.map((ord, idx) => (
                    <tr key={idx} style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.04)" }}>
                      <td style={{ padding: "12px 14px", fontWeight: "600", color: "#e2e8f0" }}>{ord.orderId}</td>
                      <td style={{ padding: "12px 14px", fontWeight: "700", color: "#10b981" }}>{ord.amount}</td>
                      <td style={{ padding: "12px 14px", color: "#cbd5e1" }}>{ord.client}</td>
                      <td style={{ padding: "12px 14px" }}>
                        <span style={{ padding: "2px 8px", background: "rgba(99, 102, 241, 0.15)", color: "#818cf8", borderRadius: "6px", fontSize: "11px", fontWeight: "600" }}>
                          {ord.platform}
                        </span>
                      </td>
                      <td style={{ padding: "12px 14px", color: "#94a3b8" }}>{ord.adId}</td>
                      <td style={{ padding: "12px 14px", color: "#cbd5e1" }}>{ord.city}</td>
                      <td style={{ padding: "12px 14px", color: "#64748b" }}>{ord.time}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
