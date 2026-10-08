/** Transport notifications shared by the browser and terminal clients. */
export type RecoveryStatus = "resuming" | "awaiting_user" | "recovered" | "failed"

export interface RecoveryState {
  status: RecoveryStatus
  recovery_id: string
  reason?: string
  attempts?: number
  can_continue?: boolean
}

export interface ContextCompaction {
  id: string
  phase: "started" | "succeeded" | "failed" | "cancelled"
}

export interface RetryStatus {
  state: "waiting" | "recovered" | "cleared" | "exhausted"
  attempt: number
  max_attempts?: number
  error_kind: string
  retry_after_s?: number
}

/** One source family the answer used, in first-use order (TP-09). The server
 * decides family names and labels, so both apps show the same names. */
export interface TurnEndUsed {
  family: string
  label: string
  private: boolean
  calls: number
  errors: number
}

/** The frame that closes an assistant turn. ``used`` and ``other_steps`` are
 * optional: turns that ran no tools omit them, as do older runtimes that
 * predate the "Used:" line, and clients show no line in those cases. */
export interface TurnEnd {
  event: "turn_end"
  chat_id: string
  turn_id?: string
  used?: TurnEndUsed[]
  other_steps?: number
}

export type NotificationEvent =
  | ({ event: "retry_status"; chat_id: string; turn_id?: string } & RetryStatus)
  | ({ event: "recovery_state"; chat_id: string; turn_id?: string } & RecoveryState)
  | {
      event: "context_compaction"
      chat_id: string
      turn_id?: string
      compaction_id: string
      phase: ContextCompaction["phase"]
    }

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function optional(value: unknown, type: "boolean" | "number" | "string"): boolean {
  return value === undefined || typeof value === type
}

export function isCompactionPhase(value: unknown): value is ContextCompaction["phase"] {
  return value === "started" || value === "succeeded" || value === "failed" || value === "cancelled"
}

export function isRecoveryState(value: unknown): value is RecoveryState {
  return isRecord(value)
    && typeof value.status === "string"
    && ["resuming", "awaiting_user", "recovered", "failed"].includes(value.status)
    && typeof value.recovery_id === "string"
    && optional(value.reason, "string")
    && optional(value.attempts, "number")
    && optional(value.can_continue, "boolean")
}

export function isTurnEndUsed(value: unknown): value is TurnEndUsed {
  return isRecord(value)
    && typeof value.family === "string"
    && typeof value.label === "string"
    && typeof value.private === "boolean"
    && typeof value.calls === "number"
    && Number.isInteger(value.calls)
    && value.calls >= 1
    && typeof value.errors === "number"
    && Number.isInteger(value.errors)
    && value.errors >= 0
    && value.errors <= value.calls
}

/** Validator for the ``turn_end`` frame. Unknown extra fields (usage,
 * goal_state, …) are tolerated: the frame carries them and clients that do
 * not care about them must still accept it. */
export function isTurnEnd(value: unknown): value is TurnEnd {
  return isRecord(value)
    && value.event === "turn_end"
    && typeof value.chat_id === "string"
    && optional(value.turn_id, "string")
    && (value.used === undefined
      || (Array.isArray(value.used) && value.used.length > 0 && value.used.every(isTurnEndUsed)))
    && (value.other_steps === undefined
      || (typeof value.other_steps === "number"
        && Number.isInteger(value.other_steps)
        && value.other_steps >= 1))
}

/** Undefined means another protocol family; null means a malformed notification. */
export function decodeNotification(value: unknown): NotificationEvent | null | undefined {
  if (!isRecord(value)) return undefined
  if (!["recovery_state", "context_compaction", "retry_status"].includes(String(value.event))) return undefined
  if (typeof value.chat_id !== "string" || !optional(value.turn_id, "string")) return null
  if (value.event === "retry_status") {
    return isRetryStatus(value) ? value as unknown as NotificationEvent : null
  }
  if (value.event === "recovery_state") {
    return isRecoveryState(value) ? value as unknown as NotificationEvent : null
  }
  return typeof value.compaction_id === "string" && value.compaction_id.length > 0
    && isCompactionPhase(value.phase) ? value as unknown as NotificationEvent : null
}

function isRetryStatus(value: Record<string, unknown>): boolean {
  return typeof value.state === "string"
    && ["waiting", "recovered", "cleared", "exhausted"].includes(value.state)
    && typeof value.attempt === "number"
    && Number.isInteger(value.attempt)
    && value.attempt >= 1
    && (value.max_attempts === undefined
      || (typeof value.max_attempts === "number"
        && Number.isInteger(value.max_attempts)
        && value.max_attempts >= value.attempt))
    && typeof value.error_kind === "string"
    && (value.retry_after_s === undefined
      || (typeof value.retry_after_s === "number"
        && Number.isFinite(value.retry_after_s)
        && value.retry_after_s >= 0))
}

/** A terminal history row must not regress when an older live event arrives. */
export function acceptsCompactionPhase(
  current: ContextCompaction["phase"] | undefined,
  incoming: ContextCompaction["phase"],
): boolean {
  return current === undefined || (current === "started" && incoming !== "started")
}
