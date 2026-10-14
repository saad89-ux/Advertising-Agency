export async function readError(response) {
  if (response.status === 401) {
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("launch-gate-auth-expired", {
        detail: { returnTo: `${window.location.pathname}${window.location.search}` },
      }));
    }
    return "Your session expired. Please sign in again.";
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    return `Request failed (HTTP ${response.status})`;
  }
  const detail = payload?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      if (typeof item === "string") return item;
      return item?.msg || item?.message || JSON.stringify(item);
    }).join(", ");
  }
  if (detail && typeof detail === "object") {
    return detail.message || detail.msg || JSON.stringify(detail);
  }
  return payload?.message || `Request failed (HTTP ${response.status})`;
}

export function requestErrorMessage(error) {
  if (error?.name === "TypeError" || /failed to fetch|networkerror|network request failed/i.test(error?.message || "")) {
    return "Cannot reach the server. Check that the Launch Gate API is running.";
  }
  return error?.message || "The request failed. Please try again.";
}

export function notificationLabel(result) {
  if (!result) return "";
  if (result.email_status === "sent") return `Alert emailed to ${result.recipient_email || result.details?.recipient_email || "the campaign contact"}`;
  if (result.email_status === "unconfigured_credentials") return "Alert NOT sent — SMTP not configured";
  return `Alert failed: ${result.error || result.details?.email_error || result.email_status || "delivery not confirmed"}`;
}
// Set at build time for Next.js clients; defaults to the local API during development.
export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

