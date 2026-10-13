"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { selectCampaign } from "../lib/campaign-scope";
import GlassToast from "../components/GlassToast";
import { readError, notificationLabel } from "../lib/api";

const AVAILABLE_PLATFORMS = [
  { id: "facebook", label: "Facebook", icon: "📘" },
  { id: "instagram", label: "Instagram", icon: "📸" },
];

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

  // Preflight & LLM Review state
  const [reviewResult, setReviewResult] = useState(null);
  const [missingForm, setMissingForm] = useState({});

  // HITL Launch Modal & Notification Contact Settings
  const [showLaunchModal, setShowLaunchModal] = useState(false);
  const [employeeEmail, setEmployeeEmail] = useState("");
  const [selectedPlatforms, setSelectedPlatforms] = useState(["facebook", "instagram"]);
  const [isLaunching, setIsLaunching] = useState(false);
  const [launchStage, setLaunchStage] = useState("");
  const [uploadedSheet, setUploadedSheet] = useState(null);
  const [creativeRef, setCreativeRef] = useState(null);
  const [formError, setFormError] = useState("");
  const [needsSignIn, setNeedsSignIn] = useState(false);

  // Sync launcher contact with localStorage
  const updateLauncherEmail = (val) => {
    setEmployeeEmail(val);
    if (typeof window !== "undefined") localStorage.setItem("launcher_email", val);
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

      }
    } catch (err) {
      console.warn("Failed to fetch chat history:", err);
    }
  };

  // Fetch launcher contact from DB or localStorage on mount
  useEffect(() => {
    if (typeof window !== "undefined") {
      const savedEmail = localStorage.getItem("launcher_email");

      if (savedEmail) {
        setEmployeeEmail(savedEmail);
      }

    }
  }, []);

  useEffect(() => {
    fetchChatHistory();
  }, [apiBaseUrl]);

  // Open the shared form first; requirements are checked after contact entry.
  const handleReviewAndCheck = (e) => {
    if (e) e.preventDefault();
    // Guard: require at least one input before opening the modal
    const hasPrompt = typeof promptText === "string" && promptText.trim().length > 0;
    const hasFile = Boolean(selectedFile && selectedFile.size > 0);
    const hasImage = Boolean(selectedImage && selectedImage.size > 0);
    if (!hasPrompt && !hasFile && !hasImage) {
      setToast({ message: "Please add a prompt, file, or image before launching.", type: "error" });
      return;
    }
    setFormError("");
    setNeedsSignIn(false);
    setShowLaunchModal(true);
  };

  // Launch Campaign & Capture Employee Contact
  const submitCampaign = async () => {
    if (!employeeEmail || !employeeEmail.includes("@")) {
      setToast({ message: "Please enter a valid launcher email address.", type: "error" });
      return;
    }

    setFormError("");
    setNeedsSignIn(false);
    if (isLaunching) return;
    setIsLaunching(true);
    setLaunchStage("Preparing campaign...");
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      };

      let clientId = "";
      if (!selectedPlatforms.length) throw new Error("Select at least one platform.");
      const metaPlatforms = selectedPlatforms.length > 0 ? selectedPlatforms : ["facebook", "instagram"];
      let currentBatchId = activeBatchId || `batch_${Date.now()}`;
      let uploadedCreativeRef = creativeRef;

      // 1. If user uploaded a campaign sheet (Method 2: CSV/XLSX), persist it to batch table
      if (inputTab === "file" && !selectedFile) throw new Error("Attach a campaign sheet for this new campaign.");
      if (inputTab === "file" && selectedFile && uploadedSheet?.file === selectedFile) currentBatchId = uploadedSheet.batchId;
      if (inputTab === "file" && selectedFile && uploadedSheet?.file !== selectedFile) {
        setLaunchStage("Uploading campaign sheet...");
        try {
          const fileFormData = new FormData();
          fileFormData.append("file", selectedFile);
          fileFormData.append("batch_id", currentBatchId);
          const uploadRes = await fetch(`${apiBaseUrl}/uploads`, {
            method: "POST",
            headers: token ? { Authorization: `Bearer ${token}` } : {},
            body: fileFormData,
          });
          if (uploadRes.ok) {
            const uploadData = await uploadRes.json();
            if (uploadData.accepted_count === 0 && !uploadData.duplicate_sheet) {
              const reasons = (uploadData.rejected_rows || [])
                .map((row) => row.reason || row.reason_code)
                .filter(Boolean)
                .join("; ");
              throw new Error(reasons || "No valid ads were accepted from the uploaded sheet.");
            }
            if (uploadData.batch_id) {
              currentBatchId = uploadData.batch_id;
              setActiveBatchId(currentBatchId);
              setUploadedSheet({file: selectedFile, batchId: currentBatchId});
            }
          } else {
            throw new Error(await readError(uploadRes));
          }
        } catch (uploadErr) {
          throw uploadErr;
        }
      }

      const clientResponse = await fetch(`${apiBaseUrl}/campaign-flow/resolve-client`, {
        method: "POST", headers, body: JSON.stringify({
          batch_id: inputTab === "file" ? currentBatchId : null,
          input_text: promptText,
          ad_overrides: {destination_url: missingForm.destination_url || ""}
        })
      });
      if (clientResponse.status === 401) {
        setNeedsSignIn(true);
        throw new Error("Please sign in to continue. Your notification email has been kept.");
      }
      if (!clientResponse.ok) throw new Error(await readError(clientResponse));
      clientId = (await clientResponse.json()).client_id;

      // 2. If user attached a creative asset (Method 3: Image/Video), persist to Cloud creative storage
      if (inputTab === "media" && selectedImage && !uploadedCreativeRef) {
        try {
          const creativeFormData = new FormData();
          creativeFormData.append("file", selectedImage);
          creativeFormData.append("client_id", clientId);
          const creativeRes = await fetch(`${apiBaseUrl}/uploads/creative`, {
            method: "POST",
            headers: token ? { Authorization: `Bearer ${token}` } : {},
            body: creativeFormData,
          });
          if (creativeRes.ok) { uploadedCreativeRef = (await creativeRes.json()).file_ref; setCreativeRef(uploadedCreativeRef); }
          if (!creativeRes.ok) {
            throw new Error(await readError(creativeRes));
          }
        } catch (creativeErr) {
          throw creativeErr;
        }
      }

      // 3. Dispatch to Meta Platforms (Instagram & Facebook)
      const campaignTitle = promptText && promptText.trim().length > 0
        ? (promptText.trim().split("\n")[0].slice(0, 60))
        : (selectedFile?.name?.replace(/\.[^.]+$/, "") || `${clientId} Campaign`);

      const launchPayload = {
        client_id: clientId,
        batch_id: currentBatchId,
        campaign_name: campaignTitle,
        employee_email: employeeEmail,
        target_platforms: metaPlatforms,
        headline: missingForm.headline || reviewResult?.campaign_summary?.headline || promptText.trim().split("\n")[0] || null,
        ad_text: missingForm.ad_text || reviewResult?.campaign_summary?.ad_text || promptText || null,
        destination_url: missingForm.destination_url || reviewResult?.campaign_summary?.destination_url || null,
        creative_file: uploadedCreativeRef || null,
      };

      setLaunchStage("Reviewing campaign ads...");
      const reviewRequest = {client_id: clientId,
        batch_id: inputTab === "file" ? currentBatchId : null,
        input_text: promptText, ad_overrides: {...launchPayload, target_platforms: metaPlatforms}};
      let checked;
      for (;;) {
      const preflightRes = await fetch(`${apiBaseUrl}/campaign-flow/preflight-review?background=true`, {
        method: "POST", headers, body: JSON.stringify(reviewRequest)
      });
      if (!preflightRes.ok) throw new Error(await readError(preflightRes));
      checked = await preflightRes.json();
      if (checked.status !== "reviewing") break;
      await new Promise(resolve => setTimeout(resolve, 1500));
      }
      setReviewResult(checked);
      if (checked.status !== "ready_for_review") {
        throw new Error("Review the campaign requirements and checks below before launch.");
      }
      setLaunchStage("Dispatching through Meta MCP...");
      const launchRes = await fetch(`${apiBaseUrl}/campaign-flow/launch-batch`, {
        method: "POST",
        headers,
        body: JSON.stringify(launchPayload),
      });

      if (launchRes.ok) {
        const launchData = await launchRes.json();
        setShowLaunchModal(false);
        setActiveBatchId(launchData.batch_id);
        setToast({
          message: (launchData.status === "blocked"
            ? "Meta connector not configured — nothing was sent."
            : launchData.status === "partial"
            ? "Campaign partially dispatched. Review the failed Meta destinations."
            : launchData.status === "launched"
            ? "Campaign dispatched to the configured Meta destinations."
            : "Meta launch failed. Review the recorded dispatch results.") + " " + (launchData.notifications || []).map(notificationLabel).join(" "),
          type: launchData.status === "launched" ? "success" : launchData.status === "blocked" ? "warning" : "error",
        });
        // Refresh IP-scoped sidebar batches
        fetchChatHistory();
        selectCampaign(launchData.batch_id, clientId);
        setSelectedFile(null);
        setUploadedSheet(null);
        setSelectedImage(null);
        setCreativeRef(null);
        {
          router.push(`/dispatches?batch_id=${encodeURIComponent(launchData.batch_id)}&client_id=${encodeURIComponent(clientId)}`);
        }
      } else {
        throw new Error(await readError(launchRes));
      }
    } catch (err) {
      console.warn(err);
      setFormError(err?.message || "The server could not confirm the campaign. Please try again.");
    } finally {
      setIsLaunching(false);
    }
  };

  const handleStartNewBatch = () => {
    const newId = `batch_${Date.now().toString(36)}`;
    setActiveBatchId(newId);
    selectCampaign("", "");
    setUploadedSheet(null);
    setReviewResult(null);
    setPromptText("");
    setSelectedFile(null);
    setSelectedImage(null);
    setCreativeRef(null);
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
                {chatHistory.length} Saved Batches
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
                    if (typeof window !== "undefined") {
                      localStorage.setItem("launch_gate_active_client", item.client_id || "");
                      localStorage.setItem("launch_gate_active_batch", item.batch_id || "");
                    }
                    selectCampaign(item.batch_id, item.client_id);
                    router.push(`/dispatches?batch_id=${encodeURIComponent(item.batch_id)}&client_id=${encodeURIComponent(item.client_id || "")}`);
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
              A place where you review, launch, and track live Meta ad campaigns with automated MCP agents
            </p>
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
                { id: "file", label: "Campaign Sheet (CSV, XLSX)", icon: "📄" },
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
                placeholder="Enter campaign prompt, headline, ad copy, or creative instructions (e.g. 'Launch our summer campaign across Meta placements targeting skincare buyers')..."
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
                  accept=".csv,.xlsx"
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
                  onChange={(e) => {setSelectedImage(e.target.files[0]); setCreativeRef(null); setReviewResult(null);}}
                  style={{ display: "none" }}
                />
                <label htmlFor="media-upload" style={{ cursor: "pointer" }}>
                  <div style={{ fontSize: "32px", marginBottom: "8px" }}>🖼️</div>
                  <div style={{ fontSize: "14px", fontWeight: "600", color: "#e2e8f0", marginBottom: "4px" }}>
                    {selectedImage ? selectedImage.name : "Attach creative visual, video asset, or ad screenshot"}
                  </div>
                  <div style={{ fontSize: "12px", color: "#64748b" }}>
                    Uploaded image dimensions are checked when available. Unsupported creative checks are marked not checked.
                  </div>
                </label>
              </div>
            )}

            {/* Action Bar */}
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "18px" }}>
              <button
                onClick={handleReviewAndCheck}
                disabled={isLaunching}
                style={{
                  padding: "12px 28px",
                  background: "linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%)",
                  border: "none",
                  borderRadius: "10px",
                  color: "#ffffff",
                  fontSize: "14px",
                  fontWeight: "700",
                  cursor: isLaunching ? "not-allowed" : "pointer",
                  display: "flex",
                  alignItems: "center",
                  gap: "8px",
                  boxShadow: "0 6px 20px rgba(99, 102, 241, 0.35)",
                }}
              >
                {isLaunching ? (
                  <>
                    <span style={{ display: "inline-block", animation: "spin 1s linear infinite" }}>⏳</span>
                    Checking campaign requirements...
                  </>
                ) : (
                  <>
                    <span>🔍</span> Review and Launch Campaign
                  </>
                )}
              </button>
            </div>
          </div>

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
                      Preflight Results — Awaiting Human Approval
                    </h3>
                    <div style={{ fontSize: "12px", color: "#94a3b8" }}>
                      Review unavailable checks before authorizing
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
                  <span style={{ color: "#64748b", display: "block", marginBottom: "2px" }}>Target Platforms:</span>
                  <div style={{ display: "flex", gap: "8px", marginTop: "4px" }}>
                    {["Instagram", "Facebook"].map((p) => (
                      <span key={p} style={{ padding: "3px 8px", background: "rgba(16, 185, 129, 0.15)", color: "#34d399", borderRadius: "6px", fontSize: "11px", fontWeight: "600" }}>
                        {p}
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
            maxWidth: "620px",
            maxHeight: "90vh",
            overflowY: "auto",
            boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.7)",
          }}>
            <h2 style={{ margin: "0 0 8px 0", fontSize: "20px", fontWeight: "800", color: "#f8fafc" }}>
              Review and Launch Campaign
            </h2>
            <p style={{ fontSize: "13px", color: "#94a3b8", lineHeight: "1.5", margin: "0 0 20px 0" }}>
              Enter the responsible media buyer email address before launch. Launch Gate dispatches rejection, blocker, and suspension alerts directly to this address via SMTP email.
            </p>

            {formError && <div role="alert" style={{color: "#fcd34d", marginBottom: 16}}>
              {formError} {needsSignIn && <Link href="/login" style={{color: "#a5b4fc"}}>Sign in</Link>}
            </div>}
            {/* Target Platforms Checkboxes */}
            <div style={{ marginBottom: "18px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
                <label style={{ fontSize: "13px", fontWeight: "600", color: "#cbd5e1" }}>
                  Meta Launch Destinations ({selectedPlatforms.length}/{AVAILABLE_PLATFORMS.length}):
                </label>
                <button
                  type="button"
                  onClick={() => {
                    if (selectedPlatforms.length === AVAILABLE_PLATFORMS.length) {
                      setSelectedPlatforms([]);
                    } else {
                      setSelectedPlatforms(AVAILABLE_PLATFORMS.map((p) => p.id));
                    }
                  }}
                  style={{
                    background: "none",
                    border: "none",
                    color: "#38bdf8",
                    fontSize: "12px",
                    cursor: "pointer",
                    padding: "2px 6px",
                    fontWeight: "600",
                  }}
                >
                  {selectedPlatforms.length === AVAILABLE_PLATFORMS.length ? "Deselect All" : "Select All Meta Destinations"}
                </button>
              </div>
              <div style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
                gap: "8px",
                background: "rgba(8, 12, 20, 0.6)",
                padding: "10px",
                borderRadius: "10px",
                border: "1px solid rgba(255, 255, 255, 0.1)",
              }}>
                {AVAILABLE_PLATFORMS.map((p) => {
                  const isChecked = selectedPlatforms.includes(p.id);
                  return (
                    <label
                      key={p.id}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "8px",
                        padding: "8px 10px",
                        borderRadius: "8px",
                        background: isChecked ? "rgba(56, 189, 248, 0.15)" : "rgba(255, 255, 255, 0.03)",
                        border: `1px solid ${isChecked ? "rgba(56, 189, 248, 0.4)" : "rgba(255, 255, 255, 0.08)"}`,
                        cursor: "pointer",
                        fontSize: "12px",
                        color: isChecked ? "#f8fafc" : "#94a3b8",
                        fontWeight: isChecked ? "600" : "400",
                        transition: "all 0.15s ease",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => {
                          setSelectedPlatforms((prev) =>
                            prev.includes(p.id) ? prev.filter((item) => item !== p.id) : [...prev, p.id]
                          );
                        }}
                        style={{ cursor: "pointer", accentColor: "#38bdf8" }}
                      />
                      <span>{p.icon}</span>
                      <span>{p.label}</span>
                    </label>
                  );
                })}
              </div>
              {selectedPlatforms.length === 0 && (
                <p style={{ margin: "6px 0 0 0", fontSize: "11px", color: "#f43f5e" }}>
                  ⚠️ Select at least one platform to proceed with launch.
                </p>
              )}
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: "14px", marginBottom: "24px" }}>
              <div>
                <p style={{color: "#94a3b8", fontSize: 12}}>Used only to send you launch and policy alerts.</p>
                <label style={{ fontSize: "13px", fontWeight: "600", color: "#cbd5e1", display: "block", marginBottom: "6px" }}>
                  Employee Email Address:
                </label>
                <input
                  type="email"
                  value={employeeEmail}
                  onChange={(e) => updateLauncherEmail(e.target.value)}
                  placeholder="e.g. buyer@launchgate.com"
                  style={{
                    width: "100%",
                    padding: "10px 14px",
                    background: "rgba(8, 12, 20, 0.8)",
                    border: `1px solid ${!employeeEmail || !employeeEmail.includes("@") ? "rgba(244, 63, 94, 0.4)" : "rgba(255, 255, 255, 0.15)"}`,
                    borderRadius: "8px",
                    color: "#ffffff",
                    fontSize: "13px",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
                {(!employeeEmail || !employeeEmail.includes("@")) && (
                  <p style={{ margin: "4px 0 0 0", fontSize: "11px", color: "#f43f5e" }}>
                    ⚠️ Valid email containing '@' is required.
                  </p>
                )}
              </div>

            </div>

            {inputTab !== "file" && <div style={{display: "grid", gap: 12, marginBottom: 20}}>
              <p style={{margin: 0, fontSize: 13, color: "#cbd5e1"}}>Confirm the campaign details before authorization.</p>
              {[{key: "headline", label: "Headline"}, {key: "ad_text", label: "Ad copy"}, {key: "destination_url", label: "Landing page URL"}].map(({key, label}) => <label key={key}>
                {label}
                <input type={key === "destination_url" ? "url" : "text"}
                  value={missingForm[key] ?? (key === "headline" ? promptText.trim().split("\n")[0] || "" : key === "ad_text" ? promptText : "")}
                  onChange={e => {setMissingForm({...missingForm, [key]: e.target.value}); setReviewResult(null);}}
                  style={{display: "block", width: "100%", boxSizing: "border-box", padding: 10, background: "#111827", color: "white", border: "1px solid #475569", borderRadius: 8}} />
              </label>)}
            </div>}
            {reviewResult?.missing_items?.length > 0 && <div role="alert" style={{color: "#fcd34d", marginBottom: 16}}>
              {reviewResult.missing_items.map(item => <p key={item.field}>{item.label}: {item.reason}</p>)}
            </div>}
            {reviewResult?.checks && <div style={{marginBottom: 16}}>
              {reviewResult.checks.map((check, i) => <p key={i} style={{color: check.status === "fail" ? "#fca5a5" : check.status === "not_checked" ? "#fcd34d" : "#6ee7b7"}}>
                {check.platform}: {check.name} — {check.status} {(check.findings || []).join(" ")}
              </p>)}
            </div>}

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
              {(() => {
                const isLaunchDisabled =
                  isLaunching ||
                  selectedPlatforms.length === 0 ||
                  !employeeEmail ||
                  !employeeEmail.includes("@");
                return (
                  <button
                    onClick={submitCampaign}
                    disabled={isLaunchDisabled}
                    style={{
                      padding: "10px 24px",
                      background: isLaunchDisabled
                        ? "rgba(255, 255, 255, 0.1)"
                        : "linear-gradient(135deg, #10b981 0%, #059669 100%)",
                      border: "none",
                      borderRadius: "8px",
                      color: isLaunchDisabled ? "#64748b" : "#ffffff",
                      fontSize: "13px",
                      fontWeight: "700",
                      cursor: isLaunchDisabled ? "not-allowed" : "pointer",
                      boxShadow: isLaunchDisabled ? "none" : "0 4px 14px rgba(16, 185, 129, 0.3)",
                      transition: "all 0.2s ease",
                    }}
                  >
                    {isLaunching ? launchStage : "Authorize & Launch via MCP Agents 🚀"}
                  </button>
                );
              })()}
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
