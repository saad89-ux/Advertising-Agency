"use client";

import React from "react";

/**
 * High-performance, standout visual charts for the Glass Droom design system.
 * Built with pure SVG and React with luminous glassmorphism styling.
 */

export function VerdictDonutChart({ data = {}, size = 200 }) {
  const ready = data.ready || 0;
  const fixNeeded = (data["fix needed"] || data.fix_needed || 0) + (data["needs a person"] || data.needs_person || 0);
  const rejected = data.rejected || 0;
  const approved = data.approved || 0;
  const sent = data.sent || 0;

  const categories = [
    { key: "ready", label: "Ready to Approve", color: "#10b981", glow: "rgba(16, 185, 129, 0.4)", value: ready },
    { key: "needs_review", label: "Human Review", color: "#f59e0b", glow: "rgba(245, 158, 11, 0.4)", value: fixNeeded },
    { key: "rejected", label: "Hard Blocked", color: "#f43f5e", glow: "rgba(244, 63, 94, 0.4)", value: rejected },
    { key: "approved", label: "Approved", color: "#3b82f6", glow: "rgba(59, 130, 246, 0.4)", value: approved },
    { key: "sent", label: "Live Dispatched", color: "#a855f7", glow: "rgba(168, 85, 247, 0.4)", value: sent },
  ];

  const total = categories.reduce((sum, item) => sum + item.value, 0) || 1;
  const radius = size / 2 - 24;
  const circumference = 2 * Math.PI * radius;
  let accumulatedOffset = 0;

  return (
    <div style={{ display: "flex", alignItems: "center", gap: "28px", flexWrap: "wrap" }}>
      <div style={{ position: "relative", width: size, height: size, flexShrink: 0 }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
          {/* Subtle Ambient Ring */}
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="transparent"
            stroke="rgba(255, 255, 255, 0.06)"
            strokeWidth="16"
          />
          {categories.map((cat) => {
            if (cat.value === 0) return null;
            const strokeDasharray = `${(cat.value / total) * circumference} ${circumference}`;
            const strokeDashoffset = -accumulatedOffset;
            accumulatedOffset += (cat.value / total) * circumference;

            return (
              <circle
                key={cat.key}
                cx={size / 2}
                cy={size / 2}
                r={radius}
                fill="transparent"
                stroke={cat.color}
                strokeWidth="16"
                strokeDasharray={strokeDasharray}
                strokeDashoffset={strokeDashoffset}
                strokeLinecap="round"
                transform={`rotate(-90 ${size / 2} ${size / 2})`}
                style={{
                  transition: "stroke-dashoffset 0.6s cubic-bezier(0.4, 0, 0.2, 1)",
                  filter: `drop-shadow(0 0 6px ${cat.glow})`,
                }}
              />
            );
          })}
        </svg>

        {/* Center Readout Capsule */}
        <div style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          pointerEvents: "none",
        }}>
          <span style={{
            fontSize: "28px",
            fontWeight: "900",
            color: "#f8fafc",
            letterSpacing: "-0.02em",
            textShadow: "0 0 12px rgba(255, 255, 255, 0.3)",
          }}>
            {total === 1 && categories.every(c => c.value === 0) ? 0 : total}
          </span>
          <span style={{ fontSize: "10px", color: "#94a3b8", textTransform: "uppercase", fontWeight: "700", letterSpacing: "0.08em" }}>
            Total Ads
          </span>
        </div>
      </div>

      {/* Luminous Legend List */}
      <div style={{ display: "flex", flexDirection: "column", gap: "10px", flex: 1, minWidth: "160px" }}>
        {categories.map((cat) => (
          <div
            key={cat.key}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "12px",
              fontSize: "12px",
              padding: "4px 8px",
              borderRadius: "6px",
              background: cat.value > 0 ? "rgba(255, 255, 255, 0.03)" : "transparent",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <span style={{
                width: "9px",
                height: "9px",
                borderRadius: "3px",
                background: cat.color,
                boxShadow: `0 0 8px ${cat.glow}`,
              }} />
              <span style={{ color: "#cbd5e1", fontWeight: "500" }}>{cat.label}</span>
            </div>
            <div style={{
              fontSize: "12px",
              fontWeight: "700",
              color: cat.value > 0 ? "#f8fafc" : "#64748b",
              background: "rgba(255, 255, 255, 0.05)",
              padding: "2px 7px",
              borderRadius: "4px",
            }}>
              {cat.value}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function FindingsBarChart({ findings = [] }) {
  const counts = {
    "UTM Compliance": 0,
    "Platform Text Limits": 0,
    "Offer / Landing Page": 0,
    "Creative Dimensions": 0,
    "Wording / Policy": 0,
  };

  findings.forEach((f) => {
    const type = (f.check_type || "").toLowerCase();
    const msg = (f.message || "").toLowerCase();
    if (type.includes("utm") || msg.includes("utm")) counts["UTM Compliance"] += 1;
    else if (type.includes("limit") || msg.includes("character") || msg.includes("length")) counts["Platform Text Limits"] += 1;
    else if (type.includes("page") || type.includes("offer") || msg.includes("price") || msg.includes("discount")) counts["Offer / Landing Page"] += 1;
    else if (type.includes("ratio") || type.includes("size") || type.includes("creative")) counts["Creative Dimensions"] += 1;
    else counts["Wording / Policy"] += 1;
  });

  const categories = [
    { label: "UTM Compliance", count: counts["UTM Compliance"], color: "#38bdf8" },
    { label: "Platform Text Limits", count: counts["Platform Text Limits"], color: "#f59e0b" },
    { label: "Offer / Landing Page", count: counts["Offer / Landing Page"], color: "#f43f5e" },
    { label: "Creative Dimensions", count: counts["Creative Dimensions"], color: "#a855f7" },
    { label: "Wording / Policy", count: counts["Wording / Policy"], color: "#ec4899" },
  ];

  const maxCount = Math.max(...categories.map((c) => c.count), 5);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
      {categories.map((cat) => {
        const pct = Math.round((cat.count / maxCount) * 100);
        return (
          <div key={cat.label}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", marginBottom: "6px" }}>
              <span style={{ color: "#cbd5e1", fontWeight: "500" }}>{cat.label}</span>
              <span style={{
                color: cat.count > 0 ? cat.color : "#64748b",
                fontWeight: "700",
                fontSize: "11px",
              }}>
                {cat.count} {cat.count === 1 ? "flag" : "flags"}
              </span>
            </div>
            <div style={{
              height: "8px",
              width: "100%",
              background: "rgba(255, 255, 255, 0.05)",
              borderRadius: "4px",
              overflow: "hidden",
              border: "1px solid rgba(255, 255, 255, 0.05)",
            }}>
              <div
                style={{
                  height: "100%",
                  width: `${pct}%`,
                  background: `linear-gradient(90deg, ${cat.color}88 0%, ${cat.color} 100%)`,
                  borderRadius: "4px",
                  boxShadow: cat.count > 0 ? `0 0 10px ${cat.color}66` : "none",
                  transition: "width 0.8s cubic-bezier(0.4, 0, 0.2, 1)",
                }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function ClientVolumeBarChart({ batches = [] }) {
  // Aggregate batch and ad counts by client
  const clientStats = {};
  batches.forEach((b) => {
    const client = (b.client_id || "Unknown").toUpperCase();
    if (!clientStats[client]) {
      clientStats[client] = { totalAds: 0, batches: 0, readyAds: 0 };
    }
    clientStats[client].batches += 1;
    clientStats[client].totalAds += b.row_count || b.ad_count || 4;
  });

  const clients = Object.entries(clientStats);
  const maxAds = Math.max(...clients.map(([_, s]) => s.totalAds), 10);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
      <div style={{
        display: "grid",
        gridTemplateColumns: `repeat(${Math.max(clients.length, 1)}, 1fr)`,
        gap: "14px",
        alignItems: "flex-end",
        height: "140px",
        padding: "10px 0",
        borderBottom: "1px solid rgba(255, 255, 255, 0.08)",
      }}>
        {clients.map(([name, stat]) => {
          const heightPct = Math.round((stat.totalAds / maxAds) * 100);
          return (
            <div key={name} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "8px", height: "100%", justifyContent: "flex-end" }}>
              <span style={{ fontSize: "11px", fontWeight: "700", color: "#38bdf8" }}>{stat.totalAds} ads</span>
              <div
                style={{
                  width: "100%",
                  maxWidth: "48px",
                  height: `${heightPct}%`,
                  minHeight: "12px",
                  background: "linear-gradient(180deg, #38bdf8 0%, #3b82f6 100%)",
                  borderRadius: "6px 6px 2px 2px",
                  boxShadow: "0 0 16px rgba(56, 189, 248, 0.3)",
                  transition: "height 0.6s ease",
                }}
              />
              <span style={{ fontSize: "10px", color: "#94a3b8", fontWeight: "600", textAlign: "center" }}>
                {name}
              </span>
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", color: "#64748b" }}>
        <span>Volume distribution across managed agency clients</span>
        <span>{batches.length} total batches</span>
      </div>
    </div>
  );
}

export function ReleaseGateScorecard({ metrics = {} }) {
  const readyPercent = metrics.ready_percent || 0;
  const passed = readyPercent >= 100;

  return (
    <div style={{
      padding: "20px",
      borderRadius: "12px",
      background: passed ? "rgba(16, 185, 129, 0.08)" : "rgba(245, 158, 11, 0.08)",
      border: `1px solid ${passed ? "rgba(16, 185, 129, 0.25)" : "rgba(245, 158, 11, 0.25)"}`,
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      flexWrap: "wrap",
      gap: "16px",
    }}>
      <div>
        <div style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "6px",
          padding: "3px 8px",
          borderRadius: "20px",
          background: passed ? "rgba(16, 185, 129, 0.2)" : "rgba(245, 158, 11, 0.2)",
          color: passed ? "#34d399" : "#fbbf24",
          fontSize: "11px",
          fontWeight: "700",
          letterSpacing: "0.05em",
          textTransform: "uppercase",
          marginBottom: "8px",
        }}>
          <span style={{ width: "6px", height: "6px", borderRadius: "50%", background: passed ? "#10b981" : "#f59e0b" }} />
          <span>{passed ? "Release Gate Open" : "Human Approval Required"}</span>
        </div>
        <div style={{ fontSize: "18px", fontWeight: "800", color: "#f8fafc" }}>
          {readyPercent}% Launch Readiness
        </div>
        <div style={{ fontSize: "12px", color: "#94a3b8", marginTop: "2px" }}>
          {passed
            ? "100% of rows meet mechanical, offer & policy compliance. Safe to dispatch."
            : "Review flagged rows and authorize dismissals or edits prior to production deployment."}
        </div>
      </div>

      <div style={{ textAlign: "right" }}>
        <div style={{ fontSize: "28px", fontWeight: "900", color: passed ? "#34d399" : "#fbbf24" }}>
          {readyPercent}%
        </div>
        <div style={{ fontSize: "10px", color: "#64748b", textTransform: "uppercase", fontWeight: "700" }}>
          Target: 100%
        </div>
      </div>
    </div>
  );
}
