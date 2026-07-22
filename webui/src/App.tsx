import {
  Component,
  type FormEvent,
  type ErrorInfo,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { DeleteConfirm } from "@/components/DeleteConfirm";
import { Sidebar } from "@/components/Sidebar";
import { ActivityView } from "@/components/activity/ActivityView";
import { SettingsView } from "@/components/settings/SettingsView";
import { ThreadShell } from "@/components/thread/ThreadShell";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { preloadMarkdownText } from "@/components/MarkdownText";
import { useSessions } from "@/hooks/useSessions";
import { useTheme } from "@/hooks/useTheme";
import { cn } from "@/lib/utils";
import {
  BootstrapError,
  deriveWsUrl,
  fetchBootstrap,
  joinGuest,
} from "@/lib/bootstrap";
import { shortChatId } from "@/lib/format";
import { NanobotClient } from "@/lib/nanobot-client";
import { ClientProvider, useClient } from "@/providers/ClientProvider";
import type { ChatSummary } from "@/lib/types";

type BootState =
  | { status: "loading" }
  | { status: "join"; message?: string }
  | { status: "owner"; message?: string }
  | { status: "error"; message: string }
  | {
      status: "ready";
      client: NanobotClient;
      token: string;
      modelName: string | null;
      access: "owner" | "guest";
      guestCode: string | null;
    };

const SIDEBAR_STORAGE_KEY = "nanobot-webui.sidebar";
const OWNER_CODE_STORAGE_KEY = "ziggy.ownerCode";
const SIDEBAR_WIDTH = 279;
type ShellView = "chat" | "activity" | "settings";

function readGuestCode(): string | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  const code = params.get("guest") ?? params.get("invite");
  const trimmed = code?.trim();
  return trimmed || null;
}

function shouldShowJoinFirst(): boolean {
  if (typeof window === "undefined") return false;
  return window.location.pathname === "/join";
}

function isOwnerPath(): boolean {
  if (typeof window === "undefined") return false;
  return window.location.pathname === "/owner";
}

function shouldRedirectRootToOwner(guestCode: string | null): boolean {
  if (typeof window === "undefined") return false;
  return window.location.pathname === "/" && !guestCode;
}

function readOwnerCode(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(OWNER_CODE_STORAGE_KEY);
  } catch {
    return null;
  }
}

interface ErrorBoundaryState {
  error: Error | null;
}

class ErrorBoundary extends Component<
  { children: ReactNode },
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Nanobot WebUI render error", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex h-full w-full items-center justify-center px-4 text-center">
          <div className="flex max-w-md flex-col items-center gap-3">
            <img
              src="/brand/ziggy_icon.png"
              alt=""
              className="h-10 w-10 opacity-60 grayscale select-none"
              aria-hidden
              draggable={false}
            />
            <p className="text-lg font-semibold">Something went wrong</p>
            <p className="text-sm text-muted-foreground">
              {this.state.error.message}
            </p>
            <button
              type="button"
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
              onClick={() => this.setState({ error: null })}
            >
              Try again
            </button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}

function readSidebarOpen(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const raw = window.localStorage.getItem(SIDEBAR_STORAGE_KEY);
    if (raw === null) return true;
    return raw === "1";
  } catch {
    return true;
  }
}

