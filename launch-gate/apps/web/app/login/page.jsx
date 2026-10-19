"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage({ apiBaseUrl = "http://localhost:8000" }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const handleLogin = async (e) => {
    if (e) e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      const res = await fetch(`${apiBaseUrl}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Invalid email or password");
      }

      const data = await res.json();
      if (typeof window !== "undefined") {
        localStorage.setItem("launch_gate_token", data.access_token);
        localStorage.setItem(
          "launch_gate_user",
          JSON.stringify({ email, role: data.role || "buyer" })
        );
        window.dispatchEvent(new Event("auth-changed"));
        window.location.href = "/";
      } else {
        router.push("/");
      }
    } catch (err) {
      setError(err.message || "Failed to sign in. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  const fillCredentials = (demoEmail, demoPass) => {
    setEmail(demoEmail);
    setPassword(demoPass);
  };

  return (
    <div style={{
      minHeight: "100vh",
      background: "#080c14",
      color: "#f8fafc",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      padding: "24px",
      fontFamily: "system-ui, -apple-system, sans-serif",
    }}>
      <div style={{
        width: "100%",
        maxWidth: "440px",
        background: "rgba(15, 23, 42, 0.75)",
        backdropFilter: "blur(20px)",
        border: "1px solid rgba(255, 255, 255, 0.1)",
        borderRadius: "20px",
        padding: "40px",
        boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.7)",
      }}>
        {/* Brand Header */}
        <div style={{ textAlign: "center", marginBottom: "32px" }}>
          <div style={{
            width: "48px",
            height: "48px",
            margin: "0 auto 16px auto",
            borderRadius: "12px",
            background: "linear-gradient(135deg, rgba(37, 99, 235, 0.8) 0%, rgba(99, 102, 241, 0.8) 100%)",
            border: "1px solid rgba(255, 255, 255, 0.2)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            boxShadow: "0 0 25px rgba(59, 130, 246, 0.4)",
          }}>
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#ffffff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <polygon points="12 2 2 7 12 12 22 7 12 2"/>
              <polyline points="2 17 12 22 22 17"/>
              <polyline points="2 12 12 17 22 12"/>
            </svg>
          </div>
          <h1 style={{
            fontSize: "24px",
            fontWeight: "800",
            letterSpacing: "-0.02em",
            margin: "0 0 8px 0",
            color: "#ffffff",
          }}>
            Welcome to Launch Gate
          </h1>
          <p style={{ fontSize: "14px", color: "#94a3b8", margin: 0 }}>
            Automated Pre-Launch Verification & Policy Safety
          </p>
        </div>

        {/* Error Alert */}
        {error && (
          <div data-testid="login-error" style={{
            background: "rgba(239, 68, 68, 0.15)",
            border: "1px solid rgba(239, 68, 68, 0.3)",
            borderRadius: "10px",
            padding: "12px 16px",
            marginBottom: "20px",
            color: "#fca5a5",
            fontSize: "13px",
            display: "flex",
            alignItems: "center",
            gap: "8px",
          }}>
            <span>⚠️</span>
            <span>{error}</span>
          </div>
        )}

        {/* Form */}
        <form onSubmit={handleLogin} style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
          <div>
            <label style={{ display: "block", fontSize: "13px", fontWeight: "600", color: "#cbd5e1", marginBottom: "6px" }}>
              Email Address
            </label>
            <input
              data-testid="login-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="buyer@launchgate.com"
              required
              style={{
                width: "100%",
                padding: "12px 14px",
                background: "rgba(8, 12, 20, 0.6)",
                border: "1px solid rgba(255, 255, 255, 0.12)",
                borderRadius: "10px",
                color: "#f8fafc",
                fontSize: "14px",
                outline: "none",
                boxSizing: "border-box",
              }}
            />
          </div>

          <div>
            <label style={{ display: "block", fontSize: "13px", fontWeight: "600", color: "#cbd5e1", marginBottom: "6px" }}>
              Password
            </label>
            <input
              data-testid="login-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••••••"
              required
              style={{
                width: "100%",
                padding: "12px 14px",
                background: "rgba(8, 12, 20, 0.6)",
                border: "1px solid rgba(255, 255, 255, 0.12)",
                borderRadius: "10px",
                color: "#f8fafc",
                fontSize: "14px",
                outline: "none",
                boxSizing: "border-box",
              }}
            />
          </div>

          <button
            data-testid="login-submit"
            type="submit"
            disabled={loading}
            style={{
              marginTop: "8px",
              padding: "13px 20px",
              borderRadius: "10px",
              background: "linear-gradient(135deg, #2563eb 0%, #4f46e5 100%)",
              border: "1px solid rgba(255, 255, 255, 0.2)",
              color: "#ffffff",
              fontSize: "14px",
              fontWeight: "700",
              cursor: loading ? "not-allowed" : "pointer",
              boxShadow: "0 0 20px rgba(37, 99, 235, 0.4)",
              transition: "all 0.2s ease",
            }}
          >
            {loading ? "Signing in..." : "Sign In to Launch Gate"}
          </button>
        </form>

        {/* Quick Demo Fill Buttons */}
        <div style={{ marginTop: "24px", paddingTop: "20px", borderTop: "1px solid rgba(255, 255, 255, 0.08)" }}>
          <div style={{ fontSize: "11px", color: "#64748b", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "10px", textAlign: "center" }}>
            Quick Demo Sign-In
          </div>
          <div style={{ display: "flex", gap: "10px" }}>
            <button
              type="button"
              data-testid="demo-buyer-btn"
              onClick={() => fillCredentials("buyer@launchgate.com", "password123")}
              style={{
                flex: 1,
                padding: "8px 12px",
                background: "rgba(255, 255, 255, 0.05)",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderRadius: "8px",
                color: "#94a3b8",
                fontSize: "12px",
                fontWeight: "600",
                cursor: "pointer",
              }}
            >
              Demo Buyer
            </button>
            <button
              type="button"
              data-testid="demo-manager-btn"
              onClick={() => fillCredentials("manager@launchgate.com", "password123")}
              style={{
                flex: 1,
                padding: "8px 12px",
                background: "rgba(255, 255, 255, 0.05)",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderRadius: "8px",
                color: "#94a3b8",
                fontSize: "12px",
                fontWeight: "600",
                cursor: "pointer",
              }}
            >
              Demo Manager
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
