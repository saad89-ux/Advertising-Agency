"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { API_BASE_URL, readError, requestErrorMessage } from "../../lib/api";

export default function SignupPage({ apiBaseUrl = API_BASE_URL }) {
  const router = useRouter();
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (localStorage.getItem("launch_gate_token")) router.replace("/");
  }, [router]);

  const handleSignup = async (event) => {
    event.preventDefault();
    setError("");
    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          display_name: displayName,
          email: email.trim().toLowerCase(),
          password,
        }),
      });
      if (!response.ok) throw new Error(await readError(response));

      const registeredEmail = email.trim().toLowerCase();
      router.replace(`/login?registered=1&email=${encodeURIComponent(registeredEmail)}`);
    } catch (signupError) {
      setError(requestErrorMessage(signupError));
    } finally {
      setLoading(false);
    }
  };

  return (
    <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24, color: "#f8fafc" }}>
      <section style={{ width: "100%", maxWidth: 440, background: "rgba(15, 23, 42, 0.9)", border: "1px solid rgba(255,255,255,0.1)", borderRadius: 12, padding: 36 }}>
        <div style={{ textAlign: "center", marginBottom: 28 }}>
          <h1 style={{ margin: "0 0 8px", fontSize: 26, fontWeight: 800 }}>Create your account</h1>
          <p style={{ margin: 0, color: "#94a3b8", fontSize: 14 }}>Launch Gate account registration</p>
        </div>

        {error && <div role="alert" style={{ marginBottom: 18, padding: "12px 14px", color: "#fecaca", background: "rgba(239,68,68,0.14)", border: "1px solid rgba(239,68,68,0.3)", borderRadius: 8, fontSize: 13 }}>{error}</div>}

        <form onSubmit={handleSignup} style={{ display: "grid", gap: 16 }}>
          <label style={{ display: "grid", gap: 6, color: "#cbd5e1", fontSize: 13, fontWeight: 600 }}>
            Display name
            <input required maxLength={100} autoComplete="name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} style={inputStyle} />
          </label>
          <label style={{ display: "grid", gap: 6, color: "#cbd5e1", fontSize: 13, fontWeight: 600 }}>
            Email address
            <input required type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} style={inputStyle} />
          </label>
          <label style={{ display: "grid", gap: 6, color: "#cbd5e1", fontSize: 13, fontWeight: 600 }}>
            Password
            <input required type="password" minLength={12} autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} style={inputStyle} />
            <span style={{ color: "#94a3b8", fontSize: 12, fontWeight: 400 }}>At least 12 characters, including a letter and a number.</span>
          </label>
          <label style={{ display: "grid", gap: 6, color: "#cbd5e1", fontSize: 13, fontWeight: 600 }}>
            Confirm password
            <input required type="password" autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} style={inputStyle} />
          </label>
          <button type="submit" disabled={loading} style={{ marginTop: 6, padding: "13px 18px", border: "1px solid rgba(255,255,255,0.2)", borderRadius: 8, background: "linear-gradient(135deg, #2563eb, #4f46e5)", color: "white", fontWeight: 700, cursor: loading ? "wait" : "pointer" }}>
            {loading ? "Creating account..." : "Create account"}
          </button>
        </form>

        <p style={{ margin: "22px 0 0", textAlign: "center", color: "#94a3b8", fontSize: 13 }}>
          Already have an account? <Link href="/login" style={{ color: "#a5b4fc", fontWeight: 600 }}>Sign in</Link>
        </p>
      </section>
    </main>
  );
}

const inputStyle = {
  width: "100%",
  boxSizing: "border-box",
  padding: "11px 12px",
  border: "1px solid rgba(255,255,255,0.14)",
  borderRadius: 8,
  background: "#0b1220",
  color: "#f8fafc",
  fontSize: 14,
};