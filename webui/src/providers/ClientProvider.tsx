import type { ReactNode } from "react";

import type { NanobotClient } from "@/lib/nanobot-client";
import { ClientContext } from "@/providers/client-context";

export function ClientProvider({
  client,
  token,
  modelName = null,
  children,
}: {
  client: NanobotClient;
  token: string;
  modelName?: string | null;
  children: ReactNode;
}) {
  return (
    <ClientContext.Provider value={{ client, token, modelName }}>
      {children}
    </ClientContext.Provider>
  );
}
