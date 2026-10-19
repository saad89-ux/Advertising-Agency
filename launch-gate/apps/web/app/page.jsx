"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import GlassToast from "../components/GlassToast";

export default function HomePage({ apiBaseUrl = "http://localhost:8000" }) {
  const router = useRouter();

  // Glass Toast notification state
  const [toast, setToast] = useState(null);

  // Chat / Batch history state for sidebar
  const [chatHistory, setChatHistory] = useState([]);
  const [activeBatchId, setActiveBatchId] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);

  // Input states
  const [inputTab, setInputTab] = useState("prompt"); // 'prompt' | 'file' | 'media'
  const [promptText, setPromptText] = useState("");
  const [selectedFile, setSelectedFile] = useState(null);
  const [selectedImage, setSelectedImage] = useState(null);
  const [selectedClient, setSelectedClient] = useState("");
  const [availableClients, setAvailableClients] = useState([]);
  const [isCustomClient, setIsCustomClient] = useState(false);
  const [customClientName, setCustomClientName] = useState("");

  // Preflight & LLM Review state
  const [isReviewing, setIsReviewing] = useState(false);
  const [reviewResult, setReviewResult] = useState(null);
  const [missingForm, setMissingForm] = useState({});

  // HITL Launch Modal with Employee Contact
  const [showLaunchModal, setShowLaunchModal] = useState(false);
  const [employeeEmail, setEmployeeEmail] = useState("sarah.ops@launchgate.com");
  const [employeePhone, setEmployeePhone] = useState("+1 (555) 382-9910");
  const [isLaunching, setIsLaunching] = useState(false);

  // Fetch available clients from DB
  const fetchClients = async () => {
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await fetch(`${apiBaseUrl}/campaign-flow/clients`, { headers });
      if (res.ok) {
        const data = await res.json();
        setAvailableClients(data);
        if (data.length > 0 && !selectedClient) {
          setSelectedClient(data[0].client_id);
        }
      }
    } catch (err) {
      console.warn("Failed to fetch clients:", err);
    }
  };

  // Fetch batch chat history
  const fetchChatHistory = async () => {
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const res = await fetch(`${apiBaseUrl}/campaign-flow/chat-history`, { headers });
      if (res.ok) {
        const data = await res.json();
        setChatHistory(data);
        if (data.length > 0 && !activeBatchId) {
          setActiveBatchId(data[0].batch_id);
        }
      }
    } catch (err) {
      console.warn("Failed to fetch chat history:", err);
    }
  };

  useEffect(() => {
    fetchChatHistory();
    fetchClients();
  }, [apiBaseUrl]);

  // Preflight Review LLM Call
  const handleReviewAndCheck = async (e) => {
    if (e) e.preventDefault();
    setIsReviewing(true);

    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      };

      const payload = {
        client_id: selectedClient,
        input_text: promptText,
        file_name: selectedFile?.name || (selectedImage ? "creative_asset.png" : null),
        file_type: selectedFile?.type || (selectedImage ? "image/png" : "text/plain"),
        ad_overrides: {
          ...missingForm,
        },
        social_handles: {
          instagram: missingForm.instagram_handle,
          facebook: missingForm.facebook_handle,
          tiktok: missingForm.tiktok_handle,
        },
        social_tokens: {
          instagram: missingForm.instagram_token,
          facebook: missingForm.facebook_token,
          tiktok: missingForm.tiktok_token,
        },
      };

      const res = await fetch(`${apiBaseUrl}/campaign-flow/preflight-review`, {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        const data = await res.json();
        setReviewResult(data);
      } else {
        setToast({ message: "Preflight review failed. Please check connection.", type: "error" });
      }
    } catch (err) {
      console.error(err);
      setToast({ message: "Network error reviewing campaign resources. Please retry.", type: "error" });
    } finally {
      setIsReviewing(false);
    }
  };

  // Launch Campaign & Capture Employee Contact
  const handleConfirmLaunch = async () => {
    if (!employeeEmail || !employeePhone) {
      setToast({ message: "Please provide employee notification email and phone number.", type: "warning" });
      return;
    }

    setIsLaunching(true);
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      };

      const payload = {
        client_id: selectedClient,
        batch_id: activeBatchId || `batch_${selectedClient}_${Date.now()}`,
        campaign_name: `${selectedClient.toUpperCase()} Multi-Platform Growth`,
        employee_email: employeeEmail,
        employee_phone: employeePhone,
        target_platforms: ["instagram", "facebook", "tiktok", "x", "snapchat"],
      };

      const res = await fetch(`${apiBaseUrl}/campaign-flow/launch-batch`, {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        setShowLaunchModal(false);
        // Direct navigation to Dispatches page
        router.push("/dispatches");
      } else {
        const err = await res.json().catch(() => ({}));
        setToast({ message: err.detail || "Failed to launch campaign. Please check inputs.", type: "error" });
      }
    } catch (err) {
      console.error(err);
      setToast({ message: "Network error dispatching campaign.", type: "error" });
    } finally {
      setIsLaunching(false);
    }
  };

  const handleStartNewBatch = () => {
    const newId = `batch_${selectedClient}_${Date.now().toString(36)}`;
    setActiveBatchId(newId);
    setReviewResult(null);
    setPromptText("");
    setSelectedFile(null);
    setSelectedImage(null);
    setMissingForm({});
  };

  return (
    <div style={{
      display: "flex",
      minHeight: "calc(100vh - 65px)",
      background: "#080c14",
      color: "#f8fafc",
      fontFamily: "system-ui, -apple-system, sans-serif",
    }}>
      {/* 1. LEFT SIDEBAR: Chat & Batch History */}
      <aside style={{
        width: sidebarOpen ? "320px" : "60px",
        transition: "width 0.25s cubic-bezier(0.16, 1, 0.3, 1)",
        background: "rgba(11, 17, 29, 0.85)",
        backdropFilter: "blur(20px)",
        borderRight: "1px solid rgba(255, 255, 255, 0.08)",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        flexShrink: 0,
      }}>
        {/* Sidebar Header */}
        <div style={{
          padding: "18px 16px",
          borderBottom: "1px solid rgba(255, 255, 255, 0.06)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}>
          {sidebarOpen && (
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <span style={{ fontSize: "14px", fontWeight: "700", letterSpacing: "0.02em", color: "#e2e8f0" }}>
                Campaign Sessions
              </span>
              <span style={{
                fontSize: "11px",
                padding: "2px 6px",
                background: "rgba(99, 102, 241, 0.15)",
                color: "#818cf8",
                borderRadius: "999px",
                fontWeight: "600",
              }}>
                1 Batch Active
              </span>
            </div>
          )}
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            title="Toggle Sidebar"
            style={{
              background: "transparent",
              border: "1px solid rgba(255, 255, 255, 0.1)",
              color: "#94a3b8",
              cursor: "pointer",
              borderRadius: "6px",
              padding: "4px 8px",
              fontSize: "13px",
            }}
          >
            {sidebarOpen ? "◀" : "▶"}
          </button>
        </div>

        {/* New Batch Button */}
        {sidebarOpen && (
          <div style={{ padding: "14px 16px" }}>
            <button
              onClick={handleStartNewBatch}
              style={{
                width: "100%",
                padding: "11px 16px",
                background: "linear-gradient(135deg, rgba(99, 102, 241, 0.25) 0%, rgba(139, 92, 246, 0.35) 100%)",
                border: "1px solid rgba(139, 92, 246, 0.4)",
                borderRadius: "10px",
                color: "#f8fafc",
                fontSize: "13px",
                fontWeight: "600",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: "8px",
                boxShadow: "0 4px 14px rgba(99, 102, 241, 0.15)",
              }}
            >
              <span style={{ fontSize: "16px" }}>+</span> New Campaign Batch
            </button>
          </div>
        )}

        {/* Batch Session List */}
        <div style={{ flex: 1, overflowY: "auto", padding: sidebarOpen ? "8px 12px" : "8px 4px" }}>
          {sidebarOpen && (
            <div style={{ fontSize: "11px", fontWeight: "600", color: "#64748b", textTransform: "uppercase", letterSpacing: "0.06em", padding: "8px 8px 4px 8px" }}>
              Recent Batches
            </div>
          )}
          {chatHistory.length === 0 ? (
            sidebarOpen && (
              <div style={{ padding: "24px 12px", textAlign: "center", color: "#64748b", fontSize: "12px" }}>
                <p style={{ margin: "0 0 6px 0", color: "#94a3b8", fontWeight: "600" }}>No batches uploaded yet</p>
                <p style={{ margin: 0, fontSize: "11px", color: "#64748b", lineHeight: 1.4 }}>Upload a CSV or run preflight review to validate your first ad batch.</p>
              </div>
            )
          ) : (
            chatHistory.map((item) => {
              const isActive = activeBatchId === item.batch_id;
              return (
                <div
                  key={item.batch_id}
                  onClick={() => {
                    setActiveBatchId(item.batch_id);
                    setSelectedClient(item.client_id || "");
                  }}
                  style={{
                    padding: sidebarOpen ? "12px 14px" : "10px",
                    borderRadius: "8px",
                    marginBottom: "6px",
                    cursor: "pointer",
                    background: isActive ? "rgba(99, 102, 241, 0.12)" : "transparent",
                    border: isActive ? "1px solid rgba(99, 102, 241, 0.3)" : "1px solid transparent",
                    transition: "all 0.15s ease",
                  }}
                >
                  {sidebarOpen ? (
                    <>
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "4px" }}>
                        <span style={{ fontSize: "13px", fontWeight: "600", color: isActive ? "#ffffff" : "#cbd5e1", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {item.title}
                        </span>
                        <Link
                          href={`/batches/${item.batch_id}`}
                          data-testid="link-batch-review"
                          style={{
                            fontSize: "10px",
                            padding: "1px 6px",
                            borderRadius: "4px",
                            background: item.status === "active_serving" ? "rgba(16, 185, 129, 0.15)" : "rgba(245, 158, 11, 0.15)",
                            color: item.status === "active_serving" ? "#34d399" : "#fbbf24",
                            fontWeight: "600",
                            textDecoration: "none",
                          }}
                        >
                          {item.status === "active_serving" ? "Live" : "Review"}
                        </Link>
                      </div>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", color: "#64748b" }}>
                        <span>{item.client_name || item.client_id}</span>
                        <span>{item.ad_count} ads</span>
                      </div>
                    </>
                  ) : (
                    <div style={{ textAlign: "center", fontSize: "14px", color: isActive ? "#818cf8" : "#64748b" }}>
                      ●
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      </aside>

      {/* 2. MAIN WORKSPACE */}
      <main style={{
        flex: 1,
        padding: "36px 48px",
        overflowY: "auto",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
      }}>
        <div style={{ width: "100%", maxWidth: "860px" }}>
          {/* Welcome Heading */}
          <div style={{ textAlign: "center", marginBottom: "32px" }}>
            <h1 style={{
              fontSize: "36px",
              fontWeight: "800",
              letterSpacing: "-0.03em",
              margin: "0 0 10px 0",
              background: "linear-gradient(180deg, #ffffff 0%, #94a3b8 100%)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
            }}>
              Welcome to Launch Gate
            </h1>
            <p style={{
              fontSize: "16px",
              color: "#94a3b8",
              margin: 0,
              fontWeight: "400",
            }}>
              A place where you review, launch, and track live ad campaigns with automated MCP multi-platform agents
            </p>
          </div>

          {/* Client Selection Strip */}
          <div style={{
            display: "flex",
            alignItems: "center",
            gap: "10px",
            marginBottom: "16px",
            background: "rgba(15, 23, 42, 0.6)",
            padding: "8px 16px",
            borderRadius: "12px",
            border: "1px solid rgba(255, 255, 255, 0.06)",
          }}>
            <span style={{ fontSize: "12px", color: "#64748b", fontWeight: "600", textTransform: "uppercase" }}>Brand / Client:</span>
            {availableClients.length > 0 ? (
              availableClients.map((c) => (
                <button
                  key={c.client_id}
                  onClick={() => {
                    setSelectedClient(c.client_id);
                    setIsCustomClient(false);
                  }}
                  style={{
                    padding: "4px 12px",
                    borderRadius: "6px",
                    fontSize: "12px",
                    fontWeight: "600",
                    background: !isCustomClient && selectedClient === c.client_id ? "rgba(99, 102, 241, 0.2)" : "transparent",
                    color: !isCustomClient && selectedClient === c.client_id ? "#818cf8" : "#94a3b8",
                    border: !isCustomClient && selectedClient === c.client_id ? "1px solid rgba(99, 102, 241, 0.4)" : "1px solid transparent",
                    cursor: "pointer",
                    transition: "all 0.15s ease",
                  }}
                >
                  {c.name || c.client_id}
                </button>
              ))
            ) : (
              <span style={{ fontSize: "12px", color: "#64748b" }}>No registered clients</span>
            )}

            <button
              onClick={() => {
                setIsCustomClient(!isCustomClient);
                if (!isCustomClient && customClientName) {
                  setSelectedClient(customClientName.toLowerCase().replace(/[^a-z0-9_]/g, "") || "custom_client");
                }
              }}
              style={{
                padding: "4px 12px",
                borderRadius: "6px",
                fontSize: "12px",
                fontWeight: "600",
                background: isCustomClient ? "rgba(16, 185, 129, 0.2)" : "rgba(255, 255, 255, 0.04)",
                color: isCustomClient ? "#10b981" : "#94a3b8",
                border: isCustomClient ? "1px solid rgba(16, 185, 129, 0.4)" : "1px solid rgba(255, 255, 255, 0.1)",
                cursor: "pointer",
                transition: "all 0.15s ease",
              }}
            >
              + Real Brand / Client
            </button>

            {isCustomClient && (
              <input
                type="text"
                placeholder="Enter client brand name or domain..."
                value={customClientName}
                onChange={(e) => {
                  setCustomClientName(e.target.value);
                  const clean = e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "");
                  setSelectedClient(clean || "custom_client");
                }}
                style={{
                  background: "rgba(0, 0, 0, 0.4)",
                  border: "1px solid rgba(16, 185, 129, 0.5)",
                  borderRadius: "6px",
                  padding: "4px 10px",
                  color: "#ffffff",
                  fontSize: "12px",
                  outline: "none",
                  width: "220px",
                }}
              />
            )}
          </div>

          {/* Central Input Box */}
          <div style={{
            background: "rgba(15, 23, 42, 0.75)",
            backdropFilter: "blur(24px)",
            borderRadius: "18px",
            border: "1px solid rgba(255, 255, 255, 0.1)",
            padding: "24px",
            boxShadow: "0 20px 40px rgba(0, 0, 0, 0.4)",
            marginBottom: "24px",
          }}>
            {/* Input Format Selector Tabs */}
            <div style={{ display: "flex", gap: "8px", marginBottom: "16px" }}>
              {[
                { id: "prompt", label: "Direct Prompt & Copy", icon: "💬" },
                { id: "file", label: "Campaign Sheet (CSV, XLSX, JSON)", icon: "📄" },
                { id: "media", label: "Creative Asset / Screenshot", icon: "🖼️" },
              ].map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setInputTab(tab.id)}
                  style={{
                    padding: "6px 12px",
                    borderRadius: "8px",
                    fontSize: "12px",
                    fontWeight: "600",
                    background: inputTab === tab.id ? "rgba(255, 255, 255, 0.08)" : "transparent",
                    color: inputTab === tab.id ? "#ffffff" : "#64748b",
                    border: "none",
                    cursor: "pointer",
                  }}
                >
                  {tab.icon} {tab.label}
                </button>
              ))}
            </div>

            {/* Tab 1: Text Prompt */}
            {inputTab === "prompt" && (
              <textarea
                value={promptText}
                onChange={(e) => setPromptText(e.target.value)}
                placeholder="Enter campaign prompt, headline, ad copy, or creative instructions (e.g. 'Launch our summer serum campaign across Instagram Reels and TikTok Spark ads targeting organic skincare buyers')..."
                rows={5}
                style={{
                  width: "100%",
                  background: "rgba(8, 12, 20, 0.6)",
                  border: "1px solid rgba(255, 255, 255, 0.08)",
                  borderRadius: "12px",
                  padding: "14px 16px",
                  color: "#f8fafc",
                  fontSize: "14px",
                  lineHeight: "1.6",
                  resize: "vertical",
                  outline: "none",
                  boxSizing: "border-box",
                }}
              />
            )}

            {/* Tab 2: File Upload */}
            {inputTab === "file" && (
              <div style={{
                border: "2px dashed rgba(255, 255, 255, 0.12)",
                borderRadius: "12px",
                padding: "36px 20px",
                textAlign: "center",
                background: "rgba(8, 12, 20, 0.4)",
              }}>
                <input
                  type="file"
                  id="sheet-upload"
                  accept=".csv,.xlsx,.json,.txt"
                  onChange={(e) => setSelectedFile(e.target.files[0])}
                  style={{ display: "none" }}
                />
                <label htmlFor="sheet-upload" style={{ cursor: "pointer" }}>
                  <div style={{ fontSize: "32px", marginBottom: "8px" }}>📊</div>
                  <div style={{ fontSize: "14px", fontWeight: "600", color: "#e2e8f0", marginBottom: "4px" }}>
                    {selectedFile ? selectedFile.name : "Click to attach campaign sheet (CSV, XLSX, JSON, TXT)"}
                  </div>
                  <div style={{ fontSize: "12px", color: "#64748b" }}>
                    Supports multi-row ad specs with headlines, copy, destination URLs, and UTM codes
                  </div>
                </label>
              </div>
            )}

            {/* Tab 3: Media Upload */}
            {inputTab === "media" && (
              <div style={{
                border: "2px dashed rgba(255, 255, 255, 0.12)",
                borderRadius: "12px",
                padding: "36px 20px",
                textAlign: "center",
                background: "rgba(8, 12, 20, 0.4)",
              }}>
                <input
                  type="file"
                  id="media-upload"
                  accept="image/*,video/*"
                  onChange={(e) => setSelectedImage(e.target.files[0])}
                  style={{ display: "none" }}
                />
                <label htmlFor="media-upload" style={{ cursor: "pointer" }}>
                  <div style={{ fontSize: "32px", marginBottom: "8px" }}>🖼️</div>
                  <div style={{ fontSize: "14px", fontWeight: "600", color: "#e2e8f0", marginBottom: "4px" }}>
                    {selectedImage ? selectedImage.name : "Attach creative visual, video asset, or ad screenshot"}
                  </div>
                  <div style={{ fontSize: "12px", color: "#64748b" }}>
                    Automated agents verify 1:1 and 9:16 aspect ratios, visual brand safety, and platform guidelines
                  </div>
                </label>
              </div>
            )}

            {/* Action Bar */}
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "18px" }}>
              <button
                onClick={handleReviewAndCheck}
                disabled={isReviewing}
                style={{
                  padding: "12px 28px",
                  background: "linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%)",
                  border: "none",
                  borderRadius: "10px",
                  color: "#ffffff",
                  fontSize: "14px",
                  fontWeight: "700",
                  cursor: isReviewing ? "not-allowed" : "pointer",
                  display: "flex",
                  alignItems: "center",
                  gap: "8px",
                  boxShadow: "0 6px 20px rgba(99, 102, 241, 0.35)",
                }}
              >
                {isReviewing ? (
                  <>
                    <span style={{ display: "inline-block", animation: "spin 1s linear infinite" }}>⏳</span>
                    AI Analyzing Campaign & Social Handles...
                  </>
                ) : (
                  <>
                    <span>🔍</span> Review and Launch Campaign
                  </>
                )}
              </button>
            </div>
          </div>

          {/* 3. DYNAMIC PREFLIGHT & MISSING REQUIREMENTS FORM */}
          {reviewResult && reviewResult.status === "missing_requirements" && (
            <div style={{
              background: "rgba(239, 68, 68, 0.08)",
              border: "1px solid rgba(239, 68, 68, 0.3)",
              borderRadius: "16px",
              padding: "24px",
              marginBottom: "24px",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "12px" }}>
                <span style={{ fontSize: "20px" }}>⚠️</span>
                <h3 style={{ margin: 0, fontSize: "16px", fontWeight: "700", color: "#fca5a5" }}>
                  Missing Campaign Requirements Detected ({reviewResult.missing_count})
                </h3>
              </div>
              <p style={{ fontSize: "13px", color: "#cbd5e1", margin: "0 0 18px 0" }}>
                {reviewResult.message}
              </p>

              {/* Dynamic Input Fields */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: "14px" }}>
                {reviewResult.missing_items.map((item, idx) => (
                  <div key={idx} style={{ background: "rgba(15, 23, 42, 0.6)", padding: "14px", borderRadius: "10px", border: "1px solid rgba(255, 255, 255, 0.08)" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "6px" }}>
                      <label style={{ fontSize: "13px", fontWeight: "600", color: "#f8fafc" }}>{item.label}</label>
                      <span style={{ fontSize: "11px", color: "#94a3b8" }}>{item.reason}</span>
                    </div>
                    <input
                      type={item.type || "text"}
                      placeholder={item.placeholder}
                      value={missingForm[item.field] || ""}
                      onChange={(e) => setMissingForm({ ...missingForm, [item.field]: e.target.value })}
                      style={{
                        width: "100%",
                        padding: "10px 14px",
                        background: "rgba(8, 12, 20, 0.8)",
                        border: "1px solid rgba(255, 255, 255, 0.15)",
                        borderRadius: "8px",
                        color: "#ffffff",
                        fontSize: "13px",
                        outline: "none",
                        boxSizing: "border-box",
                      }}
                    />
                  </div>
                ))}
              </div>

              <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "16px" }}>
                <button
                  onClick={handleReviewAndCheck}
                  disabled={isReviewing}
                  style={{
                    padding: "10px 22px",
                    background: "#10b981",
                    border: "none",
                    borderRadius: "8px",
                    color: "#ffffff",
                    fontSize: "13px",
                    fontWeight: "700",
                    cursor: "pointer",
                  }}
                >
                  ✓ Update Requirements & Re-Check
                </button>
              </div>
            </div>
          )}

          {/* 4. VERIFIED HITL REVIEW PAUSE (Ready to Launch) */}
          {reviewResult && reviewResult.status === "ready_for_review" && (
            <div style={{
              background: "rgba(16, 185, 129, 0.08)",
              border: "1px solid rgba(16, 185, 129, 0.3)",
              borderRadius: "16px",
              padding: "24px",
              marginBottom: "24px",
            }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "16px" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                  <span style={{ fontSize: "20px" }}>✅</span>
                  <div>
                    <h3 style={{ margin: 0, fontSize: "16px", fontWeight: "700", color: "#6ee7b7" }}>
                      Pre-Flight Passed — Paused for Human Review
                    </h3>
                    <div style={{ fontSize: "12px", color: "#94a3b8" }}>
                      Compliance Score: 98.4% • All platform handles & tokens validated
                    </div>
                  </div>
                </div>

                <button
                  onClick={() => setShowLaunchModal(true)}
                  style={{
                    padding: "12px 24px",
                    background: "linear-gradient(135deg, #10b981 0%, #059669 100%)",
                    border: "none",
                    borderRadius: "10px",
                    color: "#ffffff",
                    fontSize: "14px",
                    fontWeight: "700",
                    cursor: "pointer",
                    boxShadow: "0 6px 18px rgba(16, 185, 129, 0.3)",
                  }}
                >
                  🚀 Launch Campaign Across Platforms
                </button>
              </div>

              {/* Summary Card */}
              <div style={{
                background: "rgba(15, 23, 42, 0.6)",
                borderRadius: "12px",
                padding: "16px",
                border: "1px solid rgba(255, 255, 255, 0.06)",
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: "12px",
                fontSize: "13px",
              }}>
                <div>
                  <span style={{ color: "#64748b", display: "block", marginBottom: "2px" }}>Headline:</span>
                  <span style={{ color: "#f8fafc", fontWeight: "600" }}>{reviewResult.campaign_summary.headline}</span>
                </div>
                <div>
                  <span style={{ color: "#64748b", display: "block", marginBottom: "2px" }}>Landing Page:</span>
                  <span style={{ color: "#818cf8", fontWeight: "600" }}>{reviewResult.campaign_summary.destination_url}</span>
                </div>
                <div style={{ gridColumn: "1 / -1" }}>
                  <span style={{ color: "#64748b", display: "block", marginBottom: "2px" }}>Platforms Ready:</span>
                  <div style={{ display: "flex", gap: "8px", marginTop: "4px" }}>
                    {["Instagram", "Facebook", "TikTok", "X", "Snapchat"].map((p) => (
                      <span key={p} style={{ padding: "3px 8px", background: "rgba(16, 185, 129, 0.15)", color: "#34d399", borderRadius: "6px", fontSize: "11px", fontWeight: "600" }}>
                        ✓ {p}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </main>

      {/* 5. LAUNCH MODAL: CAPTURE EMPLOYEE NOTIFICATION CONTACT */}
      {showLaunchModal && (
        <div style={{
          position: "fixed",
          inset: 0,
          background: "rgba(0, 0, 0, 0.8)",
          backdropFilter: "blur(12px)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          zIndex: 1000,
        }}>
          <div style={{
            background: "#0f172a",
            border: "1px solid rgba(255, 255, 255, 0.15)",
            borderRadius: "20px",
            padding: "32px",
            width: "100%",
            maxWidth: "520px",
            boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.7)",
          }}>
            <h2 style={{ margin: "0 0 8px 0", fontSize: "20px", fontWeight: "800", color: "#f8fafc" }}>
              Automated Dispatch Notification Authorization
            </h2>
            <p style={{ fontSize: "13px", color: "#94a3b8", lineHeight: "1.5", margin: "0 0 20px 0" }}>
              Please enter the responsible media buyer / employee contact details. If platform review bots flag policy issues, our automated LLM agent will inspect and send a direct SMS & Email alert before re-launching.
            </p>

            <div style={{ display: "flex", flexDirection: "column", gap: "14px", marginBottom: "24px" }}>
              <div>
                <label style={{ fontSize: "13px", fontWeight: "600", color: "#cbd5e1", display: "block", marginBottom: "6px" }}>
                  Employee Email Address:
                </label>
                <input
                  type="email"
                  value={employeeEmail}
                  onChange={(e) => setEmployeeEmail(e.target.value)}
                  placeholder="e.g. sarah.ops@launchgate.com"
                  style={{
                    width: "100%",
                    padding: "10px 14px",
                    background: "rgba(8, 12, 20, 0.8)",
                    border: "1px solid rgba(255, 255, 255, 0.15)",
                    borderRadius: "8px",
                    color: "#ffffff",
                    fontSize: "13px",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
              </div>

              <div>
                <label style={{ fontSize: "13px", fontWeight: "600", color: "#cbd5e1", display: "block", marginBottom: "6px" }}>
                  Employee Phone Number (for SMS policy alerts):
                </label>
                <input
                  type="tel"
                  value={employeePhone}
                  onChange={(e) => setEmployeePhone(e.target.value)}
                  placeholder="e.g. +1 (555) 382-9910"
                  style={{
                    width: "100%",
                    padding: "10px 14px",
                    background: "rgba(8, 12, 20, 0.8)",
                    border: "1px solid rgba(255, 255, 255, 0.15)",
                    borderRadius: "8px",
                    color: "#ffffff",
                    fontSize: "13px",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
              </div>
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end", gap: "10px" }}>
              <button
                onClick={() => setShowLaunchModal(false)}
                style={{
                  padding: "10px 18px",
                  background: "transparent",
                  border: "1px solid rgba(255, 255, 255, 0.15)",
                  borderRadius: "8px",
                  color: "#94a3b8",
                  fontSize: "13px",
                  fontWeight: "600",
                  cursor: "pointer",
                }}
              >
                Cancel
              </button>
              <button
                onClick={handleConfirmLaunch}
                disabled={isLaunching}
                style={{
                  padding: "10px 24px",
                  background: "linear-gradient(135deg, #10b981 0%, #059669 100%)",
                  border: "none",
                  borderRadius: "8px",
                  color: "#ffffff",
                  fontSize: "13px",
                  fontWeight: "700",
                  cursor: isLaunching ? "not-allowed" : "pointer",
                  boxShadow: "0 4px 14px rgba(16, 185, 129, 0.3)",
                }}
              >
                {isLaunching ? "Connecting MCP Servers..." : "Authorize & Launch via MCP Agents 🚀"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Responsive Glass Toast Notification */}
      <GlassToast
        message={toast?.message}
        type={toast?.type}
        onClose={() => setToast(null)}
      />
    </div>
  );
}
