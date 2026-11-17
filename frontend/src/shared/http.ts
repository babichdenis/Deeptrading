const API = window.location.port === "5173" || window.location.port === "5174"
  ? `http://${window.location.hostname}:8000`
  : "";

export async function fetchJSON<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${url}`, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.clone().json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      detail = "";
    }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}
