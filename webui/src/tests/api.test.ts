import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteSession,
  fetchActivity,
  fetchSessionMessages,
  fetchWorkTask,
  fetchWorkTasks,
  updateSettings,
} from "@/lib/api";

describe("webui API helpers", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ deleted: true, key: "websocket:chat-1", messages: [] }),
      }),
    );
  });

  it("percent-encodes websocket keys when fetching session history", async () => {
    await fetchSessionMessages("tok", "websocket:chat-1");

    expect(fetch).toHaveBeenCalledWith(
      "/api/sessions/websocket%3Achat-1/messages",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });

  it("percent-encodes websocket keys when deleting a session", async () => {
    await deleteSession("tok", "websocket:chat-1");

    expect(fetch).toHaveBeenCalledWith(
      "/api/sessions/websocket%3Achat-1/delete",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });

  it("serializes settings updates as a narrow query string", async () => {
    await updateSettings("tok", {
      model: "openrouter/test",
      provider: "openrouter",
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/update?model=openrouter%2Ftest&provider=openrouter",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });

  it("normalizes activity rows from snake_case API payloads", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        activity: [
          {
            key: "websocket:chat-1",
            chat_id: "chat-1",
            created_at: "2026-01-01T00:00:00",
            updated_at: "2026-01-01T00:01:00",
            preview: "First turn",
            status: "waiting",
            live: true,
            message_count: 3,
            last_role: "assistant",
            last_text: "Pick an option",
          },
          {
            key: "websocket:chat-2",
            chat_id: "chat-2",
            created_at: null,
            updated_at: null,
            status: "unexpected",
          },
        ],
      }),
    } as Response);

    const rows = await fetchActivity("tok");

    expect(fetch).toHaveBeenCalledWith(
      "/api/activity",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
    expect(rows[0]).toMatchObject({
      chatId: "chat-1",
      status: "waiting",
      live: true,
      messageCount: 3,
      lastRole: "assistant",
    });
    expect(rows[1].status).toBe("idle");
  });

  it("normalizes work tasks and percent-encodes detail routes", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        tasks: [
          {
            task_id: "work_1",
            scope: "owner",
            session_key: "websocket:chat-1",
            chat_id: "chat-1",
            title: "Report",
            prompt_preview: "Draft report",
            status: "running",
            mode: "background",
            model: "MiniMax",
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:01:00Z",
            last_seq: 4,
            artifact_count: 1,
          },
        ],
      }),
    } as Response);

    const rows = await fetchWorkTasks("tok");
    expect(fetch).toHaveBeenCalledWith(
      "/api/work",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
    expect(rows[0]).toMatchObject({
      task_id: "work_1",
      status: "running",
      artifact_count: 1,
    });

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        task: {
          task_id: "work:encoded",
          scope: "owner",
          session_key: "websocket:chat-1",
          chat_id: "chat-1",
          title: "Detail",
          prompt_preview: "",
          status: "succeeded",
          mode: "background",
          model: "",
          created_at: "",
          updated_at: "",
          last_seq: 1,
          artifact_count: 0,
        },
      }),
    } as Response);

    await fetchWorkTask("tok", "work:encoded");
    expect(fetch).toHaveBeenLastCalledWith(
      "/api/work/work%3Aencoded",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });
});
