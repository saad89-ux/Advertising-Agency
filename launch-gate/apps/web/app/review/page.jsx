"use client";
import { scopedFetch as fetch } from "../../lib/campaign-scope";
import { readError } from "../../lib/api";

import React, { useState, useEffect } from "react";
import Link from "next/link";

/**
 * Next.js Page: /review
 * Lists recent batches visible to the reviewer and allows jumping straight into review.
 */
export default function ReviewIndexPage({ apiBaseUrl = "http://localhost:8000" }) {
  const [batches, setBatches] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [customBatchId, setCustomBatchId] = useState("");

  useEffect(() => {
    let isMounted = true;
    const token =
      typeof window !== "undefined" ? localStorage.getItem("launch_gate_token") : null;

    const headers = { Accept: "application/json" };
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    fetch(`${apiBaseUrl}/batches`, { headers })
      .then(async (res) => {
        if (!isMounted) return;
        if (!res.ok) {
          throw new Error(await readError(res));
        }
        return res.json();
      })
      .then((data) => {
        if (isMounted) {
          setBatches(Array.isArray(data) ? data : []);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (isMounted) {
          setError(err.message);
          setLoading(false);
        }
      });

    return () => {
      isMounted = false;
    };
  }, [apiBaseUrl]);

  return (
    <div className="review-index-container" data-testid="review-index-page">
      <header className="review-index-header">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: "12px" }}>
          <div>
            <h1>Launch Gate — Batch Review</h1>
            <p>Select a batch to inspect ads beside their findings.</p>
          </div>
          <a
            href="/metrics"
            className="btn-metrics-action btn-export"
            data-testid="link-review-to-metrics"
            style={{ textDecoration: "none" }}
          >
            📊 Operational Metrics Dashboard →
          </a>
        </div>
      </header>

      {/* Direct Batch Jump Input */}
      <section className="batch-jump-box">
        <h3>Open by Batch ID</h3>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (customBatchId.trim()) {
              window.location.href = `/batches/${customBatchId.trim()}`;
            }
          }}
          className="jump-form"
        >
          <input
            type="text"
            className="input-jump"
            placeholder="Enter batch UUID (e.g. 550e8400-e29b-41d4-a716-446655440000)"
            value={customBatchId}
            onChange={(e) => setCustomBatchId(e.target.value)}
            data-testid="input-jump-batch-id"
          />
          <button
            type="submit"
            className="btn-jump"
            disabled={!customBatchId.trim()}
            data-testid="btn-jump-submit"
          >
            Review Batch →
          </button>
        </form>
      </section>

      {/* Available Batches List */}
      <section className="batches-list-section">
        <h3>Recent Batches</h3>
        {loading && <p className="loading-text">Loading available batches...</p>}
        {error && <p className="error-text">Notice: {error}</p>}

        {!loading && batches.length === 0 && (
          <div className="empty-batches-state">
            <p>No batches found for your assigned clients.</p>
            <p>Upload a launch sheet first or enter a known batch ID above.</p>
          </div>
        )}

        {batches.length > 0 && (
          <div className="batches-table-wrapper">
            <table className="batches-table" data-testid="batches-table">
              <thead>
                <tr>
                  <th>Filename</th>
                  <th>Batch ID</th>
                  <th>Uploaded</th>
                  <th>Ads</th>
                  <th>Clients</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {batches.map((b) => (
                  <tr key={b.batch_id} data-testid={`batch-row-${b.batch_id}`}>
                    <td className="filename-col">
                      <strong>{b.filename}</strong>
                    </td>
                    <td className="uuid-col">
                      <code>{b.batch_id}</code>
                    </td>
                    <td>{new Date(b.created_at).toLocaleString()}</td>
                    <td>
                      <span className="count-badge">{b.ad_count ?? 0}</span>
                    </td>
                    <td>
                      {Array.isArray(b.client_ids) && b.client_ids.length > 0 ? (
                        b.client_ids.map((c) => (
                          <span key={c} className="client-pill">
                            {c}
                          </span>
                        ))
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </td>
                    <td>
                      <a
                        href={`/batches/${b.batch_id}`}
                        className="btn-review-link"
                        data-testid={`link-review-${b.batch_id}`}
                      >
                        Review Screen →
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
