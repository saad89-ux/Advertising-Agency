import "./review.css";
import Navbar from "../components/Navbar";

export const metadata = {
  title: "Launch Gate — Enterprise Ad Quality & Verification Platform",
  description: "Automated pre-launch ad quality, compliance, and verification gate with human-in-the-loop review.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body style={{
        margin: 0,
        padding: 0,
        background: "#080c14",
        backgroundImage: "radial-gradient(circle at 15% 15%, rgba(37, 99, 235, 0.15) 0%, transparent 40%), radial-gradient(circle at 85% 20%, rgba(139, 92, 246, 0.12) 0%, transparent 45%), radial-gradient(circle at 50% 90%, rgba(14, 165, 233, 0.08) 0%, transparent 50%), radial-gradient(circle at 50% 50%, #0c1222 0%, #060913 100%)",
        backgroundAttachment: "fixed",
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
        color: "#f1f5f9",
        minHeight: "100vh",
        WebkitFontSmoothing: "antialiased",
      }}>
        <Navbar />
        {children}
      </body>
    </html>
  );
}
