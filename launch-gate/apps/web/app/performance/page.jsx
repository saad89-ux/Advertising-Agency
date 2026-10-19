"use client";

import React, { useState, useEffect } from "react";

export default function PerformancePage({ apiBaseUrl = "http://localhost:8000" }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState("sales");

  const fetchPerformance = async () => {
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await fetch(`${apiBaseUrl}/campaign-flow/platform-performance`, { headers });
      if (res.ok) {
        const perf = await res.json();
        setData(perf);
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
  const trackedPlatforms = platforms.filter((p) => p.tracked);
  const anyTracked = data?.summary?.tracked || trackedPlatforms.length > 0;
  const maxSales = Math.max(...trackedPlatforms.map((p) => p.inbound_sales_usd || 0), 1000);

  const donutTotal = (data?.donut_data || []).reduce((acc, curr) => acc + (curr.value || 0), 0);

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
            {
              label: "Total Inbound Sales",
              value: data?.summary?.total_sales_usd != null ? `$${data.summary.total_sales_usd.toLocaleString()}` : "Not tracked",
              color: "#10b981",
              change: data?.summary?.tracked ? "Tracking active" : "Not tracked",
            },
            {
              label: "Blended ROAS",
              value: data?.summary?.blended_roas != null ? `${data.summary.blended_roas}x` : "Not tracked",
              color: "#818cf8",
              change: data?.summary?.tracked ? "Efficiency verified" : "Not tracked",
            },
            {
              label: "Total Ad Spend",
              value: data?.summary?.total_spend_usd != null ? `$${data.summary.total_spend_usd.toLocaleString()}` : "Not tracked",
              color: "#f59e0b",
              change: data?.summary?.tracked ? "Spend recorded" : "Not tracked",
            },
            {
              label: "Attributed Orders",
              value: data?.summary?.total_orders != null ? `${data.summary.total_orders}` : "Not tracked",
              color: "#38bdf8",
              change: data?.summary?.tracked ? "Verified via UTMs" : "Not tracked",
            },
            {
              label: "Total Impressions",
              value: data?.summary?.total_impressions != null ? `${data.summary.total_impressions.toLocaleString()}` : "Not tracked",
              color: "#ec4899",
              change: data?.summary?.tracked ? "Live impressions" : "Not tracked",
            },
          ].map((kpi, idx) => (
            <div
              key={idx}
              style={{
                background: "rgba(15, 23, 42, 0.75)",
                backdropFilter: "blur(20px)",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderRadius: "16px",
                padding: "20px 24px",
                boxShadow: "0 10px 30px rgba(0, 0, 0, 0.2)",
              }}
            >
              <div style={{ fontSize: "12px", color: "#94a3b8", fontWeight: "600", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "8px" }}>
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
            {!anyTracked ? (
              <div style={{ padding: "48px 16px", textAlign: "center", color: "#64748b", fontSize: "13px" }}>
                <div style={{ fontSize: "32px", marginBottom: "8px" }}>📊</div>
                No platform telemetry recorded. Connect real social platform APIs to stream live revenue data.
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
                {platforms.map((p, idx) => {
                  const colors = ["#6366f1", "#ec4899", "#10b981", "#38bdf8", "#f59e0b"];
                  if (!p.tracked) {
                    return (
                      <div key={idx}>
                        <div style={{ display: "flex", justifyContent: "space-between", fontSize: "13px", marginBottom: "6px" }}>
                          <span style={{ fontWeight: "600", color: "#94a3b8" }}>{p.display_name}</span>
                          <span style={{ fontSize: "12px", color: "#64748b" }}>Not tracked</span>
                        </div>
                        <div style={{ width: "100%", height: "8px", background: "rgba(255, 255, 255, 0.05)", borderRadius: "999px" }} />
                      </div>
                    );
                  }

                  const val = activeTab === "sales" ? p.inbound_sales_usd : (activeTab === "spend" ? p.spend_usd : (p.roas || 0) * 1000);
                  const percent = Math.min(100, Math.max(5, (val / maxSales) * 100));

                  return (
                    <div key={idx}>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "13px", marginBottom: "6px" }}>
                        <span style={{ fontWeight: "600", color: "#e2e8f0" }}>{p.display_name}</span>
                        <span style={{ fontWeight: "700", color: colors[idx % colors.length] }}>
                          {activeTab === "roas" ? (p.roas != null ? `${p.roas}x ROAS` : "Not tracked") : (val != null ? `$${val.toLocaleString()}` : "Not tracked")}
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
                        <span>Spend: {p.spend_usd != null ? `$${p.spend_usd}` : "Not tracked"}</span>
                        <span>Orders: {p.orders != null ? p.orders : "Not tracked"}</span>
                        <span>CTR: {p.ctr_percent != null ? `${p.ctr_percent}%` : "Not tracked"}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Donut Chart: Delivery Status Breakdown */}
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

            {/* Donut Content */}
            {donutTotal === 0 ? (
              <div style={{ display: "flex", flexDirection: "column", justifyContent: "center", alignItems: "center", height: "180px", color: "#64748b" }}>
                <div style={{ fontSize: "32px", marginBottom: "8px" }}>⭕</div>
                <div style={{ fontSize: "13px", fontWeight: "600" }}>No Dispatches Tracked</div>
                <div style={{ fontSize: "11px", color: "#475569" }}>Dispatches will graph here when executed</div>
              </div>
            ) : (
              <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "180px" }}>
                <div style={{ textAlign: "center" }}>
                  <div style={{ fontSize: "28px", fontWeight: "800", color: "#ffffff" }}>{donutTotal}</div>
                  <div style={{ fontSize: "11px", color: "#94a3b8", fontWeight: "600" }}>Total Dispatches</div>
                </div>
              </div>
            )}

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

          <div style={{
            background: "rgba(8, 12, 20, 0.6)",
            border: "1px dashed rgba(255, 255, 255, 0.12)",
            borderRadius: "12px",
            padding: "36px 20px",
            textAlign: "center",
            color: "#64748b",
            fontSize: "13px"
          }}>
            No inbound orders detected. Once campaigns launch on verified platform ad networks with UTM links, live conversions appear here in real time.
          </div>
        </div>
      </div>
    </div>
  );
}
