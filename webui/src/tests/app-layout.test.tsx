import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatSummary } from "@/lib/types";

const connectSpy = vi.fn();
const refreshSpy = vi.fn();
const createChatSpy = vi.fn().mockResolvedValue("chat-1");
const deleteChatSpy = vi.fn();
const getTokenSpy = vi.fn().mockResolvedValue("clerk-token");
let mockSessions: ChatSummary[] = [];
let mockSignedIn = true;
let clientOptions: { onReauth?: () => Promise<string | null> } | null = null;

vi.mock("@clerk/react", () => ({
  useAuth: () => ({
    getToken: getTokenSpy,
    isLoaded: true,
    isSignedIn: mockSignedIn,
  }),
  SignInButton: ({ children }: { children: React.ReactNode }) => children,
  SignUpButton: ({ children }: { children: React.ReactNode }) => children,
  UserButton: () => <button aria-label="Account" />,
}));

vi.mock("@/hooks/useSessions", async (importOriginal) => {
  const React = await import("react");
  const actual = await importOriginal<typeof import("@/hooks/useSessions")>();
  return {
    ...actual,
    useSessions: () => {
      const [sessions, setSessions] = React.useState(mockSessions);
      return {
        sessions,
        loading: false,
        error: null,
        refresh: refreshSpy,
        createChat: createChatSpy,
        updateSessionPreview: vi.fn(),
        deleteChat: async (key: string) => {
          await deleteChatSpy(key);
          setSessions((prev: ChatSummary[]) => prev.filter((s) => s.key !== key));
        },
      };
    },
  };
});

vi.mock("@/hooks/useTheme", () => ({
  useTheme: () => ({
    theme: "light" as const,
    toggle: vi.fn(),
  }),
}));

vi.mock("@/lib/bootstrap", () => ({
  BootstrapError: class BootstrapError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  fetchBootstrap: vi.fn().mockResolvedValue({
    token: "tok",
    ws_path: "/",
    expires_in: 300,
  }),
  deriveWsUrl: vi.fn(() => "ws://test"),
}));

vi.mock("@/lib/nanobot-client", () => {
  class MockClient {
    status = "idle" as const;
    defaultChatId: string | null = null;
    connect = connectSpy;
    onStatus = () => () => {};
    onError = () => () => {};
    onChat = () => () => {};
    onWork = () => () => {};
    sendMessage = vi.fn();
    sendWorkMessage = vi.fn();
    cancelWork = vi.fn();
    newChat = vi.fn();
    attach = vi.fn();
    close = vi.fn();
    updateUrl = vi.fn();

    constructor(options: { onReauth?: () => Promise<string | null> }) {
      clientOptions = options;
    }
  }

  return { NanobotClient: MockClient };
});

import App from "@/App";
import { fetchBootstrap } from "@/lib/bootstrap";

describe("App layout", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
    mockSessions = [];
    mockSignedIn = true;
    connectSpy.mockClear();
    refreshSpy.mockReset();
    createChatSpy.mockClear();
    deleteChatSpy.mockReset();
    clientOptions = null;
    vi.mocked(fetchBootstrap).mockReset().mockResolvedValue({
      token: "tok",
      ws_path: "/",
      expires_in: 300,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
      }),
    );
  });

  it("shows Clerk account actions before connecting a signed-out user", () => {
    mockSignedIn = false;

    render(<App />);

    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create account" })).toBeInTheDocument();
    expect(connectSpy).not.toHaveBeenCalled();
  });

  it("keeps sidebar layout out of the main thread width contract", async () => {
    const { container } = render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    const main = container.querySelector("main");
    expect(main).toBeInTheDocument();
    expect(main).not.toHaveAttribute("style");

    const asideClassNames = Array.from(container.querySelectorAll("aside")).map(
      (el) => el.className,
    );
    expect(asideClassNames.some((cls) => cls.includes("lg:block"))).toBe(true);
  });

  it("switches to the next session when deleting the active chat", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^First chat$/ })).toBeInTheDocument(),
    );

    fireEvent.pointerDown(screen.getByLabelText("Chat actions for First chat"), {
      button: 0,
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));

    await waitFor(() =>
      expect(screen.getByText('Delete “First chat”?')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(deleteChatSpy).toHaveBeenCalledWith("websocket:chat-a"),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /^Second chat$/ }),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText('Delete “First chat”?')).not.toBeInTheDocument();
    expect(document.body.style.pointerEvents).not.toBe("none");
  }, 15_000);

  it("opens the Cursor-style settings view from the sidebar", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/settings")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              agent: {
                model: "openai/gpt-4o",
                provider: "auto",
                resolved_provider: "openai",
                has_api_key: true,
              },
              providers: [
                { name: "auto", label: "Auto" },
                { name: "openai", label: "OpenAI" },
              ],
              runtime: {
                config_path: "/tmp/config.json",
              },
              requires_restart: false,
            }),
          };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));

    expect(await screen.findByRole("heading", { name: "General" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "AI" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("openai/gpt-4o")).toBeInTheDocument();
  });

  it("opens tenant-scoped Work from the sidebar", async () => {
    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Work" }));

    expect(
      await screen.findByRole("heading", { name: "Work" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/background tasks/i)).toBeInTheDocument();
  });

  it("uses a refreshed bootstrap token for later REST requests", async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ tasks: [] }),
    });
    vi.stubGlobal("fetch", fetchSpy);
    vi.mocked(fetchBootstrap)
      .mockResolvedValueOnce({ token: "initial", ws_path: "/", expires_in: 300 })
      .mockResolvedValueOnce({ token: "refreshed", ws_path: "/", expires_in: 300 });

    render(<App />);
    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    await act(async () => {
      await clientOptions?.onReauth?.();
    });
    fireEvent.click(screen.getByRole("button", { name: "Work" }));

    await waitFor(() => {
      const workRequest = fetchSpy.mock.calls.find(([input]) =>
        String(input).endsWith("/api/work"),
      );
      expect(workRequest?.[1]?.headers).toMatchObject({
        Authorization: "Bearer refreshed",
      });
    });
  });

  it("refreshes bootstrap credentials before expiry and retries without reconnecting", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(fetchBootstrap)
        .mockResolvedValueOnce({ token: "initial", ws_path: "/", expires_in: 31 })
        .mockRejectedValueOnce(new Error("temporary bootstrap failure"))
        .mockResolvedValueOnce({ token: "renewed", ws_path: "/", expires_in: 300 });

      render(<App />);
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(connectSpy).toHaveBeenCalledTimes(1);
      expect(fetchBootstrap).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      expect(fetchBootstrap).toHaveBeenCalledTimes(2);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(fetchBootstrap).toHaveBeenCalledTimes(3);
      expect(connectSpy).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
