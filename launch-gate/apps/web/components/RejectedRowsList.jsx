"use client";

import React from "react";

/**
 * RejectedRowsList Component
 * Displays rows rejected and isolated during ingest, showing raw cells and rejection reasons.
 */
export default function RejectedRowsList({ rejectedRows = [] }) {
  if (!rejectedRows || rejectedRows.length === 0) {
    return null;
  }

  return (
    <section className="rejected-rows-section" data-testid="rejected-rows-section">
      <div className="rejected-rows-header">
        <h2>
          Isolated Rejected Rows <span className="badge-count">({rejectedRows.length})</span>
        </h2>
        <p className="subtitle">
          These rows failed validation or format parsing during sheet upload and were safely isolated.
        </p>
      </div>

      <div className="table-responsive">
        <table className="rejected-rows-table" data-testid="rejected-rows-table">
          <thead>
            <tr>
              <th>Data Row Index</th>
              <th>Row #</th>
              <th>Reason Code</th>
              <th>Reason</th>
              <th>Raw Cells</th>
            </tr>
          </thead>
          <tbody>
            {rejectedRows.map((row, idx) => (
              <tr key={`rejected-${row.data_row_index ?? idx}`} data-testid={`rejected-row-${row.data_row_index}`}>
                <td>
                  <span className="badge-index">#{row.data_row_index}</span>
                </td>
                <td>{row.row_number ?? "—"}</td>
                <td>
                  <code className="reason-code-badge">{row.reason_code}</code>
                </td>
                <td className="reason-text">{row.reason}</td>
                <td className="raw-cells-cell">
                  {Array.isArray(row.raw_cells) ? (
                    <div className="raw-cells-preview" title={JSON.stringify(row.raw_cells)}>
                      {row.raw_cells.map((cell, cIdx) => (
                        <span key={cIdx} className="cell-pill">
                          {typeof cell === "string" ? cell : JSON.stringify(cell)}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <code>{String(row.raw_cells || "")}</code>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
