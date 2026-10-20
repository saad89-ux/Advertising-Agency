"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import SocialFeedModal from "../../components/SocialFeedModal";

export default function CampaignsPage({ apiBaseUrl = "http://localhost:8000" }) {
  const [campaigns, setCampaigns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("all"); // 'all' | 'healthy' | 'issues'
  const [previewAd, setPreviewAd] = useState(null);

  const fetchCampaigns = async () => {
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await fetch(`${apiBaseUrl}/campaign-flow/all-campaigns`, { headers });
      if (res.ok) {
        const data = await res.json();
        setCampaigns(data);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchCampaigns();
  }, [apiBaseUrl]);

  const filteredCampaigns = campaigns.filter((c) => {
    if (filter === "healthy") return !c.has_rule_violation;
    if (filter === "issues") return c.has_rule_violation;
    return true;
  });

  const totalRevenue = campaigns.reduce((acc, c) => acc + (c.inbound_sales_usd || 0), 0);
  const totalSpend = campaigns.reduce((acc, c) => acc + (c.spend_usd || 0), 0);
  const issueCount = campaigns.filter((c) => c.has_rule_violation).length;

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
              <span style={{ fontSize: "24px" }}>🎯</span>
              <h1 style={{
                fontSize: "28px",
                fontWeight: "800",
                letterSpacing: "-0.02em",
                margin: 0,
                background: "linear-gradient(180deg, #ffffff 0%, #94a3b8 100%)",
                WebkitBackgroundClip: "text",
                WebkitTextFillColor: "transparent",
              }}>
                All Live Ad Campaigns & Revenue Attribution
              </h1>
            </div>
            <p style={{ fontSize: "14px", color: "#94a3b8", margin: 0 }}>
              Master table showing all active, healthy, and flagged ad campaigns with live inbound revenue generation.
            </p>
          </div>

          <div style={{ display: "flex", gap: "12px" }}>
            <div style={{
              background: "rgba(15, 23, 42, 0.8)",
              border: "1px solid rgba(255, 255, 255, 0.08)",
              borderRadius: "10px",
              padding: "8px 16px",
              fontSize: "12px",
            }}>
              <span style={{ color: "#94a3b8" }}>Total Attributed Revenue: </span>
              <strong style={{ color: campaigns.some(c => c.tracked && c.inbound_sales_usd != null) ? "#10b981" : "#94a3b8", fontSize: "14px" }}>
                {campaigns.some(c => c.tracked && c.inbound_sales_usd != null) ? `$${totalRevenue.toLocaleString()}` : "Not tracked"}
              </strong>
            </div>
          </div>
        </div>

        {/* Filter Controls */}
        <div style={{ display: "flex", gap: "8px", marginBottom: "20px" }}>
          {[
            { id: "all", label: `All Campaigns (${campaigns.length})` },
            { id: "healthy", label: `✓ Healthy & Live Serving (${campaigns.length - issueCount})` },
            { id: "issues", label: `⚠️ With Policy Issues (${issueCount})` },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => setFilter(tab.id)}
              style={{
                padding: "8px 16px",
                borderRadius: "8px",
                fontSize: "12px",
                fontWeight: "600",
                background: filter === tab.id ? "rgba(99, 102, 241, 0.25)" : "rgba(15, 23, 42, 0.6)",
                color: filter === tab.id ? "#818cf8" : "#94a3b8",
                border: filter === tab.id ? "1px solid rgba(99, 102, 241, 0.4)" : "1px solid rgba(255, 255, 255, 0.06)",
                cursor: "pointer",
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* Master Table */}
        <div style={{
          background: "rgba(15, 23, 42, 0.75)",
          backdropFilter: "blur(20px)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "16px",
          overflow: "hidden",
          boxShadow: "0 15px 35px rgba(0, 0, 0, 0.3)",
        }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.08)", background: "rgba(8, 12, 20, 0.4)", color: "#64748b", textAlign: "left" }}>
                <th style={{ padding: "14px 18px" }}>Campaign Headline & ID</th>
                <th style={{ padding: "14px 18px" }}>Client</th>
                <th style={{ padding: "14px 18px" }}>Platform & Handle</th>
                <th style={{ padding: "14px 18px" }}>Health Status</th>
                <th style={{ padding: "14px 18px" }}>Live Revenue</th>
                <th style={{ padding: "14px 18px" }}>Spend</th>
                <th style={{ padding: "14px 18px" }}>ROAS</th>
                <th style={{ padding: "14px 18px" }}>Impressions</th>
                <th style={{ padding: "14px 18px", textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredCampaigns.length === 0 ? (
                <tr>
                  <td colSpan={9} style={{ padding: "48px 18px", textAlign: "center", color: "#64748b" }}>
                    <div style={{ fontSize: "16px", fontWeight: "600", color: "#cbd5e1", marginBottom: "6px" }}>
                      No campaigns launched yet
                    </div>
                    <div style={{ fontSize: "13px", color: "#64748b" }}>
                      Upload and approve ad batches on the Home workspace to launch campaigns across platforms.
                    </div>
                  </td>
                </tr>
              ) : (
                filteredCampaigns.map((c) => (
                  <tr key={c.ad_id} style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.04)" }}>
                    <td style={{ padding: "14px 18px" }}>
                      <div style={{ fontWeight: "700", color: "#f8fafc", marginBottom: "2px" }}>
                        "{c.headline}"
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                        <code style={{ fontSize: "11px", color: "#64748b" }}>{c.ad_id}</code>
                        {(c.is_sample || c.ad_id?.startsWith("demo-")) && (
                          <span
                            style={{
                              background: "rgba(234, 179, 8, 0.2)",
                              color: "#facc15",
                              border: "1px solid rgba(234, 179, 8, 0.4)",
                              borderRadius: "4px",
                              padding: "1px 5px",
                              fontSize: "10px",
                              fontWeight: "bold",
                              letterSpacing: "0.05em",
                            }}
                          >
                            [SAMPLE]
                          </span>
                        )}
                      </div>
                    </td>

                  <td style={{ padding: "14px 18px", fontWeight: "600", color: "#cbd5e1" }}>
                    {c.client_name}
                  </td>

                  <td style={{ padding: "14px 18px" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                      <span style={{ padding: "2px 6px", background: "rgba(99, 102, 241, 0.15)", color: "#818cf8", borderRadius: "4px", fontSize: "11px", fontWeight: "600" }}>
                        {c.platform}
                      </span>
                      <span style={{ fontSize: "12px", color: "#94a3b8" }}>{c.handle}</span>
                    </div>
                  </td>

                  <td style={{ padding: "14px 18px" }}>
                    {c.delivery_status === "blocked_connector_not_configured" ? (
                      <span style={{ padding: "3px 8px", background: "rgba(148, 163, 184, 0.15)", color: "#94a3b8", borderRadius: "6px", fontSize: "11px", fontWeight: "700" }}>
                        🔒 Blocked (Connector Unconfigured)
                      </span>
                    ) : c.has_rule_violation ? (
                      <span style={{ padding: "3px 8px", background: "rgba(239, 68, 68, 0.15)", color: "#f87171", borderRadius: "6px", fontSize: "11px", fontWeight: "700" }}>
                        ⚠️ Policy Flagged
                      </span>
                    ) : c.delivery_status === "active_live" ? (
                      <span style={{ padding: "3px 8px", background: "rgba(16, 185, 129, 0.15)", color: "#34d399", borderRadius: "6px", fontSize: "11px", fontWeight: "700" }}>
                        ● Serving Live
                      </span>
                    ) : (
                      <span style={{ padding: "3px 8px", background: "rgba(100, 116, 139, 0.15)", color: "#64748b", borderRadius: "6px", fontSize: "11px", fontWeight: "600" }}>
                        {c.delivery_status}
                      </span>
                    )}
                  </td>

                  <td style={{ padding: "14px 18px", fontWeight: "700", color: c.tracked && c.inbound_sales_usd != null ? "#10b981" : "#64748b", fontSize: "14px" }}>
                    {c.tracked && c.inbound_sales_usd != null ? `$${c.inbound_sales_usd.toLocaleString()}` : "Not tracked"}
                  </td>

                  <td style={{ padding: "14px 18px", color: c.tracked && c.spend_usd != null ? "#f8fafc" : "#64748b" }}>
                    {c.tracked && c.spend_usd != null ? `$${c.spend_usd}` : "Not tracked"}
                  </td>

                  <td style={{ padding: "14px 18px", fontWeight: "600", color: c.tracked && c.roas != null ? "#818cf8" : "#64748b" }}>
                    {c.tracked && c.roas != null ? `${c.roas}x` : "Not tracked"}
                  </td>

                  <td style={{ padding: "14px 18px", color: c.tracked && c.impressions != null ? "#94a3b8" : "#64748b" }}>
                    {c.tracked && c.impressions != null ? c.impressions.toLocaleString() : "Not tracked"}
                  </td>

                  <td style={{ padding: "14px 18px", textAlign: "right" }}>
                    <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
                      <button
                        onClick={() => setPreviewAd(c)}
                        style={{
                          padding: "5px 10px",
                          background: "rgba(255, 255, 255, 0.08)",
                          border: "1px solid rgba(255, 255, 255, 0.1)",
                          borderRadius: "6px",
                          color: "#ffffff",
                          fontSize: "11px",
                          fontWeight: "600",
                          cursor: "pointer",
                        }}
                      >
                        👁️ Preview
                      </button>

                      {c.has_rule_violation && (
                        <Link
                          href={`/remediation?ad_id=${c.ad_id}`}
                          style={{
                            padding: "5px 10px",
                            background: "rgba(239, 68, 68, 0.2)",
                            border: "1px solid rgba(239, 68, 68, 0.4)",
                            borderRadius: "6px",
                            color: "#fca5a5",
                            fontSize: "11px",
                            fontWeight: "700",
                            textDecoration: "none",
                          }}
                        >
                          Fix Issue ➔
                        </Link>
                      )}
                    </div>
                  </td>
                </tr>
              )))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Photorealistic Feed Preview Modal */}
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
    </div>
  );
}
