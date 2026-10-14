"use client";
import { API_BASE_URL } from "../../lib/api";
import { scopedFetch as fetch } from "../../lib/campaign-scope";
import { campaignScope } from "../../lib/campaign-scope";
import { readError, requestErrorMessage } from "../../lib/api";

import React, { useState, useEffect, Suspense } from "react";
import Link from "next/link";
import { useSearchParams, useRouter } from "next/navigation";
import SocialFeedModal from "../../components/SocialFeedModal";

function DispatchesContent({ apiBaseUrl = API_BASE_URL }) {
  const searchParams = useSearchParams();
  const router = useRouter();

  const [tracesData, setTracesData] = useState(null);
  const [allAds, setAllAds] = useState([]);
  const [groupedClients, setGroupedClients] = useState([]);
  const [availableClients, setAvailableClients] = useState([]);
  const activeScope = campaignScope();
  const initialClientId = searchParams?.get("client_id") || activeScope.clientId || "";
  const initialBatchId = searchParams?.get("batch_id") || activeScope.batchId || "";
  const [selectedClientId, setSelectedClientId] = useState(initialClientId.startsWith("mock-") ? "" : initialClientId);
  const [selectedBatchId, setSelectedBatchId] = useState(initialBatchId.startsWith("mock-") ? "" : initialBatchId);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [previewAd, setPreviewAd] = useState(null);

  // Sync with URL search params if they change
  useEffect(() => {
    const qClient = searchParams?.get("client_id");
    const qBatch = searchParams?.get("batch_id");
    if (qClient !== null && qClient !== undefined) {
      setSelectedClientId(qClient.startsWith("mock-") ? "" : qClient);
    }
    if (qBatch !== null && qBatch !== undefined) {
      setSelectedBatchId(qBatch.startsWith("mock-") ? "" : qBatch);
    }
  }, [searchParams]);

  const fetchData = async () => {
    setLoading(true);
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;
      const headers = token ? { Authorization: `Bearer ${token}` } : {};

      const tracesUrl = selectedClientId
        ? `${apiBaseUrl}/campaign-flow/mcp-traces?client_id=${encodeURIComponent(selectedClientId)}`
        : `${apiBaseUrl}/campaign-flow/mcp-traces`;

      const allCampUrl = selectedClientId
        ? `${apiBaseUrl}/campaign-flow/all-campaigns?client_id=${encodeURIComponent(selectedClientId)}${selectedBatchId ? `&batch_id=${encodeURIComponent(selectedBatchId)}` : ""}`
        : `${apiBaseUrl}/campaign-flow/all-campaigns`;

      const [tracesRes, campaignsRes, clientsRes] = await Promise.all([
        fetch(tracesUrl, { headers }),
        fetch(allCampUrl, { headers }),
        fetch(`${apiBaseUrl}/campaign-flow/clients`, { headers }),
      ]);

      for (const response of [tracesRes, campaignsRes, clientsRes]) if (!response.ok) throw new Error(await readError(response));
      setLoadError("");
      if (tracesRes.ok) {
        const tData = await tracesRes.json();
        setTracesData(tData);
      }

      if (clientsRes.ok) {
        const cData = await clientsRes.json();
        setAvailableClients(cData || []);
      }

      if (campaignsRes.ok) {
        const raw = await campaignsRes.json();
        const adsList = Array.isArray(raw) ? raw : (raw.campaigns || []);
        setAllAds(adsList);

        // Group into client cards
        const map = {};
        adsList.forEach((ad) => {
          const cid = ad.client_id || "default";
          if (!map[cid]) {
            map[cid] = {
              client_id: cid,
              client_name: ad.client_name || cid.replace(/_/g, " ").toUpperCase(),
              total_campaigns: 0,
              active_campaigns: 0,
              campaigns: [],
            };
          }
          const deliv = ad.delivery_status || "live_serving";
          const isAct = deliv === "live_serving" || deliv === "active_live" || deliv === "ACTIVE" || deliv === "active";
          map[cid].campaigns.push({
            name: ad.headline || `${map[cid].client_name} Campaign Ad`,
            campaign_id: ad.ad_id,
            status: isAct ? "ACTIVE" : deliv.toUpperCase(),
            platform: (ad.platform || "Instagram").toUpperCase(),
            tracked: ad.tracked !== false,
            spend: ad.spend_usd ?? ad.spend ?? null,
            orders: ad.inbound_orders ?? null,
            headline: ad.headline,
            destination_url: ad.destination_url,
            handle: ad.handle || `@${cid}`,
            ad_id: ad.ad_id,
          });
          map[cid].total_campaigns += 1;
          map[cid].active_campaigns += isAct ? 1 : 0;
        });

        setGroupedClients(Object.values(map));
      }
    } catch (err) {
      setLoadError(requestErrorMessage(err));
      setAllAds([]);
      setGroupedClients([]);
      console.error("Dispatches fetch error:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, [apiBaseUrl, selectedClientId, selectedBatchId]);

  const activeTraces = tracesData?.traces || [];
  const activeListenersCount = tracesData?.mcp_active_servers ?? 0;

  const handleSelectClientFilter = (cid) => {
    if (selectedBatchId) return;
    setSelectedClientId(cid);
    setSelectedBatchId("");
    if (typeof window !== "undefined") {
      localStorage.setItem("launch_gate_active_client", cid);
    }
    if (cid) {
      router.push(`/dispatches?client_id=${encodeURIComponent(cid)}`);
    } else {
      router.push("/dispatches");
    }
  };

  // Filter grouped clients if client_id is selected
  const displayedClients = selectedClientId
    ? groupedClients.filter((c) => c.client_id.toLowerCase() === selectedClientId.toLowerCase())
    : groupedClients;

  const currentClientMeta = availableClients.find((c) => c.client_id === selectedClientId) ||
    groupedClients.find((c) => c.client_id === selectedClientId);

  return (
    <div style={{
      minHeight: "calc(100vh - 65px)",
      background: "#080c14",
      color: "#f8fafc",
      padding: "36px 48px",
      fontFamily: "system-ui, -apple-system, sans-serif",
    }}>
      <div style={{ maxWidth: "1280px", margin: "0 auto" }}>
        
        {loadError && <p role="alert">{loadError}</p>}
      {/* Header Strip */}
        <div style={{ marginBottom: "24px", display: "flex", justifyContent: "space-between", alignItems: "flex-end", flexWrap: "wrap", gap: "16px" }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "8px" }}>
              <span style={{ fontSize: "26px" }}>📡</span>
              <h1 style={{
                fontSize: "28px",
                fontWeight: "800",
                letterSpacing: "-0.02em",
                margin: 0,
                background: "linear-gradient(180deg, #ffffff 0%, #94a3b8 100%)",
                WebkitBackgroundClip: "text",
                WebkitTextFillColor: "transparent",
              }}>
                Live Campaign Dispatches & MCP Tracking
              </h1>
            </div>
            <p style={{ fontSize: "14px", color: "#94a3b8", margin: 0 }}>
              Real-time audit log of automated Meta campaign dispatches across connected platform agents.
            </p>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
            <Link
              href="/"
              style={{
                padding: "8px 16px",
                borderRadius: "10px",
                background: "linear-gradient(135deg, rgba(99, 102, 241, 0.2) 0%, rgba(139, 92, 246, 0.3) 100%)",
                border: "1px solid rgba(139, 92, 246, 0.4)",
                color: "#ffffff",
                fontSize: "13px",
                fontWeight: "600",
                textDecoration: "none",
                display: "flex",
                alignItems: "center",
                gap: "6px",
              }}
            >
              <span>+</span> New Campaign
            </Link>

            <div style={{
              background: activeListenersCount > 0 ? "rgba(16, 185, 129, 0.12)" : "rgba(100, 116, 139, 0.12)",
              border: activeListenersCount > 0 ? "1px solid rgba(16, 185, 129, 0.3)" : "1px solid rgba(100, 116, 139, 0.3)",
              padding: "6px 14px",
              borderRadius: "999px",
              fontSize: "12px",
              fontWeight: "600",
              color: activeListenersCount > 0 ? "#34d399" : "#94a3b8",
              display: "flex",
              alignItems: "center",
              gap: "8px",
            }}>
              <span style={{
                display: "inline-block",
                width: "8px",
                height: "8px",
                borderRadius: "50%",
                background: activeListenersCount > 0 ? "#10b981" : "#64748b",
                boxShadow: activeListenersCount > 0 ? "0 0 10px #10b981" : "none"
              }}></span>
              {loadError ? "Dispatch status unavailable" : activeListenersCount > 0 ? "Meta MCP Connected" : "No active MCP listeners"}
            </div>
          </div>
        </div>

        {/* Dynamic Campaign Switcher Tabs */}
        <div style={{
          background: "rgba(15, 23, 42, 0.65)",
          backdropFilter: "blur(20px)",
          borderRadius: "14px",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          padding: "10px 14px",
          marginBottom: "24px",
          display: "flex",
          alignItems: "center",
          gap: "8px",
          overflowX: "auto",
        }}>
          <span style={{ fontSize: "12px", color: "#64748b", fontWeight: "700", textTransform: "uppercase", paddingRight: "4px" }}>
            Filter Campaign:
          </span>

          <button
            onClick={() => handleSelectClientFilter("")}
            style={{
              padding: "6px 14px",
              borderRadius: "8px",
              fontSize: "12px",
              fontWeight: "600",
              cursor: "pointer",
              background: !selectedClientId ? "rgba(99, 102, 241, 0.25)" : "rgba(255, 255, 255, 0.04)",
              color: !selectedClientId ? "#818cf8" : "#94a3b8",
              border: !selectedClientId ? "1px solid rgba(99, 102, 241, 0.5)" : "1px solid rgba(255, 255, 255, 0.06)",
              transition: "all 0.15s ease",
              whiteSpace: "nowrap",
            }}
          >
            🌟 All Campaigns ({loadError ? "Unavailable" : allAds.length})
          </button>

          {availableClients.map((client) => {
            const isSel = selectedClientId.toLowerCase() === client.client_id.toLowerCase();
            const matchingCount = allAds.filter((a) => a.client_id?.toLowerCase() === client.client_id?.toLowerCase()).length;
            return (
              <button
                key={client.client_id}
                onClick={() => handleSelectClientFilter(client.client_id)}
                style={{
                  padding: "6px 14px",
                  borderRadius: "8px",
                  fontSize: "12px",
                  fontWeight: "600",
                  cursor: "pointer",
                  background: isSel ? "rgba(16, 185, 129, 0.25)" : "rgba(255, 255, 255, 0.04)",
                  color: isSel ? "#34d399" : "#94a3b8",
                  border: isSel ? "1px solid rgba(16, 185, 129, 0.5)" : "1px solid rgba(255, 255, 255, 0.06)",
                  transition: "all 0.15s ease",
                  whiteSpace: "nowrap",
                  display: "flex",
                  alignItems: "center",
                  gap: "6px",
                }}
              >
                <span>{client.name || client.client_id}</span>
                {matchingCount > 0 && (
                  <span style={{
                    fontSize: "10px",
                    padding: "1px 6px",
                    borderRadius: "999px",
                    background: isSel ? "rgba(16, 185, 129, 0.3)" : "rgba(255, 255, 255, 0.1)",
                    color: isSel ? "#ffffff" : "#94a3b8",
                  }}>
                    {matchingCount}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {/* Selected Campaign Banner */}
        {selectedClientId && (
          <div style={{
            background: "linear-gradient(135deg, rgba(30, 41, 59, 0.7) 0%, rgba(15, 23, 42, 0.85) 100%)",
            border: "1px solid rgba(16, 185, 129, 0.3)",
            borderRadius: "14px",
            padding: "16px 20px",
            marginBottom: "24px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            flexWrap: "wrap",
            gap: "12px",
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
              <div style={{
                width: "36px",
                height: "36px",
                borderRadius: "10px",
                background: "rgba(16, 185, 129, 0.2)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: "18px",
              }}>
                🎯
              </div>
              <div>
                <div style={{ fontSize: "15px", fontWeight: "700", color: "#f8fafc" }}>
                  Active Focus: {currentClientMeta?.client_name || currentClientMeta?.name || selectedClientId}
                </div>
                <div style={{ fontSize: "12px", color: "#94a3b8" }}>
                  Client ID: <code style={{ color: "#34d399" }}>{selectedClientId}</code>
                  {selectedBatchId && <span> | Batch: <code style={{ color: "#818cf8" }}>{selectedBatchId}</code></span>}
                </div>
              </div>
            </div>

            <button
              onClick={() => handleSelectClientFilter("")}
              style={{
                background: "rgba(255, 255, 255, 0.06)",
                border: "1px solid rgba(255, 255, 255, 0.15)",
                color: "#cbd5e1",
                padding: "6px 12px",
                borderRadius: "8px",
                fontSize: "12px",
                fontWeight: "600",
                cursor: "pointer",
              }}
            >
              ✕ Clear Filter (Show All)
            </button>
          </div>
        )}

        {/* 1. MCP Platform Dispatch Pipeline Status */}
        <div style={{
          background: "rgba(15, 23, 42, 0.75)",
          backdropFilter: "blur(20px)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          borderRadius: "16px",
          padding: "20px 24px",
          marginBottom: "28px",
          boxShadow: "0 10px 30px rgba(0, 0, 0, 0.3)",
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <span style={{ fontSize: "16px" }}>⚡</span>
              <span style={{ fontSize: "14px", fontWeight: "700", color: "#f8fafc" }}>
                Multi-Platform Dispatch Pipeline Status
              </span>
            </div>
            <span style={{ fontSize: "13px", fontWeight: "600", color: activeTraces.length > 0 ? "#10b981" : "#94a3b8" }}>
              {loadError ? "Audit data unavailable" : activeTraces.length > 0 ? `${activeTraces.length} recorded events` : "No recorded events"}
            </span>
          </div>
          <p style={{ fontSize: "13px", color: "#64748b", margin: 0 }}>
            {activeTraces.length > 0
              ? "Platform dispatch events recorded in audit log. Meta API execution details are tracked below."
              : "No platform dispatch activity is recorded for this view."}
          </p>
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
              <span style={{ fontSize: "12px", color: "#94a3b8", marginLeft: "6px" }}>
                MCP Tool Calls & Platform Dispatches {selectedClientId ? `[Filtered: ${selectedClientId}]` : ""}
              </span>
            </div>
            <span style={{ fontSize: "11px", color: "#64748b" }}>
              {loadError ? "Audit data unavailable" : activeTraces.length > 0 ? "Recorded Audit Events" : "No Recorded Events"}
            </span>
          </div>

          {activeTraces.length > 0 ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "8px", fontSize: "12px", lineHeight: "1.6" }}>
              {activeTraces.map((t, idx) => (
                <div key={idx} style={{ display: "flex", gap: "12px", padding: "6px 8px", background: "rgba(255, 255, 255, 0.02)", borderRadius: "6px", alignItems: "center" }}>
                  <span style={{ color: "#64748b" }}>[{t.timestamp || "Live"}]</span>
                  <span style={{ color: "#38bdf8", fontWeight: "600" }}>{t.server}</span>
                  <span style={{ color: "#a78bfa" }}>→ {t.tool}</span>
                  <span style={{
                    color: t.status === "DISPATCH_FAILED" ? "#ef4444" : "#10b981",
                    fontWeight: "600",
                    fontSize: "11px",
                    padding: "1px 6px",
                    borderRadius: "4px",
                    background: t.status === "DISPATCH_FAILED" ? "rgba(239, 68, 68, 0.15)" : "rgba(16, 185, 129, 0.15)",
                  }}>
                    {t.status}
                  </span>
                  <span style={{ color: "#94a3b8", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {t.payload_summary || `Dispatched to Meta (${t.platform})`}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ padding: "24px 12px", textAlign: "center", color: "#64748b", fontSize: "13px" }}>
              {loadError ? "Dispatch error data is unavailable because the server request failed." : "No dispatch error data is currently recorded."}
            </div>
          )}
        </div>

        {/* 3. Live Platform Deployments by Client / Campaign */}
        <div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
            <h2 style={{ fontSize: "20px", fontWeight: "800", color: "#f8fafc", margin: 0 }}>
              Live Platform Deployments {selectedClientId ? `(${displayedClients.length > 0 ? displayedClients[0].client_name : selectedClientId})` : "Across Clients"}
            </h2>
            <span style={{ fontSize: "13px", color: "#64748b" }}>
              {loadError ? "Unavailable" : displayedClients.reduce((acc, c) => acc + (c.campaigns || []).length, 0)} Total Ads Active
            </span>
          </div>

          {loading ? (
            <div style={{ padding: "40px", textAlign: "center", color: "#94a3b8" }}>
              Loading live campaign deployments...
            </div>
          ) : loadError ? (
            <div role="alert" style={{ padding: "40px", textAlign: "center", color: "#fecaca" }}>
              Dispatch data is unavailable: {loadError}
            </div>
          ) : displayedClients.length === 0 || !displayedClients.some((c) => (c.campaigns || []).length > 0) ? (
            <div style={{
              background: "rgba(15, 23, 42, 0.7)",
              border: "1px dashed rgba(255, 255, 255, 0.12)",
              borderRadius: "16px",
              padding: "40px 24px",
              textAlign: "center",
              color: "#94a3b8",
            }}>
              <div style={{ fontSize: "32px", marginBottom: "12px" }}>📦</div>
              <div style={{ fontSize: "15px", fontWeight: "600", color: "#e2e8f0", marginBottom: "6px" }}>
                No active dispatches found {selectedClientId ? `for ${selectedClientId}` : ""}
              </div>
              <p style={{ fontSize: "13px", color: "#64748b", maxWidth: "480px", margin: "0 auto 16px auto" }}>
                Launch ads from the Home workspace to see live platform status, spend, and performance tracking here.
              </p>
              <Link
                href="/"
                style={{
                  display: "inline-block",
                  padding: "8px 18px",
                  borderRadius: "8px",
                  background: "linear-gradient(135deg, #10b981 0%, #059669 100%)",
                  color: "#ffffff",
                  fontSize: "13px",
                  fontWeight: "600",
                  textDecoration: "none",
                }}
              >
                Launch Ads Now
              </Link>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
              {displayedClients.map((client) => (
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
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px", borderBottom: "1px solid rgba(255, 255, 255, 0.06)", paddingBottom: "12px", flexWrap: "wrap", gap: "8px" }}>
                    <div>
                      <span style={{ fontSize: "18px", fontWeight: "700", color: "#ffffff", marginRight: "12px" }}>
                        {client.client_name}
                      </span>
                      <span style={{ fontSize: "12px", padding: "2px 8px", background: "rgba(99, 102, 241, 0.15)", color: "#818cf8", borderRadius: "6px", fontWeight: "600" }}>
                        Client ID: {client.client_id}
                      </span>
                    </div>
                    <span style={{ fontSize: "12px", color: "#34d399", fontWeight: "600" }}>
                      ● Active: {client.active_campaigns || client.campaigns.length} / Total: {client.total_campaigns || client.campaigns.length}
                    </span>
                  </div>

                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: "16px" }}>
                    {(client.campaigns || []).map((camp, idx) => (
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
                          transition: "border 0.2s ease",
                        }}
                      >
                        <div>
                          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "10px" }}>
                            <span style={{ fontSize: "14px", fontWeight: "700", color: "#e2e8f0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              {camp.name}
                            </span>
                            <span style={{
                              fontSize: "11px",
                              padding: "2px 8px",
                              borderRadius: "999px",
                              background: camp.status === "ACTIVE" ? "rgba(16, 185, 129, 0.15)" : "rgba(245, 158, 11, 0.15)",
                              color: camp.status === "ACTIVE" ? "#34d399" : "#fbbf24",
                              fontWeight: "600",
                            }}>
                              ● {camp.status}
                            </span>
                          </div>

                          <div style={{ fontSize: "12px", color: "#64748b", marginBottom: "4px" }}>
                            Ad ID: <span style={{ color: "#94a3b8", fontWeight: "600" }}>{camp.campaign_id}</span>
                          </div>
                          
                          <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "12px" }}>
                            <span style={{
                              fontSize: "11px",
                              padding: "2px 8px",
                              borderRadius: "4px",
                              background: "rgba(56, 189, 248, 0.12)",
                              color: "#38bdf8",
                              fontWeight: "600",
                            }}>
                              {camp.platform.includes("FACEBOOK") ? "📘 Facebook" : "📷 Instagram"}
                            </span>
                            <span style={{ fontSize: "12px", color: "#64748b" }}>
                              {camp.handle}
                            </span>
                          </div>

                          {camp.destination_url && (
                            <div style={{ fontSize: "11px", color: "#64748b", marginBottom: "12px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              🔗 <span style={{ color: "#94a3b8" }}>{camp.destination_url}</span>
                            </div>
                          )}
                        </div>

                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", borderTop: "1px solid rgba(255, 255, 255, 0.06)", paddingTop: "12px" }}>
                          <div style={{ fontSize: "12px" }}>
                            <span style={{ color: "#64748b" }}>Spend: </span>
                            <span style={{ color: "#f8fafc", fontWeight: "600" }}>
                              {camp.spend != null ? `$${Number(camp.spend).toFixed(2)}` : "Not tracked"}
                            </span>
                            <span style={{ color: "#64748b", marginLeft: "10px" }}>Orders: </span>
                            <span style={{ color: "#34d399", fontWeight: "600" }}>{camp.orders != null ? camp.orders : "Not tracked"}</span>
                          </div>

                          <button
                            onClick={() => setPreviewAd({
                              ad_id: camp.campaign_id,
                              platform: camp.platform.toLowerCase(),
                              headline: camp.headline,
                              handle: camp.handle,
                            })}
                            style={{
                              background: "rgba(255, 255, 255, 0.06)",
                              border: "1px solid rgba(255, 255, 255, 0.12)",
                              borderRadius: "6px",
                              color: "#cbd5e1",
                              fontSize: "11px",
                              fontWeight: "600",
                              padding: "4px 10px",
                              cursor: "pointer",
                            }}
                          >
                            👁️ Preview
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {previewAd && (
        <SocialFeedModal
          ad={previewAd}
          adId={previewAd.ad_id}
          platform={previewAd.platform}
          headline={previewAd.headline}
          clientHandle={previewAd.handle}
          clientId={previewAd.client_id}
          destinationUrl={previewAd.destination_url}
          reviewStatus={previewAd.platform_review_status}
          onClose={() => setPreviewAd(null)}
        />
      )}
    </div>
  );
}

export default function DispatchesPage(props) {
  return (
    <Suspense fallback={
      <div style={{ minHeight: "calc(100vh - 65px)", background: "#080c14", color: "#f8fafc", padding: "40px", textAlign: "center" }}>
        Loading Dispatches...
      </div>
    }>
      <DispatchesContent {...props} />
    </Suspense>
  );
}
