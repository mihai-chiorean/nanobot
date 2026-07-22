import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Composer } from "@/components/Composer";
import { MessageList } from "@/components/MessageList";
import { useClient } from "@/providers/ClientProvider";
import { useNanobotStream } from "@/hooks/useNanobotStream";
import { useSessionHistory } from "@/hooks/useSessions";
import { randomId } from "@/lib/id";
import type { ChatSummary } from "@/lib/types";

interface ChatPaneProps {
  session: ChatSummary | null;
  /** Provision a new chat and mark it active. Returns the new chat_id or null. */
  onNewChat: () => Promise<string | null>;
}

/**
 * The chat surface: persisted history on top, live stream below, composer
 * pinned at the bottom. When no session is active we render a centered
 * welcome card with a fully-functional composer — typing a first message
 * quietly provisions a new chat and routes the message through.
 */
export function ChatPane({ session, onNewChat }: ChatPaneProps) {
  const chatId = session?.chatId ?? null;
  const historyKey = session?.key ?? null;
  const { messages: historical, loading } = useSessionHistory(historyKey);
  const { client } = useClient();
  const [booting, setBooting] = useState(false);
  const pendingFirstRef = useRef<string | null>(null);

  const initial = useMemo(() => historical, [historical]);
  const { messages, isStreaming, send, setMessages } = useNanobotStream(
    chatId,
    initial,
  );

  useEffect(() => {
    if (!loading && chatId) setMessages(historical);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, chatId, historical]);

  // Once a session becomes active, flush any first-message stashed from the
  // welcome composer so the user's keystroke "just sends".
  useEffect(() => {
    if (!chatId) return;
    const pending = pendingFirstRef.current;
    if (!pending) return;
    pendingFirstRef.current = null;
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
  }, [chatId, client, setMessages]);

  const handleWelcomeSend = useCallback(
    async (content: string) => {
      if (booting) return;
      setBooting(true);
      pendingFirstRef.current = content;
      const newId = await onNewChat();
      if (!newId) {
        // Creation failed — release the lock so the user can retry.
        pendingFirstRef.current = null;
        setBooting(false);
      }
    },
    [booting, onNewChat],
  );

  if (!session) {
    return (
      <section className="flex min-h-0 flex-1 flex-col">
        <div className="flex flex-1 flex-col items-center justify-center gap-8 px-4 pb-6">
          <div className="flex flex-col items-center gap-4 animate-in fade-in-0 slide-in-from-bottom-2 duration-500">
            <div className="flex items-center gap-3">
              <img
                src="/brand/ziggy_icon.png"
                alt=""
                className="h-11 w-11 select-none rounded-xl drop-shadow-sm"
                aria-hidden
                draggable={false}
              />
              <div className="text-left">
                <div className="text-2xl font-semibold leading-7">Ziggy</div>
                <div className="text-xs font-medium text-muted-foreground">
                  private agent workspace
                </div>
              </div>
            </div>
            <h1 className="text-xl font-medium tracking-tight text-foreground/90">
              What's on your mind?
            </h1>
            <p className="max-w-md text-center text-sm text-muted-foreground">
              Your conversations stay in the private workspace on Spark. Start
              typing and I'll open a new chat.
            </p>
          </div>
          <div className="w-full animate-in fade-in-0 slide-in-from-bottom-2 duration-500">
            <Composer
              compact
              disabled={booting}
              onSend={handleWelcomeSend}
              placeholder={
                booting ? "Opening a new chat…" : "Type your message…"
              }
            />
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="relative flex min-h-0 flex-1 flex-col">
      <MessageList messages={messages} isStreaming={isStreaming} />
      <Composer
        onSend={send}
        disabled={!chatId}
        placeholder="Type your message…"
      />
    </section>
  );
}
