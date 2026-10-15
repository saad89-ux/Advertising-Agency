"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";

export default function HomePage({ apiBaseUrl = "http://localhost:8000" }) {
  const [currentUser, setCurrentUser] = useState(null);
  const [token, setToken] = useState(null);
  const [authError, setAuthError] = useState(null);
  const [authLoading, setAuthLoading] = useState(false);

  // Manual login fields
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  // Upload state
  const [selectedFile, setSelectedFile] = useState(null);
  const [uploadLoading, setUploadLoading] = useState(false);
  const [uploadError, setUploadError] = useState(null);
  const [uploadSuccess, setUploadSuccess] = useState(null);

  // Recent batches
  const [batches, setBatches] = useState([]);
  const [loadingBatches, setLoadingBatches] = useState(false);
  const [customBatchId, setCustomBatchId] = useState("");

  useEffect(() => {
    if (typeof window !== "undefined") {
      const savedToken = localStorage.getItem("launch_gate_token");
      const savedUser = localStorage.getItem("launch_gate_user");
      if (savedToken) {
        setToken(savedToken);
        if (savedUser) {
          try {
            setCurrentUser(JSON.parse(savedUser));
          } catch {
            setCurrentUser({ email: "Authenticated User" });
          }
        }
      }
    }
  }, []);

  const fetchRecentBatches = (authToken) => {
    const activeToken = authToken || token;
    const headers = { Accept: "application/json" };
    if (activeToken) {
      headers["Authorization"] = `Bearer ${activeToken}`;
    }
    setLoadingBatches(true);
    fetch(`${apiBaseUrl}/batches`, { headers })
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => {
        setBatches(Array.isArray(data) ? data : []);
        setLoadingBatches(false);
      })
      .catch(() => setLoadingBatches(false));
  };

  useEffect(() => {
    fetchRecentBatches(token);
  }, [token, apiBaseUrl]);

  const handleLogin = async (loginEmail, loginPassword) => {
    setAuthLoading(true);
    setAuthError(null);
    try {
      const res = await fetch(`${apiBaseUrl}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: loginEmail, password: loginPassword }),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Authentication failed");
      }
      const data = await res.json();
      const accessToken = data.access_token;
      const userObj = { email: loginEmail };
      localStorage.setItem("launch_gate_token", accessToken);
      localStorage.setItem("launch_gate_user", JSON.stringify(userObj));
      setToken(accessToken);
      setCurrentUser(userObj);
      fetchRecentBatches(accessToken);
    } catch (err) {
      setAuthError(err.message);
    } finally {
      setAuthLoading(false);
    }
  };

  const handleLogout = () => {
    localStorage.removeItem("launch_gate_token");
    localStorage.removeItem("launch_gate_user");
    setToken(null);
    setCurrentUser(null);
  };

  const handleUpload = async (e) => {
    e.preventDefault();
    if (!selectedFile) {
      setUploadError("Please choose a CSV or XLSX launch sheet first.");
      return;
    }
    if (!token) {
      setUploadError("You must be logged in as a Buyer to upload a launch sheet.");
      return;
    }

    setUploadLoading(true);
    setUploadError(null);
    setUploadSuccess(null);

    const formData = new FormData();
    formData.append("file", selectedFile);

    try {
      const res = await fetch(`${apiBaseUrl}/uploads`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
        },
        body: formData,
      });

      const data = await res.json().catch(() => ({}));

      if (!res.ok) {
        const detail = typeof data.detail === "object" ? JSON.stringify(data.detail) : data.detail;
        throw new Error(detail || `Upload failed with HTTP ${res.status}`);
      }

      const batchId = data.batch_id || data.id;
      setUploadSuccess(`Upload successful! Batch ID: ${batchId}`);
      if (batchId) {
        window.location.href = `/batches/${batchId}`;
      }
    } catch (err) {
      setUploadError(err.message);
    } finally {
      setUploadLoading(false);
    }
  };

  return (
    <main className="review-screen-container" style={{ maxWidth: "1100px", margin: "0 auto", padding: "24px" }}>
      {/* Top Banner */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", borderBottom: "2px solid #e2e8f0", paddingBottom: "16px", marginBottom: "24px", flexWrap: "wrap", gap: "16px" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: "28px", fontWeight: "700", color: "#0f172a" }}>🚀 Launch Gate</h1>
          <p style={{ margin: "4px 0 0 0", color: "#64748b" }}>Pre-launch Ad Quality, Automated Compliance & Review Gate</p>
        </div>
        <div style={{ display: "flex", gap: "10px", alignItems: "center" }}>
          <a
            href="/metrics"
            className="btn-metrics-action btn-export"
            style={{ textDecoration: "none", fontSize: "14px", padding: "8px 16px" }}
            data-testid="link-home-to-metrics"
          >
            📊 Metrics Dashboard
          </a>
          <a
            href="http://127.0.0.1:8000/docs"
            target="_blank"
            rel="noreferrer"
            className="btn-metrics-action"
            style={{ textDecoration: "none", fontSize: "14px", padding: "8px 16px", background: "#f8fafc", border: "1px solid #cbd5e1", color: "#334155" }}
          >
            📖 Interactive API Docs
          </a>
        </div>
      </div>

      {/* Grid Layout: Left Column = Auth & Upload, Right Column = Recent Batches */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: "24px" }}>
        {/* Left Column: User Auth & Upload */}
        <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
          {/* Card 1: User Session / One-Click Login */}
          <div style={{ background: "#ffffff", border: "1px solid #e2e8f0", borderRadius: "10px", padding: "20px", boxShadow: "0 1px 3px rgba(0,0,0,0.05)" }}>
            <h2 style={{ fontSize: "18px", marginTop: 0, marginBottom: "12px", color: "#1e293b" }}>
              👤 User Authentication
            </h2>

            {currentUser ? (
              <div style={{ background: "#f0fdf4", border: "1px solid #bbf7d0", borderRadius: "8px", padding: "14px" }}>
                <p style={{ margin: "0 0 8px 0", color: "#166534", fontWeight: "600" }}>
                  ✅ Signed In as: <span style={{ color: "#15803d" }}>{currentUser.email}</span>
                </p>
                <button
                  type="button"
                  onClick={handleLogout}
                  className="btn-action"
                  style={{ background: "#ef4444", color: "#ffffff", border: "none", padding: "6px 14px", borderRadius: "6px", cursor: "pointer", fontSize: "13px" }}
                >
                  Sign Out
                </button>
              </div>
            ) : (
              <div>
                <p style={{ fontSize: "13px", color: "#64748b", marginTop: 0 }}>
                  Select a demo account for instant one-click login:
                </p>
                <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", marginBottom: "16px" }}>
                  <button
                    type="button"
                    disabled={authLoading}
                    onClick={() => handleLogin("buyer@launchgate.com", "password123")}
                    style={{ background: "#2563eb", color: "#ffffff", border: "none", padding: "8px 14px", borderRadius: "6px", cursor: "pointer", fontSize: "13px", fontWeight: "500" }}
                  >
                    👤 One-Click Buyer (BrightLeaf)
                  </button>
                  <button
                    type="button"
                    disabled={authLoading}
                    onClick={() => handleLogin("manager@launchgate.com", "password123")}
                    style={{ background: "#475569", color: "#ffffff", border: "none", padding: "8px 14px", borderRadius: "6px", cursor: "pointer", fontSize: "13px", fontWeight: "500" }}
                  >
                    🛡️ One-Click Account Manager
                  </button>
                </div>

                <details style={{ fontSize: "13px", color: "#475569" }}>
                  <summary style={{ cursor: "pointer", fontWeight: "500" }}>Or sign in manually</summary>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      handleLogin(email, password);
                    }}
                    style={{ marginTop: "10px", display: "flex", flexDirection: "column", gap: "8px" }}
                  >
                    <input
                      type="email"
                      placeholder="Email address"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      required
                      style={{ padding: "8px", borderRadius: "6px", border: "1px solid #cbd5e1" }}
                    />
                    <input
                      type="password"
                      placeholder="Password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      required
                      style={{ padding: "8px", borderRadius: "6px", border: "1px solid #cbd5e1" }}
                    />
                    <button
                      type="submit"
                      disabled={authLoading}
                      style={{ padding: "8px", borderRadius: "6px", background: "#0f172a", color: "#ffffff", border: "none", cursor: "pointer" }}
                    >
                      {authLoading ? "Signing in..." : "Sign In"}
                    </button>
                  </form>
                </details>

                {authError && (
                  <p style={{ color: "#dc2626", fontSize: "13px", marginTop: "10px" }}>❌ {authError}</p>
                )}
              </div>
            )}
          </div>

          {/* Card 2: Upload Launch Sheet */}
          <div style={{ background: "#ffffff", border: "1px solid #e2e8f0", borderRadius: "10px", padding: "20px", boxShadow: "0 1px 3px rgba(0,0,0,0.05)" }}>
            <h2 style={{ fontSize: "18px", marginTop: 0, marginBottom: "8px", color: "#1e293b" }}>
              📁 Upload Launch Sheet
            </h2>
            <p style={{ fontSize: "13px", color: "#64748b", marginTop: 0, marginBottom: "16px" }}>
              Upload any CSV or XLSX sheet (sample sheets are in <code>data/labeled_sheets/</code>).
            </p>

            <form onSubmit={handleUpload}>
              <div style={{ border: "2px dashed #cbd5e1", borderRadius: "8px", padding: "16px", textAlign: "center", marginBottom: "14px", background: "#f8fafc" }}>
                <input
                  type="file"
                  accept=".csv,.xlsx"
                  onChange={(e) => {
                    if (e.target.files && e.target.files[0]) {
                      setSelectedFile(e.target.files[0]);
                    }
                  }}
                  id="sheet-upload-input"
                  style={{ display: "block", margin: "0 auto", fontSize: "13px" }}
                />
                {selectedFile && (
                  <p style={{ margin: "8px 0 0 0", fontSize: "13px", color: "#2563eb", fontWeight: "500" }}>
                    Selected: {selectedFile.name} ({(selectedFile.size / 1024).toFixed(1)} KB)
                  </p>
                )}
              </div>

              <button
                type="submit"
                disabled={uploadLoading || !selectedFile}
                className="btn-action btn-approve"
                style={{
                  width: "100%",
                  padding: "10px 16px",
                  fontSize: "14px",
                  fontWeight: "600",
                  cursor: uploadLoading || !selectedFile ? "not-allowed" : "pointer",
                  opacity: uploadLoading || !selectedFile ? 0.6 : 1,
                }}
              >
                {uploadLoading ? "⏳ Ingesting & Analyzing Sheet..." : "🚀 Upload & Launch Review →"}
              </button>
            </form>

            {uploadError && (
              <div style={{ marginTop: "12px", padding: "10px", background: "#fef2f2", border: "1px solid #fecaca", borderRadius: "6px", color: "#dc2626", fontSize: "13px" }}>
                ❌ {uploadError}
              </div>
            )}
            {uploadSuccess && (
              <div style={{ marginTop: "12px", padding: "10px", background: "#f0fdf4", border: "1px solid #bbf7d0", borderRadius: "6px", color: "#166534", fontSize: "13px" }}>
                ✅ {uploadSuccess}
              </div>
            )}
          </div>
        </div>

        {/* Right Column: Recent Batches & Quick Jump */}
        <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
          {/* Card 3: Direct Batch Jump */}
          <div style={{ background: "#ffffff", border: "1px solid #e2e8f0", borderRadius: "10px", padding: "20px", boxShadow: "0 1px 3px rgba(0,0,0,0.05)" }}>
            <h2 style={{ fontSize: "18px", marginTop: 0, marginBottom: "8px", color: "#1e293b" }}>
              🔍 Open Specific Batch
            </h2>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (customBatchId.trim()) {
                  window.location.href = `/batches/${customBatchId.trim()}`;
                }
              }}
              style={{ display: "flex", gap: "8px" }}
            >
              <input
                type="text"
                placeholder="Enter Batch UUID"
                value={customBatchId}
                onChange={(e) => setCustomBatchId(e.target.value)}
                style={{ flex: 1, padding: "8px 12px", borderRadius: "6px", border: "1px solid #cbd5e1", fontSize: "13px" }}
              />
              <button
                type="submit"
                disabled={!customBatchId.trim()}
                style={{ padding: "8px 14px", background: "#0f172a", color: "#ffffff", border: "none", borderRadius: "6px", cursor: "pointer", fontSize: "13px", fontWeight: "500" }}
              >
                Open →
              </button>
            </form>
          </div>

          {/* Card 4: Recent Uploaded Batches */}
          <div style={{ background: "#ffffff", border: "1px solid #e2e8f0", borderRadius: "10px", padding: "20px", boxShadow: "0 1px 3px rgba(0,0,0,0.05)", flex: 1 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
              <h2 style={{ fontSize: "18px", margin: 0, color: "#1e293b" }}>
                📋 Recent Batches
              </h2>
              <button
                type="button"
                onClick={() => fetchRecentBatches(token)}
                style={{ background: "transparent", border: "1px solid #cbd5e1", borderRadius: "4px", padding: "4px 8px", fontSize: "12px", cursor: "pointer" }}
              >
                🔄 Refresh
              </button>
            </div>

            {loadingBatches ? (
              <p style={{ color: "#64748b", fontSize: "13px" }}>Loading batches...</p>
            ) : batches.length === 0 ? (
              <div style={{ textAlign: "center", padding: "30px 10px", color: "#94a3b8" }}>
                <p style={{ margin: "0 0 6px 0", fontSize: "15px" }}>No batches loaded yet</p>
                <p style={{ margin: 0, fontSize: "12px" }}>Upload a CSV or XLSX sheet above to start reviewing ads.</p>
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "10px", maxHeight: "380px", overflowY: "auto" }}>
                {batches.slice(0, 8).map((b) => (
                  <div
                    key={b.batch_id}
                    style={{
                      border: "1px solid #e2e8f0",
                      borderRadius: "6px",
                      padding: "12px",
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      background: "#f8fafc",
                    }}
                  >
                    <div>
                      <div style={{ fontWeight: "600", fontSize: "13px", color: "#0f172a" }}>
                        Batch #{b.batch_id.slice(0, 8)}...
                      </div>
                      <div style={{ fontSize: "12px", color: "#64748b", marginTop: "2px" }}>
                        Client: <strong>{b.client_id || "brightleaf"}</strong> | {b.ad_count !== undefined ? `${b.ad_count} ads` : "Uploaded"}
                      </div>
                    </div>
                    <a
                      href={`/batches/${b.batch_id}`}
                      style={{
                        background: "#2563eb",
                        color: "#ffffff",
                        padding: "6px 12px",
                        borderRadius: "5px",
                        textDecoration: "none",
                        fontSize: "12px",
                        fontWeight: "500",
                      }}
                    >
                      Review Cockpit →
                    </a>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </main>
  );
}
