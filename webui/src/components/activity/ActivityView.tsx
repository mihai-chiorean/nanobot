import {
  AlertCircle,
  BriefcaseBusiness,
  ChevronLeft,
  Clock3,
  Download,
  Loader2,
  RefreshCcw,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { useWork } from "@/hooks/useWork";
import { relativeTime, shortChatId } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { InboundEvent, WorkStatus, WorkTask } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

interface ActivityViewProps {
  onBackToChat: () => void;
  onOpenChat: (key: string) => void;
}

const STATUS_COPY: Record<WorkStatus, { label: string; className: string }> = {
  scheduled: {
    label: "Scheduled",
    className: "bg-indigo-500/10 text-indigo-700 dark:text-indigo-300",
  },
  queued: {
    label: "Queued",
    className: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  },
  running: {
    label: "Running",
    className: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  },
  waiting: {
    label: "Needs input",
    className: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
  },
  succeeded: {
    label: "Done",
    className: "bg-muted text-muted-foreground",
  },
  failed: {
    label: "Failed",
    className: "bg-destructive/10 text-destructive",
  },
  cancelled: {
    label: "Cancelled",
    className: "bg-muted text-muted-foreground",
  },
  interrupted: {
    label: "Interrupted",
    className: "bg-destructive/10 text-destructive",
  },
};

function StatusPill({ status }: { status: WorkStatus }) {
  const meta = STATUS_COPY[status];
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md px-2 py-0.5 text-[11px] font-medium",
        meta.className,
      )}
    >
      {meta.label}
    </span>
  );
}

function taskTitle(task: WorkTask): string {
  const title = task.title.trim() || task.prompt_preview.trim();
  if (title) return title.length > 82 ? `${title.slice(0, 79)}...` : title;
  return `Task ${shortChatId(task.task_id)}`;
}

function WorkRow({
  task,
  selected,
  onSelect,
}: {
  task: WorkTask;
  selected: boolean;
  onSelect: (task: WorkTask) => void;
}) {
  const subtitle = task.result_summary || task.error || task.prompt_preview;
  const artifactLabel = task.artifact_count === 1 ? "artifact" : "artifacts";
  return (
    <button
      type="button"
      onClick={() => onSelect(task)}
      className={cn(
        "flex w-full min-w-0 flex-col gap-2 border-b border-border/50 px-4 py-3 text-left transition-colors",
        selected ? "bg-accent/45" : "hover:bg-accent/35",
      )}
    >
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{taskTitle(task)}</div>
          <div className="mt-0.5 truncate text-xs text-muted-foreground">
            {subtitle || "No summary yet"}
          </div>
        </div>
        <StatusPill status={task.status} />
      </div>
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <Clock3 className="h-3 w-3" />
        <span>{relativeTime(task.updated_at || task.created_at) || "No timestamp"}</span>
        <span aria-hidden>·</span>
        <span>{task.artifact_count} {artifactLabel}</span>
        {task.mode === "scheduled_plan" ? (
          <>
            <span aria-hidden>·</span>
            <span>plan</span>
          </>
        ) : null}
      </div>
    </button>
  );
}

