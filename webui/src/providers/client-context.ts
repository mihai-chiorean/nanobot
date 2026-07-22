import { createContext, useContext } from "react";

import type { NanobotClient } from "@/lib/nanobot-client";

export interface ClientContextValue {
  client: NanobotClient;
  token: string;
  modelName: string | null;
}

export const ClientContext = createContext<ClientContextValue | null>(null);

export function useClient(): ClientContextValue {
  const ctx = useContext(ClientContext);
  if (!ctx) {
    throw new Error("useClient must be used within a ClientProvider");
  }
  return ctx;
}
