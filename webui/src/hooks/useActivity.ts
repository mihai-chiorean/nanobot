import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, fetchActivity } from "@/lib/api";
import type { ActivityItem } from "@/lib/types";
import { useClient } from "@/providers/client-context";

export function useActivity(): {
  activity: ActivityItem[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
} {
  const { token } = useClient();
  const tokenRef = useRef(token);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  tokenRef.current = token;

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      setActivity(await fetchActivity(tokenRef.current));
      setError(null);
    } catch (err) {
      const msg =
        err instanceof ApiError ? `HTTP ${err.status}` : (err as Error).message;
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { activity, loading, error, refresh };
}