function WorkDetail({
  task,
  onOpenChat,
  onReply,
  onCancel,
}: {
  task: WorkTask | null;
  onOpenChat: (key: string) => void;
  onReply: (taskId: string, content: string) => void;
  onCancel: (taskId: string) => void;
}) {
  const [reply, setReply] = useState("");

  if (!task) {
    return (
      <div className="flex h-full min-h-[320px] items-center justify-center px-6 text-center text-sm text-muted-foreground">
        Select a task to inspect its steps, artifacts, and linked chat.
      </div>
    );
  }

  const steps = task.steps ?? [];
  const artifacts = task.artifacts ?? [];
  const terminal = ["succeeded", "failed", "cancelled", "interrupted"].includes(task.status);
  const isScheduledPlan = task.mode === "scheduled_plan";

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h2 className="truncate text-base font-semibold">{taskTitle(task)}</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {task.model || "Local model"} · {shortChatId(task.chat_id)}
          </p>
        </div>
        <StatusPill status={task.status} />
      </div>

      {task.error ? (
        <div className="mt-4 flex gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          <AlertCircle className="mt-0.5 h-4 w-4 flex-none" />
          <span>{task.error}</span>
        </div>
      ) : null}

      <div className="mt-5">
        <div className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
          {isScheduledPlan ? "Plan" : "Steps"}
        </div>
        {isScheduledPlan ? (
          <div className="rounded-md border border-border/60 px-3 py-4 text-sm text-muted-foreground">
            This scheduled plan will create a Work run when its cron job fires.
            The plan artifact below contains the exact goal, schedule, tools, and instructions.
          </div>
        ) : steps.length === 0 ? (
          <div className="rounded-md border border-border/60 px-3 py-4 text-sm text-muted-foreground">
            No explicit steps yet. Tool activity will appear as the task runs.
          </div>
        ) : (
          <div className="overflow-hidden rounded-md border border-border/60">
            {steps.map((step) => (
              <div key={step.step_id} className="border-b border-border/50 px-3 py-3 last:border-b-0">
                <div className="flex items-center justify-between gap-3">
                  <div className="truncate text-sm font-medium">{step.title}</div>
                  <span className="text-[11px] text-muted-foreground">{step.status}</span>
                </div>
                {step.summary ? (
                  <div className="mt-1 text-xs text-muted-foreground">{step.summary}</div>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="mt-5">
        <div className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
          Artifacts
        </div>
        {artifacts.length === 0 ? (
          <div className="rounded-md border border-border/60 px-3 py-4 text-sm text-muted-foreground">
            No artifacts published yet.
          </div>
        ) : (
          <div className="overflow-hidden rounded-md border border-border/60">
            {artifacts.map((artifact) => (
              <a
                key={artifact.artifact_id}
                href={artifact.url}
                className="flex items-center justify-between gap-3 border-b border-border/50 px-3 py-3 text-sm hover:bg-accent/35 last:border-b-0"
              >
                <span className="min-w-0 truncate">{artifact.name}</span>
                <Download className="h-4 w-4 flex-none text-muted-foreground" />
              </a>
            ))}
          </div>
        )}
      </div>

      <div className="mt-5 flex flex-wrap gap-2">
        <Button
          type="button"
          variant="secondary"
          onClick={() => onOpenChat(task.session_key)}
        >
          Open linked chat
        </Button>
        {!terminal && !isScheduledPlan ? (
          <Button
            type="button"
            variant="ghost"
            onClick={() => onCancel(task.task_id)}
          >
            Cancel
          </Button>
        ) : null}
      </div>

      {task.status === "waiting" ? (
        <form
          className="mt-4 flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const trimmed = reply.trim();
            if (!trimmed) return;
            onReply(task.task_id, trimmed);
            setReply("");
          }}
        >
          <input
            value={reply}
            onChange={(event) => setReply(event.target.value)}
            placeholder="Reply to this task"
            className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-foreground/20"
          />
          <Button type="submit" disabled={!reply.trim()}>
            Send
          </Button>
        </form>
      ) : null}
    </div>
  );
}

export function ActivityView({ onBackToChat, onOpenChat }: ActivityViewProps) {
  const { client } = useClient();
  const { tasks, loading, error, refresh, loadTask } = useWork();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedTask, setSelectedTask] = useState<WorkTask | null>(null);

  const selectedFromList = useMemo(
    () => tasks.find((task) => task.task_id === selectedId) ?? null,
    [selectedId, tasks],
  );

  useEffect(() => {
    if (!selectedId && tasks[0]) {
      setSelectedId(tasks[0].task_id);
    }
  }, [selectedId, tasks]);

  useEffect(() => {
    if (!selectedId) return;
    void loadTask(selectedId).then((task) => {
      if (task) setSelectedTask(task);
    });
  }, [loadTask, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    return client.onWork(selectedId, (ev: InboundEvent) => {
      if (ev.event === "work.event" || ev.event === "work.created") {
        void loadTask(selectedId).then((task) => {
          if (task) setSelectedTask(task);
        });
        void refresh();
      }
    });
  }, [client, loadTask, refresh, selectedId]);

  const effectiveSelected = selectedTask?.task_id === selectedId
    ? selectedTask
    : selectedFromList;

  return (
    <div className="min-h-0 flex-1 overflow-hidden bg-background">
      <main className="mx-auto flex h-full w-full max-w-[1200px] flex-col px-4 py-5 sm:px-6 sm:py-6">
        <div className="mb-4 flex items-center justify-between gap-3">
          <button
            type="button"
            onClick={onBackToChat}
            className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground"
          >
            <ChevronLeft className="h-3.5 w-3.5" />
            Back to chat
          </button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={() => void refresh()}
            className="h-8 w-8"
            aria-label="Refresh work"
          >
            <RefreshCcw className="h-4 w-4" />
          </Button>
        </div>

        <div className="mb-5">
          <div className="flex items-center gap-2">
            <BriefcaseBusiness className="h-4 w-4 text-muted-foreground" />
            <h1 className="text-base font-semibold tracking-tight">Work</h1>
          </div>
          <p className="mt-1 max-w-[34rem] text-sm text-muted-foreground">
            Background tasks running on the private Spark appliance.
          </p>
        </div>

        <section className="grid min-h-0 flex-1 overflow-hidden rounded-lg border border-border/60 bg-card/60 md:grid-cols-[minmax(280px,380px)_1fr]">
          <div className="min-h-[220px] overflow-y-auto border-b border-border/60 md:border-b-0 md:border-r">
            {loading ? (
              <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Loading work...
              </div>
            ) : error ? (
              <div className="px-4 py-10 text-sm text-muted-foreground">
                Could not load work: {error}
              </div>
            ) : tasks.length === 0 ? (
              <div className="px-4 py-10 text-sm text-muted-foreground">
                No background work yet.
              </div>
            ) : (
              tasks.map((task) => (
                <WorkRow
                  key={task.task_id}
                  task={task}
                  selected={task.task_id === selectedId}
                  onSelect={(next) => setSelectedId(next.task_id)}
                />
              ))
            )}
          </div>
          <WorkDetail
            task={effectiveSelected}
            onOpenChat={onOpenChat}
            onReply={(taskId, content) => {
              client.sendWorkMessage(taskId, content);
              void refresh();
            }}
            onCancel={(taskId) => {
              client.cancelWork(taskId);
              void refresh();
            }}
          />
        </section>
      </main>
    </div>
  );
}
