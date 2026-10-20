"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

export default function Navbar({ apiBaseUrl = "http://localhost:8000" }) {
  const pathname = usePathname();
  const [user, setUser] = useState(null);
  const [token, setToken] = useState(null);
  const [backendOnline, setBackendOnline] = useState(true);
  const [diagInfo, setDiagInfo] = useState(null);

  const checkAuth = () => {
    if (typeof window !== "undefined") {
      const savedToken = localStorage.getItem("launch_gate_token");
      const savedUser = localStorage.getItem("launch_gate_user");
      if (savedToken) {
        setToken(savedToken);
        if (savedUser) {
          try {
            setUser(JSON.parse(savedUser));
          } catch {
            setUser({ email: "buyer@launchgate.com", role: "buyer" });
          }
        } else {
          setUser({ email: "buyer@launchgate.com", role: "buyer" });
        }
      } else {
        setToken(null);
        setUser(null);
      }
    }
  };

  const handleLogout = () => {
    if (typeof window !== "undefined") {
      localStorage.removeItem("launch_gate_token");
      localStorage.removeItem("launch_gate_user");
      setToken(null);
      setUser(null);
      window.location.href = "/login";
    }
  };

  useEffect(() => {
    checkAuth();
    if (typeof window !== "undefined") {
      window.addEventListener("auth-changed", checkAuth);
      window.addEventListener("storage", checkAuth);
    }
    // Health & diagnostics polling
    fetch(`${apiBaseUrl}/health`)
      .then((res) => setBackendOnline(res.ok))
      .catch(() => setBackendOnline(false));

    fetch(`${apiBaseUrl}/diagnostics/ai-tracing`)
      .then((res) => res.ok ? res.json() : null)
      .then((data) => setDiagInfo(data))
      .catch(() => {});

    return () => {
      if (typeof window !== "undefined") {
        window.removeEventListener("auth-changed", checkAuth);
        window.removeEventListener("storage", checkAuth);
      }
    };
  }, [apiBaseUrl]);

  const isManager = false;

  const navLinks = [
    { label: "Home", href: "/" },
    { label: "Live Dispatches", href: "/dispatches" },
    { label: "Performance", href: "/performance" },
    { label: "Violations & Feedback", href: "/violations" },
    { label: "Remediation & HITL", href: "/remediation" },
    { label: "Alerts", href: "/alerts" },
    { label: "All Campaigns", href: "/campaigns" },
    { label: "Revenue Dashboard", href: "/dashboard" },
  ];

  return (
    <header style={{
      background: "rgba(8, 12, 20, 0.75)",
      backdropFilter: "blur(20px) saturate(180%)",
      WebkitBackdropFilter: "blur(20px) saturate(180%)",
      borderBottom: "1px solid rgba(255, 255, 255, 0.08)",
      position: "sticky",
      top: 0,
      zIndex: 100,
      color: "#f8fafc",
      boxShadow: "0 10px 30px -10px rgba(0, 0, 0, 0.5)",
    }}>
      <div style={{
        maxWidth: "1440px",
        margin: "0 auto",
        padding: "0 24px",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        height: "64px",
        gap: "16px",
      }}>
        
        {/* Sleek Typographic Brand Mark */}
        <Link href="/" style={{ display: "flex", alignItems: "center", gap: "10px", textDecoration: "none", color: "#ffffff" }}>
          <div style={{
            width: "34px",
            height: "34px",
            borderRadius: "8px",
            background: "linear-gradient(135deg, rgba(37, 99, 235, 0.8) 0%, rgba(99, 102, 241, 0.8) 100%)",
            border: "1px solid rgba(255, 255, 255, 0.2)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            boxShadow: "0 0 20px rgba(59, 130, 246, 0.35)",
          }}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#ffffff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <polygon points="12 2 2 7 12 12 22 7 12 2"/>
              <polyline points="2 17 12 22 22 17"/>
              <polyline points="2 12 12 17 22 12"/>
            </svg>
          </div>
          <div>
            <div style={{
              fontSize: "16px",
              fontWeight: "900",
              letterSpacing: "0.15em",
              background: "linear-gradient(135deg, #ffffff 0%, #cbd5e1 50%, #93c5fd 100%)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
              lineHeight: 1.1,
            }}>
              LAUNCHGATE
            </div>
            <div style={{ fontSize: "10px", color: "#64748b", letterSpacing: "0.08em", textTransform: "uppercase", fontWeight: "600" }}>
              Ad Quality Intelligence
            </div>
          </div>
        </Link>

        {/* Global Glass Navigation Links */}
        <nav style={{ display: "flex", alignItems: "center", gap: "4px" }}>
          {navLinks.map((link) => {
            const isActive = pathname === link.href || (link.href !== "/" && pathname?.startsWith(link.href));
            return (
              <Link
                key={link.href}
                href={link.href}
                style={{
                  textDecoration: "none",
                  fontSize: "13px",
                  fontWeight: isActive ? "600" : "500",
                  padding: "7px 14px",
                  borderRadius: "8px",
                  color: isActive ? "#ffffff" : "#94a3b8",
                  background: isActive ? "rgba(255, 255, 255, 0.08)" : "transparent",
                  border: isActive ? "1px solid rgba(255, 255, 255, 0.12)" : "1px solid transparent",
                  transition: "all 0.15s ease",
                  display: "flex",
                  alignItems: "center",
                  gap: "6px",
                }}
              >
                <span>{link.label}</span>
              </Link>
            );
          })}
        </nav>

        {/* Right Section: Clean Status Capsule & Auth */}
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <div style={{
            display: "flex",
            alignItems: "center",
            gap: "8px",
            background: "rgba(15, 23, 42, 0.65)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "999px",
            padding: "6px 14px",
          }}>
            <div style={{
              width: "7px",
              height: "7px",
              borderRadius: "50%",
              background: "#10b981",
              boxShadow: "0 0 8px rgba(16, 185, 129, 0.6)",
            }} />
            <span style={{ fontSize: "12px", fontWeight: "600", color: "#e2e8f0", letterSpacing: "0.02em" }}>
              Live Engine Active
            </span>
          </div>

          <div style={{
            display: "flex",
            alignItems: "center",
            gap: "8px",
            background: "rgba(99, 102, 241, 0.12)",
            border: "1px solid rgba(99, 102, 241, 0.25)",
            borderRadius: "8px",
            padding: "5px 12px",
            fontSize: "12px",
            fontWeight: "600",
            color: "#a5b4fc",
          }}>
            <span>⚡</span>
            <span>Meta Campaign Launcher</span>
          </div>
        </div>
      </div>
    </header>
  );
}
