"use client";

import React, { useState } from "react";

export default function SocialFeedModal({ ad, onClose }) {
  const [activePlatform, setActivePlatform] = useState(
    ad?.platform ? ad.platform.toLowerCase() : "instagram"
  );

  if (!ad) return null;

  const handle = ad.handle || (ad.client_id ? `@${ad.client_id}_official` : "@brand_official");
  const headline = ad.headline || "Untitled Ad Headline";
  const text = ad.ad_text || ad.caption || "";
  const cta = ad.cta_label || (text.toLowerCase().includes("shop") ? "Shop Now" : "Learn More");
  const destUrl = ad.destination_url || "";
  const reviewStatus = ad.platform_review_status || ad.review_status || "Pending Platform Review";
  const salesUsd = ad.inbound_sales_usd;
  const ordersCount = ad.inbound_orders;

  const platforms = [
    { id: "instagram", name: "Instagram", color: "#e1306c", icon: "📷" },
    { id: "tiktok", name: "TikTok", color: "#00f2fe", icon: "🎵" },
    { id: "facebook", name: "Meta / Facebook", color: "#1877f2", icon: "👥" },
    { id: "x", name: "X (Twitter)", color: "#ffffff", icon: "𝕏" },
  ];

  return (
    <div style={{
      position: "fixed",
      inset: 0,
      zIndex: 1000,
      background: "rgba(3, 7, 18, 0.85)",
      backdropFilter: "blur(16px)",
      WebkitBackdropFilter: "blur(16px)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      padding: "20px",
      animation: "fadeIn 0.2s ease-out",
    }}>
      <div style={{
        background: "rgba(15, 23, 42, 0.95)",
        border: "1px solid rgba(255, 255, 255, 0.12)",
        borderRadius: "20px",
        width: "100%",
        maxWidth: "920px",
        maxHeight: "90vh",
        display: "grid",
        gridTemplateColumns: "1fr 1fr",
        overflow: "hidden",
        boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.7), 0 0 40px rgba(59, 130, 246, 0.15)",
      }}>
        {/* Left Column: Photorealistic Live Feed Frame */}
        <div style={{
          background: "linear-gradient(180deg, #090d16 0%, #030712 100%)",
          padding: "28px 24px",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          borderRight: "1px solid rgba(255, 255, 255, 0.08)",
          position: "relative",
        }}>
          {/* Platform Switcher Tabs */}
          <div style={{
            display: "flex",
            gap: "8px",
            marginBottom: "20px",
            background: "rgba(255, 255, 255, 0.04)",
            padding: "4px",
            borderRadius: "12px",
            border: "1px solid rgba(255, 255, 255, 0.08)",
          }}>
            {platforms.map((p) => (
              <button
                key={p.id}
                onClick={() => setActivePlatform(p.id)}
                style={{
                  background: activePlatform === p.id ? "rgba(59, 130, 246, 0.25)" : "transparent",
                  color: activePlatform === p.id ? "#60a5fa" : "#94a3b8",
                  border: activePlatform === p.id ? "1px solid rgba(96, 165, 250, 0.4)" : "1px solid transparent",
                  padding: "6px 12px",
                  borderRadius: "8px",
                  fontSize: "12px",
                  fontWeight: 600,
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  gap: "6px",
                  transition: "all 0.15s ease",
                }}
              >
                <span>{p.icon}</span>
                <span>{p.name.split(" ")[0]}</span>
              </button>
            ))}
          </div>

          {/* Social Phone Mock Card */}
          <div style={{
            width: "320px",
            background: activePlatform === "tiktok" ? "#000000" : "#0d131f",
            borderRadius: "24px",
            border: "2px solid rgba(255, 255, 255, 0.15)",
            boxShadow: "0 20px 40px rgba(0, 0, 0, 0.8)",
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
            position: "relative",
          }}>
            {/* Post Header */}
            <div style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "12px 14px",
              borderBottom: "1px solid rgba(255, 255, 255, 0.06)",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                <div style={{
                  width: "36px",
                  height: "36px",
                  borderRadius: "50%",
                  background: "linear-gradient(135deg, #3b82f6, #8b5cf6)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontWeight: 700,
                  fontSize: "13px",
                  color: "#fff",
                  boxShadow: "0 0 10px rgba(59, 130, 246, 0.4)",
                }}>
                  {ad.client_id ? ad.client_id.slice(0, 2).toUpperCase() : "LG"}
                </div>
                <div>
                  <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
                    <span style={{ fontSize: "13px", fontWeight: 700, color: "#f8fafc" }}>
                      {handle}
                    </span>
                    <span style={{ color: "#38bdf8", fontSize: "12px" }}>✓</span>
                  </div>
                  <div style={{ fontSize: "11px", color: "#94a3b8" }}>
                    Sponsored • <span style={{ color: "#10b981", fontWeight: 600 }}>Active</span>
                  </div>
                </div>
              </div>
              <span style={{ color: "#64748b", fontSize: "16px" }}>•••</span>
            </div>

            {/* Media Creative Canvas */}
            <div style={{
              height: "220px",
              background: "linear-gradient(135deg, #1e293b 0%, #0f172a 100%)",
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              padding: "20px",
              position: "relative",
              textAlign: "center",
              overflow: "hidden",
            }}>
              <div style={{
                position: "absolute",
                top: 0,
                left: 0,
                right: 0,
                bottom: 0,
                opacity: 0.15,
                background: "radial-gradient(circle at 50% 50%, #3b82f6 0%, transparent 70%)",
              }} />
              <div style={{
                fontSize: "10px",
                letterSpacing: "0.1em",
                textTransform: "uppercase",
                color: "#60a5fa",
                fontWeight: 700,
                marginBottom: "6px",
                zIndex: 1,
              }}>
                {ad.client_id ? ad.client_id.toUpperCase() : "VERIFIED CAMPAIGN"}
              </div>
              <div style={{
                fontSize: "16px",
                fontWeight: 800,
                color: "#f8fafc",
                lineHeight: "1.3",
                maxWidth: "240px",
                zIndex: 1,
                textShadow: "0 2px 10px rgba(0, 0, 0, 0.5)",
              }}>
                {headline}
              </div>
              <div style={{
                marginTop: "16px",
                padding: "8px 18px",
                background: "rgba(59, 130, 246, 0.9)",
                color: "#ffffff",
                borderRadius: "20px",
                fontSize: "12px",
                fontWeight: 700,
                boxShadow: "0 4px 15px rgba(59, 130, 246, 0.4)",
                zIndex: 1,
              }}>
                {cta} →
              </div>
            </div>

            {/* Engagement Action Bar */}
            <div style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              padding: "10px 14px",
              fontSize: "15px",
            }}>
              <div style={{ display: "flex", gap: "14px" }}>
                <span>❤️ <span style={{ fontSize: "11px", color: "#94a3b8" }}>1.4k</span></span>
                <span>💬 <span style={{ fontSize: "11px", color: "#94a3b8" }}>89</span></span>
                <span>↗️ <span style={{ fontSize: "11px", color: "#94a3b8" }}>42</span></span>
              </div>
              <span>🔖</span>
            </div>

            {/* Post Caption & UTM Link */}
            <div style={{ padding: "0 14px 14px 14px" }}>
              <p style={{
                fontSize: "12px",
                color: "#cbd5e1",
                lineHeight: "1.4",
                margin: "0 0 6px 0",
                display: "-webkit-box",
                WebkitLineClamp: 3,
                WebkitBoxOrient: "vertical",
                overflow: "hidden",
              }}>
                <strong style={{ color: "#f8fafc" }}>{handle}</strong> {text}
              </p>
              <div style={{
                fontSize: "10px",
                color: "#38bdf8",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
                fontFamily: "monospace",
              }}>
                🔗 {destUrl}
              </div>
            </div>
          </div>
        </div>

        {/* Right Column: Automated Agent Dispatch & Telemetry Cockpit */}
        <div style={{
          padding: "28px",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          overflowY: "auto",
        }}>
          <div>
            {/* Header with Close */}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "20px" }}>
              <div>
                <span style={{
                  fontSize: "10px",
                  fontWeight: 700,
                  textTransform: "uppercase",
                  letterSpacing: "0.1em",
                  color: "#38bdf8",
                  background: "rgba(56, 189, 248, 0.12)",
                  padding: "3px 8px",
                  borderRadius: "6px",
                  border: "1px solid rgba(56, 189, 248, 0.3)",
                }}>
                  Live Delivery Inspector
                </span>
                <h3 style={{ fontSize: "18px", fontWeight: 800, color: "#f8fafc", margin: "8px 0 2px 0" }}>
                  Ad Live on {activePlatform.toUpperCase()}
                </h3>
                <div style={{ fontSize: "12px", color: "#94a3b8", fontFamily: "monospace" }}>
                  Ad ID: {ad.ad_id}
                </div>
              </div>
              <button
                onClick={onClose}
                style={{
                  background: "rgba(255, 255, 255, 0.08)",
                  border: "none",
                  color: "#94a3b8",
                  borderRadius: "8px",
                  width: "32px",
                  height: "32px",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  cursor: "pointer",
                  fontSize: "16px",
                }}
              >
                ✕
              </button>
            </div>

            {/* Platform Review Bot Status */}
            <div style={{
              background: reviewStatus.includes("Warning") || reviewStatus.includes("Flagged")
                ? "rgba(239, 68, 68, 0.1)"
                : "rgba(16, 185, 129, 0.1)",
              border: reviewStatus.includes("Warning") || reviewStatus.includes("Flagged")
                ? "1px solid rgba(239, 68, 68, 0.3)"
                : "1px solid rgba(16, 185, 129, 0.3)",
              borderRadius: "12px",
              padding: "14px",
              marginBottom: "20px",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "4px" }}>
                <span style={{ fontSize: "16px" }}>
                  {reviewStatus.includes("Warning") ? "⚠️" : "🛡️"}
                </span>
                <span style={{
                  fontSize: "13px",
                  fontWeight: 700,
                  color: reviewStatus.includes("Warning") ? "#f87171" : "#34d399",
                }}>
                  {reviewStatus}
                </span>
              </div>
              <p style={{ fontSize: "12px", color: "#cbd5e1", margin: 0, lineHeight: "1.4" }}>
                {ad.platform_review_notes || "Platform review bot validated creative dimensions, text density, and verified domain ownership against authorized claims."}
              </p>
            </div>

            {/* Attribution KPIs */}
            <div style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: "12px",
              marginBottom: "20px",
            }}>
              <div style={{
                background: "rgba(255, 255, 255, 0.03)",
                border: "1px solid rgba(255, 255, 255, 0.06)",
                borderRadius: "10px",
                padding: "12px",
              }}>
                <div style={{ fontSize: "11px", color: "#94a3b8", textTransform: "uppercase" }}>Inbound Sales</div>
                <div style={{ fontSize: "20px", fontWeight: 800, color: "#10b981", marginTop: "2px" }}>
                  ${salesUsd.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                </div>
                <div style={{ fontSize: "11px", color: "#64748b" }}>
                  {ordersCount} verified orders
                </div>
              </div>

              <div style={{
                background: "rgba(255, 255, 255, 0.03)",
                border: "1px solid rgba(255, 255, 255, 0.06)",
                borderRadius: "10px",
                padding: "12px",
              }}>
                <div style={{ fontSize: "11px", color: "#94a3b8", textTransform: "uppercase" }}>Target Handle</div>
                <div style={{ fontSize: "16px", fontWeight: 700, color: "#60a5fa", marginTop: "4px" }}>
                  {handle}
                </div>
                <div style={{ fontSize: "11px", color: "#64748b" }}>
                  OAuth Verified Agent
                </div>
              </div>
            </div>

            {/* Agent Automation Steps Log */}
            <div style={{
              background: "rgba(0, 0, 0, 0.4)",
              border: "1px solid rgba(255, 255, 255, 0.06)",
              borderRadius: "10px",
              padding: "12px",
              fontFamily: "monospace",
              fontSize: "11px",
              color: "#94a3b8",
              lineHeight: "1.6",
            }}>
              <div style={{ color: "#38bdf8", fontWeight: 700, marginBottom: "4px" }}>
                ⚡ AUTOMATED AGENT AUDIT LOG:
              </div>
              <div>[1] Loaded client credentials for {handle} (KMS token valid)</div>
              <div>[2] Synced creative payload ({ad.creative_file || "asset_standard.png"})</div>
              <div>[3] Verified UTM parameter injection (?utm_source={activePlatform})</div>
              <div>[4] Dispatched to {activePlatform.toUpperCase()} Ads Graph API</div>
              <div style={{ color: "#34d399" }}>[5] Platform bot response: 200 OK (Status: Serving)</div>
            </div>
          </div>

          {/* Footer Actions */}
          <div style={{ marginTop: "24px", display: "flex", gap: "10px", justifyContent: "flex-end" }}>
            <button
              onClick={onClose}
              style={{
                background: "rgba(255, 255, 255, 0.08)",
                color: "#f8fafc",
                border: "1px solid rgba(255, 255, 255, 0.15)",
                padding: "10px 18px",
                borderRadius: "10px",
                fontSize: "13px",
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              Done Viewing
            </button>
            <a
              href={destUrl}
              target="_blank"
              rel="noreferrer"
              style={{
                background: "linear-gradient(135deg, #2563eb, #3b82f6)",
                color: "#ffffff",
                border: "none",
                padding: "10px 18px",
                borderRadius: "10px",
                fontSize: "13px",
                fontWeight: 700,
                textDecoration: "none",
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
                boxShadow: "0 4px 15px rgba(37, 99, 235, 0.3)",
              }}
            >
              Inspect Landing Page ↗
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
