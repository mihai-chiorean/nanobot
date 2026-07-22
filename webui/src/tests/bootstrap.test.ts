import { describe, expect, it, vi } from "vitest";

import { fetchBootstrap } from "@/lib/bootstrap";

describe("fetchBootstrap", () => {
  it("exchanges the Clerk token at the authenticated bootstrap route", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ token: "ziggy-token", ws_path: "/ws" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchBootstrap("clerk-token")).resolves.toMatchObject({
      token: "ziggy-token",
      ws_path: "/ws",
    });
    expect(fetchMock).toHaveBeenCalledWith("/auth/bootstrap", {
      method: "GET",
      headers: { Authorization: "Bearer clerk-token" },
    });
  });
});
