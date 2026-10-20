"use client";

import React, { useState, useEffect } from "react";
import SocialFeedModal from "../../components/SocialFeedModal";

export default function AttributionPage() {
  const [summary, setSummary] = useState(null);
  const [ads, setAds] = useState([]);
  const [liveEvents, setLiveEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filterPlatform, setFilterPlatform] = useState("");
  const [filterClient, setFilterClient] = useState("");
  const [selectedAdForFeed, setSelectedAdForFeed] = useState(null);
  const [simulatingId, setSimulatingId] = useState(null);

  const apiBaseUrl = "http://localhost:8000";

  const fetchAttributionData = async () => {
    try {
      const token = localStorage.getItem("launch_gate_token");
      const headers = token ? { Authorization: `Bearer ${token}` } : {};

      const [sumRes, adsRes, eventsRes] = await Promise.all([
        fetch(`${apiBaseUrl}/attribution/summary`, { headers }),
        fetch(`${apiBaseUrl}/attribution/ads`, { headers }),
        fetch(`${apiBaseUrl}/attribution/live-events`, { headers }),
      ]);

      if (sumRes.ok) setSummary(await sumRes.json());
      if (adsRes.ok) setAds(await adsRes.json());
      if (eventsRes.ok) setLiveEvents(await eventsRes.json());
    } catch (err) {
      console.error("Failed to load attribution data:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAttributionData();
    // Poll live events stream every 5 seconds
    const interval = setInterval(fetchAttributionData, 5000);
    return () => clearInterval(interval);
  }, []);

  const handleSimulateSale = async (adId) => {
    setSimulatingId(adId);
    try {
      const token = localStorage.getItem("launch_gate_token");
      const res = await fetch(`${apiBaseUrl}/attribution/simulate-sale`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          ad_id: adId,
          order_amount: 89.5,
          customer_city: "Austin, TX",
        }),
      });
      if (res.ok) {
        fetchAttributionData();
      }
    } catch (err) {
      console.error("Failed to simulate sale:", err);
    } finally {
      setSimulatingId(null);
    }
  };

  const filteredAds = ads.filter((a) => {
    if (filterPlatform && a.platform?.toLowerCase() !== filterPlatform.toLowerCase()) return false;
    if (filterClient && a.client_id !== filterClient) return false;
    return true;
  });

  return (
    <div style={{ minHeight: "100vh", background: "#080c14", color: "#f8fafc", fontFamily: "sans-serif" }}>
      <main style={{ maxWidth: "1440px", margin: "0 auto", padding: "32px 24px" }}>
        {/* Banner */}
        <div style={{
          background: "linear-gradient(135deg, rgba(16, 185, 129, 0.2) 0%, rgba(15, 23, 42, 0.6) 100%)",
          border: "1px solid rgba(16, 185, 129, 0.25)",
          borderRadius: "18px",
          padding: "28px 32px",
          marginBottom: "32px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          backdropFilter: "blur(20px)",
          boxShadow: "0 10px 30px -10px rgba(0, 0, 0, 0.5)",
        }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "8px" }}>
              <span style={{
                background: "rgba(16, 185, 129, 0.2)",
                color: "#34d399",
                fontSize: "11px",
                fontWeight: 700,
                letterSpacing: "0.1em",
                textTransform: "uppercase",
                padding: "3px 10px",
                borderRadius: "20px",
                border: "1px solid rgba(16, 185, 129, 0.4)",
              }}>
                Live Conversion Engine
              </span>
              <span style={{ fontSize: "12px", color: "#38bdf8", fontWeight: 600 }}>
                ● Real-Time UTM Attribution Streaming
              </span>
            </div>
            <h1 style={{ fontSize: "28px", fontWeight: 800, margin: "0 0 6px 0", letterSpacing: "-0.02em" }}>
              Continuous Inbound Sales & Ad Attribution
            </h1>
            <p style={{ color: "#94a3b8", fontSize: "14px", margin: 0, maxWidth: "720px", lineHeight: "1.5" }}>
              Tracking which approved ads are generating inbound sales, verified purchases, and ROAS across Instagram, TikTok, Facebook, Snapchat, and X handles.
            </p>
          </div>

          <div style={{ textAlign: "right" }}>
            <div style={{ fontSize: "12px", color: "#94a3b8", textTransform: "uppercase" }}>Total Attributed Revenue</div>
            <div style={{ fontSize: "36px", fontWeight: 900, color: "#10b981", letterSpacing: "-0.02em" }}>
              ${summary?.total_inbound_sales_usd?.toLocaleString(undefined, { minimumFractionDigits: 2 }) || "0.00"}
            </div>
            <div style={{ fontSize: "12px", color: "#64748b" }}>
              Across {summary?.total_inbound_orders || 0} customer orders • Avg ROAS: <strong style={{ color: "#38bdf8" }}>{summary?.overall_roas || 0}x</strong>
            </div>
          </div>
        </div>

        {/* Top KPI Cards */}
        {summary && (
          <div style={{
            display: "grid",
            gridTemplateColumns: "repeat(4, 1fr)",
            gap: "16px",
            marginBottom: "32px",
          }}>
            {[
              { label: "Total Inbound Orders", val: summary.total_inbound_orders, sub: "Direct checkout conversions", color: "#38bdf8" },
              { label: "Aggregate ROAS", val: `${summary.overall_roas}x`, sub: `Return on $${summary.total_spend_usd?.toLocaleString()} spend`, color: "#34d399" },
              { label: "Total Impressions", val: summary.total_impressions?.toLocaleString(), sub: `Avg CTR: ${summary.overall_ctr_percent}%`, color: "#a78bfa" },
              { label: "Platform Policy Flags", val: summary.flagged_violations_count, sub: `${summary.clean_ads_count} clean active ads`, color: summary.flagged_violations_count > 0 ? "#f87171" : "#10b981" },
            ].map((stat, i) => (
              <div
                key={i}
                style={{
                  background: "rgba(15, 23, 42, 0.6)",
                  border: "1px solid rgba(255, 255, 255, 0.08)",
                  borderRadius: "14px",
                  padding: "18px 20px",
                  backdropFilter: "blur(12px)",
                }}
              >
                <div style={{ fontSize: "12px", color: "#94a3b8", textTransform: "uppercase" }}>
                  {stat.label}
                </div>
                <div style={{ fontSize: "24px", fontWeight: 800, color: stat.color, margin: "6px 0 2px 0" }}>
                  {stat.val}
                </div>
                <div style={{ fontSize: "11px", color: "#64748b" }}>
                  {stat.sub}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Sales by Platform Breakdown & Live Ticker */}
        <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr", gap: "24px", marginBottom: "32px" }}>
          {/* Platform Performance Bars */}
          <div style={{
            background: "rgba(15, 23, 42, 0.6)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "16px",
            padding: "24px",
            backdropFilter: "blur(12px)",
          }}>
            <h3 style={{ fontSize: "16px", fontWeight: 700, margin: "0 0 16px 0", color: "#f8fafc" }}>
              Inbound Sales by Social Gateway
            </h3>

            <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
              {summary?.platforms_breakdown?.map((p) => {
                const maxSales = summary.platforms_breakdown[0]?.sales_usd || 1;
                const pct = Math.round((p.sales_usd / maxSales) * 100);

                return (
                  <div key={p.platform}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: "13px", marginBottom: "6px" }}>
                      <span style={{ fontWeight: 700, color: "#f8fafc", textTransform: "capitalize" }}>
                        {p.platform} ({p.active_ads} active ads)
                      </span>
                      <div style={{ display: "flex", gap: "12px" }}>
                        <span style={{ color: "#34d399", fontWeight: 700 }}>
                          ${p.sales_usd.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                        </span>
                        <span style={{ color: "#60a5fa", fontWeight: 600 }}>
                          {p.roas}x ROAS
                        </span>
                      </div>
                    </div>
                    <div style={{
                      width: "100%",
                      height: "8px",
                      background: "rgba(255, 255, 255, 0.06)",
                      borderRadius: "4px",
                      overflow: "hidden",
                    }}>
                      <div style={{
                        width: `${pct}%`,
                        height: "100%",
                        background: "linear-gradient(90deg, #3b82f6, #10b981)",
                        borderRadius: "4px",
                        transition: "width 0.5s ease",
                      }} />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Live Inbound Sales Ticker */}
          <div style={{
            background: "rgba(15, 23, 42, 0.6)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "16px",
            padding: "24px",
            backdropFilter: "blur(12px)",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
              <h3 style={{ fontSize: "16px", fontWeight: 700, margin: 0, color: "#f8fafc" }}>
                Live Inbound Sales Ticker
              </h3>
              <span style={{ fontSize: "11px", color: "#10b981", fontWeight: 600 }}>
                ● Real-time
              </span>
            </div>

            <div style={{
              display: "flex",
              flexDirection: "column",
              gap: "10px",
              maxHeight: "220px",
              overflowY: "auto",
            }}>
              {liveEvents.map((ev, i) => (
                <div
                  key={i}
                  style={{
                    background: ev.type === "policy_alert" ? "rgba(239, 68, 68, 0.1)" : "rgba(16, 185, 129, 0.08)",
                    border: ev.type === "policy_alert" ? "1px solid rgba(239, 68, 68, 0.25)" : "1px solid rgba(16, 185, 129, 0.2)",
                    borderRadius: "10px",
                    padding: "10px 14px",
                    display: "flex",
                    alignItems: "center",
                    gap: "10px",
                  }}
                >
                  <span style={{ fontSize: "16px" }}>
                    {ev.type === "policy_alert" ? "⚠️" : "🛒"}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{
                      fontSize: "12px",
                      color: ev.type === "policy_alert" ? "#f87171" : "#e2e8f0",
                      fontWeight: 600,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}>
                      {ev.event_text}
                    </div>
                    <div style={{ fontSize: "11px", color: "#94a3b8" }}>
                      {ev.handle} • {ev.client_id.toUpperCase()}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Detailed Ads Performance Matrix */}
        <div style={{
          background: "rgba(15, 23, 42, 0.7)",
          border: "1px solid rgba(255, 255, 255, 0.1)",
          borderRadius: "18px",
          padding: "24px",
          backdropFilter: "blur(20px)",
        }}>
          <div style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "20px",
          }}>
            <div>
              <h2 style={{ fontSize: "18px", fontWeight: 800, margin: "0 0 4px 0", color: "#f8fafc" }}>
                Ad Performance & Policy Status Matrix
              </h2>
              <p style={{ fontSize: "13px", color: "#94a3b8", margin: 0 }}>
                Every live ad with sales generation, ROAS, platform review status, and feed view.
              </p>
            </div>

            <div style={{ display: "flex", gap: "10px" }}>
              <select
                value={filterPlatform}
                onChange={(e) => setFilterPlatform(e.target.value)}
                style={{
                  background: "rgba(0, 0, 0, 0.4)",
                  border: "1px solid rgba(255, 255, 255, 0.15)",
                  color: "#f8fafc",
                  padding: "8px 12px",
                  borderRadius: "8px",
                  fontSize: "12px",
                  outline: "none",
                }}
              >
                <option value="">All Meta Platforms</option>
                <option value="instagram">Instagram</option>
                <option value="facebook">Meta / Facebook</option>
              </select>

              <select
                value={filterClient}
                onChange={(e) => setFilterClient(e.target.value)}
                style={{
                  background: "rgba(0, 0, 0, 0.4)",
                  border: "1px solid rgba(255, 255, 255, 0.15)",
                  color: "#f8fafc",
                  padding: "8px 12px",
                  borderRadius: "8px",
                  fontSize: "12px",
                  outline: "none",
                }}
              >
                <option value="">All Clients</option>
                {Array.from(new Set(ads.map((a) => a.client_id).filter(Boolean))).map((cid) => (
                  <option key={cid} value={cid}>
                    {cid.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase())}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {loading ? (
            <div style={{ padding: "40px", textAlign: "center", color: "#94a3b8" }}>
              Loading ad attribution performance...
            </div>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.1)", textAlign: "left" }}>
                    <th style={{ padding: "12px", color: "#94a3b8", fontWeight: 600 }}>Ad ID & Headline</th>
                    <th style={{ padding: "12px", color: "#94a3b8", fontWeight: 600 }}>Platform & Handle</th>
                    <th style={{ padding: "12px", color: "#94a3b8", fontWeight: 600 }}>Inbound Sales</th>
                    <th style={{ padding: "12px", color: "#94a3b8", fontWeight: 600 }}>Orders</th>
                    <th style={{ padding: "12px", color: "#94a3b8", fontWeight: 600 }}>ROAS</th>
                    <th style={{ padding: "12px", color: "#94a3b8", fontWeight: 600 }}>Platform Review</th>
                    <th style={{ padding: "12px", color: "#94a3b8", fontWeight: 600, textAlign: "right" }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredAds.map((ad) => (
                    <tr
                      key={ad.ad_id}
                      style={{
                        borderBottom: "1px solid rgba(255, 255, 255, 0.05)",
                        transition: "background 0.15s ease",
                      }}
                    >
                      <td style={{ padding: "14px 12px" }}>
                        <div style={{ fontWeight: 700, color: "#f8fafc" }}>
                          {ad.headline || "Untitled Ad"}
                        </div>
                        <div style={{ fontSize: "11px", color: "#64748b", fontFamily: "monospace" }}>
                          {ad.ad_id.slice(0, 12)}...
                        </div>
                      </td>

                      <td style={{ padding: "14px 12px" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                          <span style={{ fontWeight: 600, color: "#60a5fa" }}>{ad.handle}</span>
                        </div>
                        <div style={{ fontSize: "11px", color: "#94a3b8", textTransform: "capitalize" }}>
                          {ad.platform}
                        </div>
                      </td>

                      <td style={{ padding: "14px 12px", fontWeight: 800, color: ad.inbound_sales_usd != null ? "#10b981" : "#64748b", fontSize: "14px" }}>
                        {ad.inbound_sales_usd != null ? `$${ad.inbound_sales_usd.toLocaleString(undefined, { minimumFractionDigits: 2 })}` : "Not tracked"}
                      </td>

                      <td style={{ padding: "14px 12px", color: ad.inbound_orders != null ? "#cbd5e1" : "#64748b" }}>
                        {ad.inbound_orders != null ? ad.inbound_orders : "Not tracked"}
                      </td>

                      <td style={{ padding: "14px 12px" }}>
                        {ad.roas != null ? (
                          <span style={{
                            padding: "3px 8px",
                            borderRadius: "6px",
                            fontSize: "12px",
                            fontWeight: 700,
                            background: ad.roas >= 4.0 ? "rgba(16, 185, 129, 0.2)" : "rgba(59, 130, 246, 0.2)",
                            color: ad.roas >= 4.0 ? "#34d399" : "#60a5fa",
                          }}>
                            {ad.roas}x
                          </span>
                        ) : (
                          <span style={{ color: "#64748b", fontSize: "12px" }}>Not tracked</span>
                        )}
                      </td>

                      <td style={{ padding: "14px 12px" }}>
                        {ad.has_rule_violation ? (
                          <span style={{
                            padding: "4px 8px",
                            borderRadius: "6px",
                            fontSize: "11px",
                            fontWeight: 700,
                            background: "rgba(239, 68, 68, 0.2)",
                            color: "#f87171",
                            border: "1px solid rgba(239, 68, 68, 0.4)",
                          }}>
                            ⚠️ Policy Flagged
                          </span>
                        ) : (
                          <span style={{
                            padding: "4px 8px",
                            borderRadius: "6px",
                            fontSize: "11px",
                            fontWeight: 700,
                            background: "rgba(16, 185, 129, 0.15)",
                            color: "#34d399",
                            border: "1px solid rgba(16, 185, 129, 0.3)",
                          }}>
                            ✓ {ad.platform_review_status || "Approved"}
                          </span>
                        )}
                      </td>

                      <td style={{ padding: "14px 12px", textAlign: "right" }}>
                        <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
                          <button
                            onClick={() => handleSimulateSale(ad.ad_id)}
                            disabled={simulatingId === ad.ad_id}
                            title="Simulate verified customer checkout"
                            style={{
                              background: "rgba(16, 185, 129, 0.2)",
                              border: "1px solid rgba(16, 185, 129, 0.4)",
                              color: "#34d399",
                              borderRadius: "8px",
                              padding: "6px 10px",
                              fontSize: "11px",
                              fontWeight: 700,
                              cursor: "pointer",
                            }}
                          >
                            {simulatingId === ad.ad_id ? "+$..." : "+ $89.50 Sale"}
                          </button>

                          <button
                            onClick={() => setSelectedAdForFeed(ad)}
                            style={{
                              background: "rgba(59, 130, 246, 0.2)",
                              border: "1px solid rgba(59, 130, 246, 0.4)",
                              color: "#60a5fa",
                              borderRadius: "8px",
                              padding: "6px 12px",
                              fontSize: "11px",
                              fontWeight: 700,
                              cursor: "pointer",
                            }}
                          >
                            View Live Feed 📱
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {selectedAdForFeed && (
          <SocialFeedModal ad={selectedAdForFeed} onClose={() => setSelectedAdForFeed(null)} />
        )}
      </main>
    </div>
  );
}
