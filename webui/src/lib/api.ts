import type {
  ActivityItem,
  ActivityStatus,
  ChatSummary,
  SettingsPayload,
  SettingsUpdate,
  ModelRuntime,
  ModelSwitchTarget,
  WorkArtifact,
  WorkEventItem,
  WorkStatus,
  WorkTask,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  url: string,
  token: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(url, {
    ...(init ?? {}),
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${token}`,
    },
    credentials: "same-origin",
  });
  if (!res.ok) {
    throw new ApiError(res.status, `HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

function splitKey(key: string): { channel: string; chatId: string } {
  const idx = key.indexOf(":");
  if (idx === -1) return { channel: "", chatId: key };
  return { channel: key.slice(0, idx), chatId: key.slice(idx + 1) };
}

export async function listSessions(
  token: string,
  base: string = "",
): Promise<ChatSummary[]> {
  type Row = {
    key: string;
    created_at: string | null;
    updated_at: string | null;
    preview?: string;
  };
  const body = await request<{ sessions: Row[] }>(
    `${base}/api/sessions`,
    token,
  );
  return body.sessions.map((s) => ({
    key: s.key,
    ...splitKey(s.key),
    createdAt: s.created_at,
    updatedAt: s.updated_at,
    preview: s.preview ?? "",
  }));
}

/** Signed image URL attached to a historical user message. The server
 * emits these in place of raw on-disk paths so the client can render
 * previews without learning where media lives on disk. Each URL is a
 * self-authenticating ``/api/media/...`` route (see backend
 * ``_sign_media_path``) safe to drop into an ``<img src>`` attribute. */
export interface SessionMediaUrl {
  url: string;
  name?: string;
}

export async function fetchSessionMessages(
  token: string,
  key: string,
  base: string = "",
): Promise<{
  key: string;
  created_at: string | null;
  updated_at: string | null;
  messages: Array<{
    role: string;
    content: string;
    timestamp?: string;
    tool_calls?: unknown;
    tool_call_id?: string;
    name?: string;
    /** Present on ``user`` turns that attached images. Paths have already
     * been stripped server-side; only the signed fetch URLs survive. */
    media_urls?: SessionMediaUrl[];
  }>;
}> {
  return request(
    `${base}/api/sessions/${encodeURIComponent(key)}/messages`,
    token,
  );
}

export async function deleteSession(
  token: string,
  key: string,
  base: string = "",
): Promise<boolean> {
  const body = await request<{ deleted: boolean }>(
    `${base}/api/sessions/${encodeURIComponent(key)}/delete`,
    token,
  );
  return body.deleted;
}

function normalizeActivityStatus(value: string | undefined): ActivityStatus {
  return value === "active" || value === "waiting" ? value : "idle";
}

export async function fetchActivity(
  token: string,
  base: string = "",
): Promise<ActivityItem[]> {
  type Row = {
    key: string;
    chat_id: string;
    created_at: string | null;
    updated_at: string | null;
    preview?: string;
    status?: string;
    live?: boolean;
    message_count?: number;
    last_role?: string | null;
    last_text?: string;
  };
  const body = await request<{ activity: Row[] }>(
    `${base}/api/activity`,
    token,
  );
  return body.activity.map((item) => ({
    key: item.key,
    chatId: item.chat_id,
    createdAt: item.created_at,
    updatedAt: item.updated_at,
    preview: item.preview ?? "",
    status: normalizeActivityStatus(item.status),
    live: Boolean(item.live),
    messageCount: item.message_count ?? 0,
    lastRole: item.last_role ?? null,
    lastText: item.last_text ?? "",
  }));
}

function normalizeWorkStatus(value: string | undefined): WorkStatus {
  switch (value) {
    case "scheduled":
    case "queued":
    case "running":
    case "waiting":
    case "succeeded":
    case "failed":
    case "cancelled":
    case "interrupted":
      return value;
    default:
      return "interrupted";
  }
}

function normalizeWorkTask(raw: Record<string, unknown>): WorkTask {
  return {
    task_id: String(raw.task_id ?? ""),
    scope: String(raw.scope ?? ""),
    session_key: String(raw.session_key ?? ""),
    chat_id: String(raw.chat_id ?? ""),
    title: String(raw.title ?? ""),
    prompt_preview: String(raw.prompt_preview ?? ""),
    status: normalizeWorkStatus(String(raw.status ?? "")),
    mode: String(raw.mode ?? ""),
    model: String(raw.model ?? ""),
    created_at: String(raw.created_at ?? ""),
    updated_at: String(raw.updated_at ?? ""),
    started_at: (raw.started_at as string | null | undefined) ?? null,
    completed_at: (raw.completed_at as string | null | undefined) ?? null,
    last_seq: Number(raw.last_seq ?? 0),
    result_summary: (raw.result_summary as string | null | undefined) ?? null,
    error: (raw.error as string | null | undefined) ?? null,
    artifact_count: Number(raw.artifact_count ?? 0),
    steps: Array.isArray(raw.steps) ? raw.steps as WorkTask["steps"] : undefined,
    artifacts: Array.isArray(raw.artifacts)
      ? raw.artifacts as WorkArtifact[]
      : undefined,
  };
}

export async function fetchWorkTasks(
  token: string,
  base: string = "",
): Promise<WorkTask[]> {
  const body = await request<{ tasks: Array<Record<string, unknown>> }>(
    `${base}/api/work`,
    token,
  );
  return body.tasks.map(normalizeWorkTask);
}

export async function fetchWorkTask(
  token: string,
  taskId: string,
  base: string = "",
): Promise<WorkTask> {
  const body = await request<{ task: Record<string, unknown> }>(
    `${base}/api/work/${encodeURIComponent(taskId)}`,
    token,
  );
  return normalizeWorkTask(body.task);
}

export async function fetchWorkEvents(
  token: string,
  taskId: string,
  afterSeq: number = 0,
  base: string = "",
): Promise<WorkEventItem[]> {
  const body = await request<{ events: WorkEventItem[] }>(
    `${base}/api/work/${encodeURIComponent(taskId)}/events?after_seq=${afterSeq}`,
    token,
  );
  return body.events;
}

export async function fetchSettings(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(`${base}/api/settings`, token);
}

export async function updateSettings(
  token: string,
  update: SettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (update.model !== undefined) query.set("model", update.model);
  if (update.provider !== undefined) query.set("provider", update.provider);
  return request<SettingsPayload>(`${base}/api/settings/update?${query}`, token);
}

export async function fetchModelStatus(
  token: string,
  base: string = "",
): Promise<ModelRuntime> {
  const body = await request<{ model_runtime: ModelRuntime }>(
    `${base}/api/model/status`,
    token,
  );
  return body.model_runtime;
}

export async function switchModel(
  token: string,
  target: ModelSwitchTarget,
  base: string = "",
): Promise<ModelRuntime> {
  const body = await request<{ model_runtime: ModelRuntime }>(
    `${base}/api/model/switch?target=${encodeURIComponent(target)}`,
    token,
  );
  return body.model_runtime;
}
