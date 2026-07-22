import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { AskUserPrompt } from "@/components/thread/AskUserPrompt";
import { ThreadComposer } from "@/components/thread/ThreadComposer";
import { ThreadHeader } from "@/components/thread/ThreadHeader";
import { StreamErrorNotice } from "@/components/thread/StreamErrorNotice";
import { ThreadViewport } from "@/components/thread/ThreadViewport";
import { useNanobotStream, type SendImage } from "@/hooks/useNanobotStream";
import { useSessionHistory } from "@/hooks/useSessions";
import { randomId } from "@/lib/id";
import type { ChatSummary, UIMessage } from "@/lib/types";
import { useClient } from "@/providers/client-context";

interface ThreadShellProps {
  session: ChatSummary | null;
  title: string;
  onToggleSidebar: () => void;
  onGoHome: () => void;
  onNewChat: () => Promise<string | null>;
  onSessionPreview?: (key: string, preview: string) => void;
  hideSidebarToggleOnDesktop?: boolean;
}

function toModelBadgeLabel(modelName: string | null): string | null {
  if (!modelName) return null;
  const trimmed = modelName.trim();
  if (!trimmed) return null;
  const leaf = trimmed.split("/").pop() ?? trimmed;
  return leaf || trimmed;
}

export function ThreadShell({
  session,
  title,
  onToggleSidebar,
  onGoHome,
  onNewChat,
  onSessionPreview = () => {},
  hideSidebarToggleOnDesktop = false,
}: ThreadShellProps) {
  const { t } = useTranslation();
  const chatId = session?.chatId ?? null;
  const historyKey = session?.key ?? null;
  const { messages: historical, loading } = useSessionHistory(historyKey);
  const { client, modelName } = useClient();
  const [booting, setBooting] = useState(false);
  const pendingFirstRef = useRef<string | null>(null);
  const pendingBackgroundRef = useRef<{
    content: string;
    images?: SendImage[];
  } | null>(null);
  const messageCacheRef = useRef<Map<string, UIMessage[]>>(new Map());

  const initial = useMemo(() => {
    if (!chatId) return historical;
    return messageCacheRef.current.get(chatId) ?? historical;
  }, [chatId, historical]);
  const {
    messages,
    isStreaming,
    send,
    setMessages,
    streamError,
    dismissStreamError,
  } = useNanobotStream(chatId, initial);
  const showHeroComposer = messages.length === 0 && !loading;
  const pendingAsk = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message.kind === "trace") continue;
      if (message.role === "user") return null;
      if (message.role === "assistant" && message.buttons?.some((row) => row.length > 0)) {
        return {
          question: message.content,
          buttons: message.buttons,
        };
      }
      if (message.role === "assistant") return null;
    }
    return null;
  }, [messages]);

  useEffect(() => {
    if (!chatId || loading) return;
    const cached = messageCacheRef.current.get(chatId);
    // When the user switches away and back, keep the local in-memory thread
    // state (including not-yet-persisted messages) instead of replacing it with
    // whatever the history endpoint currently knows about.
    setMessages(cached && cached.length > 0 ? cached : historical);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, chatId, historical]);

  useEffect(() => {
    if (chatId) return;
    setMessages(historical);
  }, [chatId, historical, setMessages]);

  useEffect(() => {
    if (!chatId) return;
    messageCacheRef.current.set(chatId, messages);
  }, [chatId, messages]);

  useEffect(() => {
    if (!chatId) return;
    const pending = pendingFirstRef.current;
    if (!pending) return;
    pendingFirstRef.current = null;
    if (session) {
      onSessionPreview(session.key, pending);
    }
    client.sendMessage(chatId, pending);
    setMessages((prev) => [
      ...prev,
      {
        id: randomId(),
        role: "user",
        content: pending,
        createdAt: Date.now(),
      },
    ]);
    setBooting(false);
  }, [chatId, client, onSessionPreview, session, setMessages]);

  useEffect(() => {
    if (!chatId) return;
    const pending = pendingBackgroundRef.current;
    if (!pending) return;
    pendingBackgroundRef.current = null;
    if (session) {
      onSessionPreview(session.key, pending.content);
    }
    client.createWork(
      chatId,
      pending.content,
      pending.images?.map((img) => img.media),
    );
    setMessages((prev) => [
      ...prev,
      {
        id: randomId(),
        role: "tool",
        kind: "trace",
        traceKind: "progress",
        content: "Started background work. Open Work to track progress.",
        traces: ["Started background work. Open Work to track progress."],
        createdAt: Date.now(),
      },
    ]);
    setBooting(false);
  }, [chatId, client, onSessionPreview, session, setMessages]);

  const handleWelcomeSend = useCallback(
    async (content: string) => {
      if (booting) return;
      setBooting(true);
      pendingFirstRef.current = content;
      const newId = await onNewChat();
      if (!newId) {
        pendingFirstRef.current = null;
        setBooting(false);
      } else {
        onSessionPreview(`websocket:${newId}`, content);
      }
    },
    [booting, onNewChat, onSessionPreview],
  );

  const handleWelcomeBackground = useCallback(
    async (content: string, images?: SendImage[]) => {
      if (booting) return;
      setBooting(true);
      pendingBackgroundRef.current = { content, images };
      const newId = await onNewChat();
      if (!newId) {
        pendingBackgroundRef.current = null;
        setBooting(false);
      } else {
        onSessionPreview(`websocket:${newId}`, content);
      }
    },
    [booting, onNewChat, onSessionPreview],
  );

  const handleSend = useCallback(
    (content: string, images?: SendImage[]) => {
      if (session) {
        onSessionPreview(session.key, content);
      }
      send(content, images);
    },
    [onSessionPreview, send, session],
  );

  const handleSendBackground = useCallback(
    (content: string, images?: SendImage[]) => {
      if (!session || !chatId) return;
      onSessionPreview(session.key, content);
      client.createWork(
        chatId,
        content,
        images?.map((img) => img.media),
      );
      setMessages((prev) => [
        ...prev,
        {
          id: randomId(),
          role: "tool",
          kind: "trace",
          traceKind: "progress",
          content: "Started background work. Open Work to track progress.",
          traces: ["Started background work. Open Work to track progress."],
          createdAt: Date.now(),
        },
      ]);
    },
    [chatId, client, onSessionPreview, session, setMessages],
  );

  const emptyState = loading ? (
    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
      {t("thread.loadingConversation")}
    </div>
  ) : (
    <div className="flex w-full max-w-[40rem] flex-col gap-2 text-left animate-in fade-in-0 slide-in-from-bottom-2 duration-500">
      <div className="inline-flex items-center gap-2 text-[11px] font-medium text-muted-foreground">
        <img
          src="/brand/ziggy_icon.png"
          alt=""
          aria-hidden
          draggable={false}
          className="h-4 w-4 rounded-sm opacity-90"
        />
        <span className="text-foreground/82">Ziggy</span>
      </div>
      <p className="max-w-[28rem] text-[13px] leading-6 text-muted-foreground">
        {t("thread.empty.description")}
      </p>
    </div>
  );

  return (
    <section className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
      <ThreadHeader
        title={title}
        onToggleSidebar={onToggleSidebar}
        onGoHome={onGoHome}
        hideSidebarToggleOnDesktop={hideSidebarToggleOnDesktop}
      />
      <ThreadViewport
        messages={messages}
        isStreaming={isStreaming}
        emptyState={emptyState}
        composer={
          <>
            {streamError ? (
              <StreamErrorNotice
                error={streamError}
                onDismiss={dismissStreamError}
              />
            ) : null}
            {pendingAsk ? (
              <AskUserPrompt
                question={pendingAsk.question}
                buttons={pendingAsk.buttons}
                onAnswer={handleSend}
              />
            ) : null}
            {session ? (
              <ThreadComposer
                onSend={handleSend}
                onSendBackground={handleSendBackground}
                disabled={!chatId}
                placeholder={
                  showHeroComposer
                    ? t("thread.composer.placeholderHero")
                    : t("thread.composer.placeholderThread")
                }
                modelLabel={toModelBadgeLabel(modelName)}
                variant={showHeroComposer ? "hero" : "thread"}
              />
            ) : (
              <ThreadComposer
                onSend={handleWelcomeSend}
                onSendBackground={handleWelcomeBackground}
                disabled={booting}
                placeholder={
                  booting
                    ? t("thread.composer.placeholderOpening")
                    : t("thread.composer.placeholderHero")
                }
                modelLabel={toModelBadgeLabel(modelName)}
                variant="hero"
              />
            )}
          </>
        }
      />
    </section>
  );
}
