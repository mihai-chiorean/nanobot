import type { BootstrapResponse, GuestJoinResponse } from "./types";

export class BootstrapError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "BootstrapError";
  }
}

/**
 * Fetch a short-lived token + the WebSocket path from the gateway's
 * ``/webui/bootstrap`` endpoint. Localhost-only on the server side.
 */
export async function fetchBootstrap(
  baseUrl: string = "",
  guestCode?: string | null,
  ownerCode?: string | null,
): Promise<BootstrapResponse> {
  const path = guestCode
    ? `/webui/guest/bootstrap?code=${encodeURIComponent(guestCode)}`
    : "/webui/bootstrap";
  const headers: Record<string, string> = {};
  const trimmedOwnerCode = ownerCode?.trim();
  if (!guestCode && trimmedOwnerCode) {
    headers["X-Ziggy-Owner-Code"] = trimmedOwnerCode;
  }
  const res = await fetch(`${baseUrl}${path}`, {
    method: "GET",
    credentials: "same-origin",
    headers,
  });
  if (!res.ok) {
    throw new BootstrapError(res.status, `bootstrap failed: HTTP ${res.status}`);
  }
  const body = (await res.json()) as BootstrapResponse;
  if (!body.token || !body.ws_path) {
    throw new Error("bootstrap response missing token or ws_path");
  }
  return body;
}

export async function joinGuest(
  email: string,
  invite?: string,
  baseUrl: string = "",
): Promise<GuestJoinResponse> {
  const query = new URLSearchParams({ email });
  const trimmedInvite = invite?.trim();
  if (trimmedInvite) query.set("invite", trimmedInvite);
  const res = await fetch(`${baseUrl}/api/guest/join?${query}`, {
    method: "GET",
    credentials: "same-origin",
  });
  if (!res.ok) {
    throw new BootstrapError(res.status, `join failed: HTTP ${res.status}`);
  }
  const body = (await res.json()) as GuestJoinResponse;
  if (!body.code || !body.guest_url) {
    throw new Error("join response missing guest code");
  }
  return body;
}

/** Derive a WebSocket URL from the current window location and the server-provided path.
 *
 * Keeps the path segment exactly as the server registered it: the root ``/``
 * stays ``/`` and non-root paths are not given an extra trailing slash. This
 * matters because some WS servers dispatch handshakes based on the literal
 * path, not a normalised form.
 */
export function deriveWsUrl(wsPath: string, token: string): string {
  const path = wsPath && wsPath.startsWith("/") ? wsPath : `/${wsPath || ""}`;
  const query = `?token=${encodeURIComponent(token)}`;
  if (typeof window === "undefined") {
    return `ws://127.0.0.1:8765${path}${query}`;
  }
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const host = window.location.host;
  return `${scheme}://${host}${path}${query}`;
}
