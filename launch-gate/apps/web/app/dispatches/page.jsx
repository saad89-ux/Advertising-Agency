"use client";

import React, { useState, useEffect } from "react";
import SocialFeedModal from "../../components/SocialFeedModal";

export default function DispatchesPage({ apiBaseUrl = "http://localhost:8000" }) {
  const [tracesData, setTracesData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [previewAd, setPreviewAd] = useState(null);

  // 60-second login / dispatch countdown simulation
  const [countdown, setCountdown] = useState(60);
  const [mcpStep, setMcpStep] = useState(0);

  const mcpSteps = [
    { label: "1. Resolving Platform Handles & Vault Credentials", duration: 10 },
    { label: "2. Executing MCP Instagram Graph OAuth Handshake", duration: 20 },
    { label: "3. Injecting Meta Ads Creative Container & Pixel", duration: 35 },
    { label: "4. Verifying TikTok Commercial Audio Rights Library", duration: 48 },
    { label: "5. Dispatched Across All Social Handles & Live Serving Active", duration: 60 },
  ];

  useEffect(() => {
    const timer = setInterval(() => {
      setCountdown((prev) => {
        if (prev <= 1) {
          clearInterval(timer);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const elapsed = 60 - countdown;
    if (elapsed < 12) setMcpStep(0);
    else if (elapsed < 24) setMcpStep(1);
    else if (elapsed < 38) setMcpStep(2);
    else if (elapsed < 52) setMcpStep(3);
    else setMcpStep(4);
  }, [countdown]);

  const fetchTraces = async () => {
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await fetch(`${apiBaseUrl}/campaign-flow/mcp-traces`, { headers });
      if (res.ok) {
        const data = await res.json();
        setTracesData(data);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTraces();
  }, [apiBaseUrl]);

  const clients = [
    {
      client_id: "brightleaf",
      client_name: "BrightLeaf Wellness",
      campaign_id: "BL-CAMP-7701",
      batch_id: "batch_bl_summer_101",
      active_platforms: [
        {
          platform: "instagram",
          name: "Instagram",
          handle: "@brightleaf_wellness",
          ad_id: "IG-BL-88491",
          status: "Live & Serving",
          statusColor: "#10b981",
          headline: "Organic Radiance Daily Serum",
          spend: "$412.00",
          orders: 38,
          previewUrl: "https://www.instagram.com/brightleaf_wellness/p/ad_88491",
        },
        {
          platform: "facebook",
          name: "Meta / Facebook",
          handle: "act_8492049102",
          ad_id: "FB-BL-99201",
          status: "Live & Serving",
          statusColor: "#10b981",
          headline: "Clean Skincare Morning Ritual",
          spend: "$340.00",
          orders: 29,
          previewUrl: "https://www.facebook.com/brightleaf_wellness/posts/99201",
        },
        {
          platform: "tiktok",
          name: "TikTok",
          handle: "@brightleaf_tok",
          ad_id: "TT-BL-33120",
          status: "Live & Serving",
          statusColor: "#10b981",
          headline: "Natural Skin Glow in 7 Days",
          spend: "$280.00",
          orders: 24,
          previewUrl: "https://www.tiktok.com/@brightleaf_tok/video/33120",
        },
      ],
    },
    {
      client_id: "calmleaf",
      client_name: "CalmLeaf Extracts",
      campaign_id: "CL-CAMP-8802",
      batch_id: "batch_cl_relax_102",
      active_platforms: [
        {
          platform: "instagram",
          name: "Instagram",
          handle: "@calmleaf_official",
          ad_id: "IG-CL-77192",
          status: "Live & Serving",
          statusColor: "#10b981",
          headline: "Evening Relaxation Herbal Drops",
          spend: "$310.00",
          orders: 26,
          previewUrl: "https://www.instagram.com/calmleaf_official/p/ad_77192",
        },
        {
          platform: "tiktok",
          name: "TikTok",
          handle: "@calmleaf_tok",
          ad_id: "TT-CL-22019",
          status: "Under Review",
          statusColor: "#f59e0b",
          headline: "Wind Down Peacefully Tonight",
          spend: "$80.00",
          orders: 4,
          previewUrl: "https://www.tiktok.com/@calmleaf_tok/video/22019",
        },
        {
          platform: "x",
          name: "X / Twitter",
          handle: "@calmleaf_x",
          ad_id: "X-CL-55102",
          status: "Live & Serving",
          statusColor: "#10b981",
          headline: "Deep Calm Without The Fog",
          spend: "$195.00",
          orders: 14,
          previewUrl: "https://x.com/calmleaf_x/status/55102",
        },
      ],
    },
    {
      client_id: "oakandhome",
      client_name: "Oak & Home Design",
      campaign_id: "OH-CAMP-9903",
      batch_id: "batch_oh_fall_103",
      active_platforms: [
        {
          platform: "facebook",
          name: "Meta / Facebook",
          handle: "act_9928172641",
          ad_id: "FB-OH-11029",
          status: "Live & Serving",
          statusColor: "#10b981",
          headline: "Handcrafted Artisan Dining Tables",
          spend: "$520.00",
          orders: 18,
          previewUrl: "https://www.facebook.com/oakandhome_living/posts/11029",
        },
        {
          platform: "instagram",
          name: "Instagram",
          handle: "@oakandhome_living",
          ad_id: "IG-OH-44910",
          status: "Live & Serving",
          statusColor: "#10b981",
          headline: "Solid Oak Living Room Sets",
          spend: "$410.00",
          orders: 15,
          previewUrl: "https://www.instagram.com/oakandhome_living/p/44910",
        },
      ],
    },
  ];

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
              Real-time monitoring of automated agents authenticating through MCP platform servers and serving live ads.
            </p>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
            <div style={{
              background: "rgba(16, 185, 129, 0.12)",
              border: "1px solid rgba(16, 185, 129, 0.3)",
              padding: "6px 14px",
              borderRadius: "999px",
              fontSize: "12px",
              fontWeight: "600",
              color: "#34d399",
              display: "flex",
              alignItems: "center",
              gap: "8px",
            }}>
              <span style={{ display: "inline-block", width: "8px", height: "8px", borderRadius: "50%", background: "#10b981", boxShadow: "0 0 10px #10b981" }}></span>
              5 MCP Servers Connected
            </div>
          </div>
        </div>

        {/* 1. MCP Automated Login & Execution Progress Bar */}
        <div style={{
          background: "rgba(15, 23, 42, 0.75)",
          backdropFilter: "blur(20px)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "16px",
          padding: "20px 24px",
          marginBottom: "28px",
          boxShadow: "0 10px 30px rgba(0, 0, 0, 0.3)",
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <span style={{ fontSize: "16px" }}>⚡</span>
              <span style={{ fontSize: "14px", fontWeight: "700", color: "#f8fafc" }}>
                Multi-Platform Automated Login & Injection Progress
              </span>
            </div>
            <span style={{ fontSize: "13px", fontWeight: "700", color: countdown > 0 ? "#818cf8" : "#10b981" }}>
              {countdown > 0 ? `${countdown}s remaining in automated cycle` : "✓ Automated Login & Dispatch Cycle Complete"}
            </span>
          </div>

          {/* Animated Bar */}
          <div style={{ width: "100%", height: "8px", background: "rgba(255, 255, 255, 0.08)", borderRadius: "999px", overflow: "hidden", marginBottom: "12px" }}>
            <div style={{
              width: `${((60 - countdown) / 60) * 100}%`,
              height: "100%",
              background: "linear-gradient(90deg, #6366f1 0%, #10b981 100%)",
              transition: "width 0.9s linear",
            }} />
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", color: "#94a3b8" }}>
            {mcpSteps.map((step, idx) => (
              <span key={idx} style={{ color: mcpStep >= idx ? "#cbd5e1" : "#475569", fontWeight: mcpStep === idx ? "700" : "500" }}>
                {step.label}
              </span>
            ))}
          </div>
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
              <span style={{ fontSize: "12px", color: "#94a3b8", marginLeft: "6px" }}>MCP Tool Calls & Live Handshakes Terminal</span>
            </div>
            <span style={{ fontSize: "11px", color: "#64748b" }}>Live SSE Stream Active</span>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "8px", fontSize: "12px", lineHeight: "1.6" }}>
            {(tracesData?.traces || []).map((t, idx) => (
              <div key={idx} style={{ display: "flex", gap: "12px", padding: "6px 8px", background: "rgba(255, 255, 255, 0.02)", borderRadius: "6px" }}>
                <span style={{ color: "#64748b" }}>[{t.timestamp}]</span>
                <span style={{ color: "#38bdf8", fontWeight: "600" }}>{t.server}</span>
                <span style={{ color: "#a78bfa" }}>→ {t.tool}</span>
                <span style={{ color: "#10b981" }}>{t.status}</span>
                <span style={{ color: "#f59e0b" }}>({t.duration_ms}ms)</span>
                <span style={{ color: "#94a3b8", flex: 1 }}>{t.payload_summary}</span>
              </div>
            ))}
          </div>
        </div>

        {/* 3. Client Multi-Platform Active Delivery Matrix */}
        <div>
          <h2 style={{ fontSize: "20px", fontWeight: "800", color: "#f8fafc", margin: "0 0 16px 0" }}>
            Live Platform Deployments by Client
          </h2>

          <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
            {clients.map((client) => (
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
                {/* Client Bar */}
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px", borderBottom: "1px solid rgba(255, 255, 255, 0.06)", paddingBottom: "12px" }}>
                  <div>
                    <span style={{ fontSize: "18px", fontWeight: "700", color: "#ffffff", marginRight: "12px" }}>
                      {client.client_name}
                    </span>
                    <span style={{ fontSize: "12px", padding: "2px 8px", background: "rgba(99, 102, 241, 0.15)", color: "#818cf8", borderRadius: "6px", fontWeight: "600" }}>
                      Trackable ID: {client.campaign_id}
                    </span>
                  </div>
                  <span style={{ fontSize: "12px", color: "#64748b" }}>
                    Batch: {client.batch_id}
                  </span>
                </div>

                {/* Platform Cards Grid */}
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: "16px" }}>
                  {client.active_platforms.map((p, idx) => (
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
                            {p.name}
                          </span>
                          <span style={{
                            fontSize: "11px",
                            padding: "2px 8px",
                            borderRadius: "999px",
                            background: p.status === "Live & Serving" ? "rgba(16, 185, 129, 0.15)" : "rgba(245, 158, 11, 0.15)",
                            color: p.statusColor,
                            fontWeight: "600",
                          }}>
                            ● {p.status}
                          </span>
                        </div>

                        <div style={{ fontSize: "12px", color: "#64748b", marginBottom: "4px" }}>
                          Handle: <span style={{ color: "#94a3b8", fontWeight: "600" }}>{p.handle}</span> • ID: <span style={{ color: "#94a3b8" }}>{p.ad_id}</span>
                        </div>
                        <div style={{ fontSize: "13px", fontWeight: "600", color: "#cbd5e1", marginBottom: "14px" }}>
                          "{p.headline}"
                        </div>
                      </div>

                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", borderTop: "1px solid rgba(255, 255, 255, 0.06)", paddingTop: "12px" }}>
                        <div style={{ fontSize: "12px" }}>
                          <span style={{ color: "#64748b" }}>Spend: </span>
                          <span style={{ color: "#f8fafc", fontWeight: "600" }}>{p.spend}</span>
                          <span style={{ color: "#64748b", marginLeft: "8px" }}>Orders: </span>
                          <span style={{ color: "#10b981", fontWeight: "600" }}>{p.orders}</span>
                        </div>

                        <button
                          onClick={() => setPreviewAd({
                            ad_id: p.ad_id,
                            platform: p.platform,
                            headline: p.headline,
                            handle: p.handle,
                            client_id: client.client_id,
                          })}
                          style={{
                            padding: "6px 12px",
                            background: "rgba(255, 255, 255, 0.08)",
                            border: "1px solid rgba(255, 255, 255, 0.12)",
                            borderRadius: "6px",
                            color: "#ffffff",
                            fontSize: "11px",
                            fontWeight: "600",
                            cursor: "pointer",
                          }}
                        >
                          👁️ Feed Preview
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Photorealistic Feed Preview Modal */}
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
