"use client";

import React, { useEffect } from "react";

/**
 * Modern Glass Droom Toast & Notification Banner.
 * Replaces un-styled, browser-native alert() popups with a responsive, polished UI.
 */
export default function GlassToast({
  message,
  type = "error", // 'error' | 'success' | 'warning' | 'info'
  onClose,
  duration = 5000,
}) {
  useEffect(() => {
    if (!message || duration <= 0) return;
    const timer = setTimeout(() => {
      if (onClose) onClose();
    }, duration);
    return () => clearTimeout(timer);
  }, [message, duration, onClose]);

  if (!message) return null;

  const typeConfig = {
    error: {
      border: "rgba(239, 68, 68, 0.4)",
      bg: "rgba(239, 68, 68, 0.12)",
      text: "#fca5a5",
      icon: "✕",
      iconBg: "rgba(239, 68, 68, 0.25)",
      glow: "0 8px 30px rgba(239, 68, 68, 0.25)",
    },
    success: {
      border: "rgba(16, 185, 129, 0.4)",
      bg: "rgba(16, 185, 129, 0.12)",
      text: "#6ee7b7",
      icon: "✓",
      iconBg: "rgba(16, 185, 129, 0.25)",
      glow: "0 8px 30px rgba(16, 185, 129, 0.25)",
    },
    warning: {
      border: "rgba(245, 158, 11, 0.4)",
      bg: "rgba(245, 158, 11, 0.12)",
      text: "#fcd34d",
      icon: "!",
      iconBg: "rgba(245, 158, 11, 0.25)",
      glow: "0 8px 30px rgba(245, 158, 11, 0.25)",
    },
    info: {
      border: "rgba(99, 102, 241, 0.4)",
      bg: "rgba(99, 102, 241, 0.12)",
      text: "#a5b4fc",
      icon: "ℹ",
      iconBg: "rgba(99, 102, 241, 0.25)",
      glow: "0 8px 30px rgba(99, 102, 241, 0.25)",
    },
  }[type] || typeConfig.error;

  return (
    <div
      style={{
        position: "fixed",
        bottom: "28px",
        right: "28px",
        zIndex: 9999,
        maxWidth: "460px",
        width: "calc(100vw - 56px)",
        background: "rgba(11, 17, 29, 0.92)",
        backdropFilter: "blur(24px) saturate(180%)",
        WebkitBackdropFilter: "blur(24px) saturate(180%)",
        border: `1px solid ${typeConfig.border}`,
        borderRadius: "14px",
        padding: "14px 18px",
        boxShadow: typeConfig.glow,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "14px",
        animation: "toastSlideIn 0.28s cubic-bezier(0.16, 1, 0.3, 1)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "12px", flex: 1 }}>
        <div
          style={{
            width: "28px",
            height: "28px",
            borderRadius: "50%",
            background: typeConfig.iconBg,
            color: typeConfig.text,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontWeight: "800",
            fontSize: "13px",
            flexShrink: 0,
          }}
        >
          {typeConfig.icon}
        </div>
        <div style={{ fontSize: "13px", color: "#f8fafc", lineHeight: "1.45", wordBreak: "break-word" }}>
          {message}
        </div>
      </div>

      {onClose && (
        <button
          onClick={onClose}
          style={{
            background: "transparent",
            border: "none",
            color: "#94a3b8",
            cursor: "pointer",
            fontSize: "16px",
            padding: "4px 8px",
            borderRadius: "6px",
            lineHeight: 1,
            transition: "all 0.15s ease",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.color = "#ffffff")}
          onMouseLeave={(e) => (e.currentTarget.style.color = "#94a3b8")}
          title="Dismiss"
        >
          ✕
        </button>
      )}

      <style jsx>{`
        @keyframes toastSlideIn {
          from {
            transform: translateY(20px) scale(0.96);
            opacity: 0;
          }
          to {
            transform: translateY(0) scale(1);
            opacity: 1;
          }
        }
      `}</style>
    </div>
  );
}