export default function App() {
  const { t } = useTranslation();
  const [state, setState] = useState<BootState>({ status: "loading" });
  const guestCodeRef = useRef<string | null>(readGuestCode());
  const ownerCodeRef = useRef<string | null>(readOwnerCode());

  const boot = useCallback(async (guestCode: string | null, ownerCode?: string | null) => {
    const bootResponse = await fetchBootstrap("", guestCode, ownerCode);
    const url = deriveWsUrl(bootResponse.ws_path, bootResponse.token);
    const client = new NanobotClient({
      url,
      onReauth: async () => {
        try {
          const refreshed = await fetchBootstrap("", guestCode, ownerCode);
          return deriveWsUrl(refreshed.ws_path, refreshed.token);
        } catch {
          return null;
        }
      },
    });
    client.connect();
    setState({
      status: "ready",
      client,
      token: bootResponse.token,
      modelName: bootResponse.model_name ?? null,
      access: bootResponse.access === "guest" ? "guest" : "owner",
      guestCode: bootResponse.guest_code ?? guestCode,
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        if (shouldShowJoinFirst() && !guestCodeRef.current) {
          if (!cancelled) setState({ status: "join" });
          return;
        }
        const guestCode = guestCodeRef.current;
        if (shouldRedirectRootToOwner(guestCode)) {
          window.location.replace("/owner");
          return;
        }
        await boot(guestCode, guestCode ? null : ownerCodeRef.current);
      } catch (e) {
        if (cancelled) return;
        if (
          isOwnerPath() &&
          e instanceof BootstrapError &&
          (e.status === 401 || e.status === 403)
        ) {
          setState({ status: "owner" });
          return;
        }
        if (
          !guestCodeRef.current &&
          !isOwnerPath() &&
          e instanceof BootstrapError &&
          (e.status === 401 || e.status === 403)
        ) {
          setState({ status: "join" });
          return;
        }
        setState({ status: "error", message: (e as Error).message });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [boot]);

  useEffect(() => {
    const warm = () => preloadMarkdownText();
    const win = globalThis as typeof globalThis & {
      requestIdleCallback?: (
        callback: IdleRequestCallback,
        options?: IdleRequestOptions,
      ) => number;
      cancelIdleCallback?: (handle: number) => void;
    };
    if (typeof win.requestIdleCallback === "function") {
      const id = win.requestIdleCallback(warm, { timeout: 1500 });
      return () => win.cancelIdleCallback?.(id);
    }
    const id = globalThis.setTimeout(warm, 250);
    return () => globalThis.clearTimeout(id);
  }, []);

  if (state.status === "loading") {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="flex flex-col items-center gap-3 animate-in fade-in-0 duration-300">
          <img
            src="/brand/ziggy_icon.png"
            alt=""
            className="h-10 w-10 animate-pulse select-none"
            aria-hidden
            draggable={false}
          />
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-foreground/40" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-foreground/60" />
            </span>
            {t("app.loading.connecting")}
          </div>
        </div>
      </div>
    );
  }
  if (state.status === "error") {
    return (
      <div className="flex h-full w-full items-center justify-center px-4 text-center">
        <div className="flex max-w-md flex-col items-center gap-3">
          <img
            src="/brand/ziggy_icon.png"
            alt=""
            className="h-10 w-10 opacity-60 grayscale select-none"
            aria-hidden
            draggable={false}
          />
          <p className="text-lg font-semibold">{t("app.error.title")}</p>
          <p className="text-sm text-muted-foreground">{state.message}</p>
          <p className="text-xs text-muted-foreground">
            {t("app.error.gatewayHint")}
          </p>
        </div>
      </div>
    );
  }
  if (state.status === "join") {
    return (
      <JoinView
        message={state.message}
        onJoin={async (email, invite) => {
          const joined = await joinGuest(email, invite);
          guestCodeRef.current = joined.code;
          if (typeof window !== "undefined") {
            window.history.replaceState(null, "", joined.guest_url);
          }
          await boot(joined.code, null);
        }}
      />
    );
  }
  if (state.status === "owner") {
    return (
      <OwnerView
        message={state.message}
        onUnlock={async (code) => {
          ownerCodeRef.current = code;
          if (typeof window !== "undefined") {
            window.sessionStorage.setItem(OWNER_CODE_STORAGE_KEY, code);
          }
          await boot(null, code);
        }}
      />
    );
  }

  const handleModelNameChange = (modelName: string | null) => {
    setState((current) =>
      current.status === "ready" ? { ...current, modelName } : current,
    );
  };

  return (
    <ClientProvider
      client={state.client}
      token={state.token}
      modelName={state.modelName}
      access={state.access}
      guestCode={state.guestCode}
    >
      <ErrorBoundary>
        <Shell onModelNameChange={handleModelNameChange} />
      </ErrorBoundary>
    </ClientProvider>
  );
}

function JoinView({
  message,
  onJoin,
}: {
  message?: string;
  onJoin: (email: string, invite: string) => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [invite, setInvite] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(message ?? null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onJoin(email, invite);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full w-full items-center justify-center bg-background px-4">
      <form
        onSubmit={submit}
        className="flex w-full max-w-sm flex-col gap-4 rounded-lg border border-border bg-background p-5 shadow-sm"
      >
        <div className="flex items-center gap-3">
          <img
            src="/brand/ziggy_icon.png"
            alt=""
            className="h-9 w-9 select-none"
            aria-hidden
            draggable={false}
          />
          <div>
            <h1 className="text-base font-semibold">Join Ziggy</h1>
            <p className="text-sm text-muted-foreground">Use your email to keep your chat.</p>
          </div>
        </div>
        <label className="flex flex-col gap-1.5 text-sm font-medium">
          Email
          <input
            type="email"
            required
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="h-10 rounded-md border border-input bg-background px-3 text-sm outline-none ring-offset-background focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm font-medium">
          Access code
          <input
            type="text"
            autoComplete="off"
            value={invite}
            onChange={(event) => setInvite(event.target.value)}
            className="h-10 rounded-md border border-input bg-background px-3 text-sm outline-none ring-offset-background focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <button
          type="submit"
          disabled={busy}
          className="h-10 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busy ? "Joining..." : "Continue"}
        </button>
      </form>
    </div>
  );
}

function OwnerView({
  message,
  onUnlock,
}: {
  message?: string;
  onUnlock: (code: string) => Promise<void>;
}) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(message ?? null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onUnlock(code);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full w-full items-center justify-center bg-background px-4">
      <form
        onSubmit={submit}
        className="flex w-full max-w-sm flex-col gap-4 rounded-lg border border-border bg-background p-5 shadow-sm"
      >
        <div className="flex items-center gap-3">
          <img
            src="/brand/ziggy_icon.png"
            alt=""
            className="h-9 w-9 select-none"
            aria-hidden
            draggable={false}
          />
          <div>
            <h1 className="text-base font-semibold">Ziggy Owner</h1>
            <p className="text-sm text-muted-foreground">
              Enter the owner access code.
            </p>
          </div>
        </div>
        <label className="flex flex-col gap-1.5 text-sm font-medium">
          Owner code
          <input
            type="password"
            required
            autoComplete="current-password"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            className="h-10 rounded-md border border-input bg-background px-3 text-sm outline-none ring-offset-background focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <button
          type="submit"
          disabled={busy}
          className="h-10 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busy ? "Unlocking..." : "Unlock"}
        </button>
      </form>
    </div>
  );
}

function Shell({ onModelNameChange }: { onModelNameChange: (modelName: string | null) => void }) {
  const { t, i18n } = useTranslation();
  const { theme, toggle } = useTheme();
  const clientContext = useClient();
  const isGuest = clientContext.access === "guest";
  const {
    sessions,
    loading,
    refresh,
    createChat,
    deleteChat,
    updateSessionPreview,
  } = useSessions();
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [view, setView] = useState<ShellView>("chat");
  const [desktopSidebarOpen, setDesktopSidebarOpen] =
    useState<boolean>(readSidebarOpen);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<{
    key: string;
    label: string;
  } | null>(null);
  const lastSessionsLen = useRef(0);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        SIDEBAR_STORAGE_KEY,
        desktopSidebarOpen ? "1" : "0",
      );
    } catch {
      // ignore storage errors (private mode, etc.)
    }
  }, [desktopSidebarOpen]);

  useEffect(() => {
    if (activeKey) return;
    if (sessions.length > 0 && lastSessionsLen.current === 0) {
      setActiveKey(sessions[0].key);
    }
    lastSessionsLen.current = sessions.length;
  }, [sessions, activeKey]);

  const activeSession = useMemo<ChatSummary | null>(() => {
    if (!activeKey) return null;
    return sessions.find((s) => s.key === activeKey) ?? null;
  }, [sessions, activeKey]);

  const closeDesktopSidebar = useCallback(() => {
    setDesktopSidebarOpen(false);
  }, []);

  const closeMobileSidebar = useCallback(() => {
    setMobileSidebarOpen(false);
  }, []);

  const toggleSidebar = useCallback(() => {
    const isDesktop =
      typeof window !== "undefined" &&
      window.matchMedia("(min-width: 1024px)").matches;
    if (isDesktop) {
      setDesktopSidebarOpen((v) => !v);
    } else {
      setMobileSidebarOpen((v) => !v);
    }
  }, []);

  const onNewChat = useCallback(async () => {
    try {
      const chatId = await createChat();
      setActiveKey(`websocket:${chatId}`);
      setView("chat");
      setMobileSidebarOpen(false);
      return chatId;
    } catch (e) {
      console.error("Failed to create chat", e);
      return null;
    }
  }, [createChat]);

  const onSelectChat = useCallback(
    (key: string) => {
      setActiveKey(key);
      setView("chat");
      setMobileSidebarOpen(false);
    },
    [],
  );

  const onConfirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const key = pendingDelete.key;
    const deletingActive = activeKey === key;
    const currentIndex = sessions.findIndex((s) => s.key === key);
    const fallbackKey = deletingActive
      ? (sessions[currentIndex + 1]?.key ?? sessions[currentIndex - 1]?.key ?? null)
      : activeKey;
    setPendingDelete(null);
    if (deletingActive) setActiveKey(fallbackKey);
    try {
      await deleteChat(key);
    } catch (e) {
      if (deletingActive) setActiveKey(key);
      console.error("Failed to delete session", e);
    }
  }, [pendingDelete, deleteChat, activeKey, sessions]);

  const headerTitle = activeSession
    ? activeSession.preview ||
      t("chat.fallbackTitle", { id: shortChatId(activeSession.chatId) })
    : t("app.brand");

  useEffect(() => {
    document.title = activeSession
      ? t("app.documentTitle.chat", { title: headerTitle })
      : t("app.documentTitle.base");
  }, [activeSession, headerTitle, i18n.resolvedLanguage, t]);

  const sidebarProps = {
    sessions,
    activeKey,
    loading,
    theme,
    onToggleTheme: toggle,
    onNewChat: () => {
      void onNewChat();
    },
    onSelect: onSelectChat,
    onRefresh: () => void refresh(),
    onRequestDelete: (key: string, label: string) =>
      setPendingDelete({ key, label }),
    activeView: view,
    guestMode: isGuest,
    onOpenActivity: () => {
      if (isGuest) return;
      setView("activity" as const);
      setMobileSidebarOpen(false);
    },
    onOpenSettings: () => {
      if (isGuest) return;
      setView("settings" as const);
      setMobileSidebarOpen(false);
    },
  };

  return (
    <div className="relative flex h-full w-full overflow-hidden">
      {/* Desktop sidebar: in normal flow, so the thread area width stays honest. */}
      <aside
        className={cn(
          "relative z-20 hidden shrink-0 overflow-hidden lg:block",
          "transition-[width] duration-300 ease-out",
        )}
        style={{ width: desktopSidebarOpen ? SIDEBAR_WIDTH : 0 }}
      >
        <div
          className={cn(
            "absolute inset-y-0 left-0 h-full w-[279px] overflow-hidden bg-sidebar shadow-inner-right",
            "transition-transform duration-300 ease-out",
            desktopSidebarOpen ? "translate-x-0" : "-translate-x-full",
          )}
        >
          <Sidebar {...sidebarProps} onCollapse={closeDesktopSidebar} />
        </div>
      </aside>

      <Sheet
        open={mobileSidebarOpen}
        onOpenChange={(open) => setMobileSidebarOpen(open)}
      >
        <SheetContent
          side="left"
          showCloseButton={false}
          className="w-[279px] p-0 sm:max-w-[279px] lg:hidden"
        >
          <Sidebar {...sidebarProps} onCollapse={closeMobileSidebar} />
        </SheetContent>
      </Sheet>

      <main className="flex h-full min-w-0 flex-1 flex-col">
        {view === "activity" && !isGuest ? (
          <ActivityView
            onBackToChat={() => setView("chat")}
            onOpenChat={(key) => {
              setActiveKey(key);
              setView("chat");
              setMobileSidebarOpen(false);
            }}
          />
        ) : view === "settings" && !isGuest ? (
          <SettingsView
            theme={theme}
            onToggleTheme={toggle}
            onBackToChat={() => setView("chat")}
            onModelNameChange={onModelNameChange}
          />
        ) : (
          <ThreadShell
            session={activeSession}
            title={headerTitle}
            onToggleSidebar={toggleSidebar}
            onGoHome={() => setActiveKey(null)}
            onNewChat={onNewChat}
            onSessionPreview={updateSessionPreview}
            hideSidebarToggleOnDesktop={desktopSidebarOpen}
          />
        )}
      </main>

      <DeleteConfirm
        open={!!pendingDelete}
        title={pendingDelete?.label ?? ""}
        onCancel={() => setPendingDelete(null)}
        onConfirm={onConfirmDelete}
      />
    </div>
  );
}
