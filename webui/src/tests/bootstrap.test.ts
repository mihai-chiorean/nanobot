import { beforeEach, describe, expect, it, vi } from "vitest";

import { fetchBootstrap, joinGuest } from "@/lib/bootstrap";

describe("webui bootstrap", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          token: "tok",
          ws_path: "/",
          expires_in: 300,
          access: "guest",
          guest_code: "guest-code",
        }),
      }),
    );
  });

  it("uses the owner bootstrap route by default", async () => {
    await fetchBootstrap();

    expect(fetch).toHaveBeenCalledWith(
      "/webui/bootstrap",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("uses the guest bootstrap route when a guest code is present", async () => {
    await fetchBootstrap("", "guest code");

    expect(fetch).toHaveBeenCalledWith(
      "/webui/guest/bootstrap?code=guest%20code",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("joins guests by email", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        code: "guest-code",
        guest_url: "/?guest=guest-code",
        created: true,
      }),
    } as Response);

    const body = await joinGuest("Test@Example.com", "let-me-in");

    expect(body.code).toBe("guest-code");
    expect(fetch).toHaveBeenCalledWith(
      "/api/guest/join?email=Test%40Example.com&invite=let-me-in",
      expect.objectContaining({ method: "GET" }),
    );
  });
});
