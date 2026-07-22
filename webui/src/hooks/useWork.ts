import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, fetchWorkTask, fetchWorkTasks } from "@/lib/api";
import type { WorkTask } from "@/lib/types";
import { useClient } from "@/providers/client-context";

export function useWork(): {
  tasks: WorkTask[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  loadTask: (taskId: string) => Promise<WorkTask | null>;
} {
  const { token } = useClient();
  const tokenRef = useRef(token);
  const [tasks, setTasks] = useState<WorkTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  tokenRef.current = token;

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      setTasks(await fetchWorkTasks(tokenRef.current));
      setError(null);
    } catch (err) {
      const msg =
        err instanceof ApiError ? `HTTP ${err.status}` : (err as Error).message;
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadTask = useCallback(async (taskId: string) => {
    try {
      return await fetchWorkTask(tokenRef.current, taskId);
    } catch (err) {
      const msg =
        err instanceof ApiError ? `HTTP ${err.status}` : (err as Error).message;
      setError(msg);
      return null;
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { tasks, loading, error, refresh, loadTask };
}
