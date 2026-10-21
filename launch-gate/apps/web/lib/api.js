export async function readError(response) {
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

export function notificationLabel(result) {
  if (!result) return "";
  if (result.email_status === "sent") return `Alert emailed to ${result.recipient_email || result.details?.recipient_email || "the campaign contact"}`;
  if (result.email_status === "unconfigured_credentials") return "Alert NOT sent — SMTP not configured";
  return `Alert failed: ${result.error || result.details?.email_error || result.email_status || "delivery not confirmed"}`;
}
