export default function HomePage() {
  return (
    <main className="review-screen-container">
      <h1>Launch Gate</h1>
      <p>Pre-launch ad quality and compliance gate.</p>
      <div style={{ marginTop: "20px", display: "flex", gap: "12px", flexWrap: "wrap" }}>
        <a
          href="/review"
          className="btn-action btn-approve"
          style={{ textDecoration: "none", display: "inline-block" }}
        >
          Go to Batch Review Screen →
        </a>
        <a
          href="/metrics"
          className="btn-metrics-action btn-export"
          style={{ textDecoration: "none", display: "inline-block" }}
          data-testid="link-home-to-metrics"
        >
          📊 Quality & Operational Metrics →
        </a>
      </div>
    </main>
  );
}
