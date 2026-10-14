"use client";
import { API_BASE_URL } from "../../lib/api";
import { readError, requestErrorMessage } from "../../lib/api";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

export default function LoginPage({ apiBaseUrl = API_BASE_URL }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const registeredEmail = params.get("email");
    if (registeredEmail) setEmail(registeredEmail);
    if (params.get("registered") === "1") {
      setNotice("Account created. Sign in with your email and password.");
    }
    if (localStorage.getItem("launch_gate_token")) router.replace("/");
  }, [router]);

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
        if (res.status === 401) throw new Error("Email or password is incorrect.");
        throw new Error(await readError(res));
      }

      const data = await res.json();
      if (typeof window !== "undefined") {
        localStorage.setItem("launch_gate_token", data.access_token);
        localStorage.setItem(
          "launch_gate_user",
          JSON.stringify({ email, role: data.role || "buyer" })
        );
        window.dispatchEvent(new Event("auth-changed"));
        const requestedPath = new URLSearchParams(window.location.search).get("next");
        let destination = "/";
        if (requestedPath) {
          const candidate = new URL(requestedPath, window.location.origin);
          if (candidate.origin === window.location.origin) {
            destination = `${candidate.pathname}${candidate.search}${candidate.hash}`;
          }
        }
        window.location.href = destination;
      } else {
        router.push("/");
      }
    } catch (err) {
      setError(requestErrorMessage(err));
    } finally {
      setLoading(false);
    }
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
        {notice && (
          <div role="status" style={{ background: "rgba(16, 185, 129, 0.12)", border: "1px solid rgba(16, 185, 129, 0.3)", borderRadius: "10px", padding: "12px 16px", marginBottom: "20px", color: "#6ee7b7", fontSize: "13px" }}>
            {notice}
          </div>
        )}
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
              placeholder="name@company.com"
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

        <p style={{ margin: "22px 0 0", textAlign: "center", color: "#94a3b8", fontSize: "13px" }}>
          New to Launch Gate? <Link href="/signup" style={{ color: "#a5b4fc", fontWeight: 600 }}>Create an account</Link>
        </p>

      </div>
    </div>
  );
}
