import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronLeft, Loader2, RefreshCw } from "lucide-react";

import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchSettings, switchModel, updateSettings } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";
import type { ModelSwitchTarget, SettingsPayload } from "@/lib/types";

interface SettingsViewProps {
  theme: "light" | "dark";
  onToggleTheme: () => void;
  onBackToChat: () => void;
  onModelNameChange: (modelName: string | null) => void;
}

export function SettingsView({
  onBackToChat,
  onModelNameChange,
}: SettingsViewProps) {
  const { token } = useClient();
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [switchingTarget, setSwitchingTarget] = useState<ModelSwitchTarget | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    model: "",
    provider: "auto",
  });

  const applyPayload = useCallback((payload: SettingsPayload) => {
    setSettings(payload);
    setForm({
      model: payload.agent.model,
      provider: payload.agent.provider,
    });
    onModelNameChange(payload.model_runtime?.active_model || payload.agent.model || null);
  }, [onModelNameChange]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchSettings(token)
      .then((payload) => {
        if (!cancelled) {
          applyPayload(payload);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError((err as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [applyPayload, token]);

  useEffect(() => {
    if (settings?.model_runtime?.status !== "switching") return;
    const id = window.setInterval(() => {
      fetchSettings(token)
        .then((payload) => {
          applyPayload(payload);
          if (payload.model_runtime?.status !== "switching") {
            setSwitchingTarget(null);
          }
        })
        .catch(() => {
          // The gateway restarts during a switch. Keep polling quietly.
        });
    }, 5_000);
    return () => window.clearInterval(id);
  }, [applyPayload, settings?.model_runtime?.status, token]);

  const dirty = useMemo(() => {
    if (!settings) return false;
    return (
      form.model !== settings.agent.model ||
      form.provider !== settings.agent.provider
    );
  }, [form, settings]);

  const save = async () => {
    if (!dirty || saving) return;
    setSaving(true);
    try {
      const payload = await updateSettings(token, form);
      applyPayload(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const switchRuntime = async (target: ModelSwitchTarget) => {
    if (switchingTarget) return;
    setSwitchingTarget(target);
    try {
      const runtime = await switchModel(token, target);
      setSettings((prev) => prev ? { ...prev, model_runtime: runtime } : prev);
      onModelNameChange(runtime.active_model ?? null);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
      setSwitchingTarget(null);
    }
  };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto bg-background">
      <main className="mx-auto w-full max-w-[1000px] px-6 py-6">
        <button
          type="button"
          onClick={onBackToChat}
          className="mb-4 inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="h-3.5 w-3.5" />
          Back to chat
        </button>

        <h1 className="mb-6 text-base font-semibold tracking-tight">General</h1>

        {loading ? (
          <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            Loading settings...
          </div>
        ) : error ? (
          <SettingsGroup>
            <SettingsRow title="Could not load settings">
              <span className="max-w-[520px] text-sm text-muted-foreground">{error}</span>
            </SettingsRow>
          </SettingsGroup>
        ) : settings ? (
          <SettingsSection
            form={form}
            setForm={setForm}
            settings={settings}
            dirty={dirty}
            saving={saving}
            switchingTarget={switchingTarget}
            onSave={save}
            onSwitchModel={switchRuntime}
          />
        ) : null}
      </main>
    </div>
  );
}

function SettingsSection({
  form,
  setForm,
  settings,
  dirty,
  saving,
  switchingTarget,
  onSave,
  onSwitchModel,
}: {
  form: {
    model: string;
    provider: string;
  };
  setForm: React.Dispatch<React.SetStateAction<{
    model: string;
    provider: string;
  }>>;
  settings: SettingsPayload;
  dirty: boolean;
  saving: boolean;
  switchingTarget: ModelSwitchTarget | null;
  onSave: () => void;
  onSwitchModel: (target: ModelSwitchTarget) => void;
}) {
  const runtime = settings.model_runtime;
  const isSwitching = runtime?.status === "switching" || switchingTarget !== null;
  const active = runtime?.active_model || settings.agent.model;
  return (
    <div className="space-y-7">
      <section>
        <h2 className="mb-2 px-2 text-xs font-medium text-muted-foreground">AI</h2>
        <SettingsGroup>
          <SettingsRow title="Provider">
            <select
              value={form.provider}
              onChange={(event) => setForm((prev) => ({ ...prev, provider: event.target.value }))}
              className={cn(
                "h-8 w-[210px] rounded-md border border-input bg-background px-2 text-sm",
                "outline-none transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring",
              )}
            >
              {settings.providers.map((provider) => (
                <option key={provider.name} value={provider.name}>
                  {provider.label}
                </option>
              ))}
            </select>
          </SettingsRow>

          <SettingsRow title="Model">
            <Input
              value={form.model}
              onChange={(event) => setForm((prev) => ({ ...prev, model: event.target.value }))}
              className="h-8 w-[280px]"
            />
          </SettingsRow>

          <SettingsRow title="Active backend">
            <div className="flex flex-col items-end gap-2">
              <div className="flex items-center gap-2 text-sm">
                {isSwitching ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                ) : null}
                <span className="font-medium">{active}</span>
                {runtime?.engine ? (
                  <span className="text-xs text-muted-foreground">{runtime.engine}</span>
                ) : null}
              </div>
              {runtime?.error ? (
                <div className="max-w-[360px] text-right text-xs text-destructive">
                  {runtime.error}
                </div>
              ) : null}
              <div className="flex gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant={active === "qwen3.6-35b" ? "secondary" : "outline"}
                  disabled={isSwitching}
                  onClick={() => onSwitchModel("qwen")}
                >
                  {switchingTarget === "qwen" ? (
                    <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                  ) : null}
                  Use Qwen
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={active === "MiniMax-M2.5" ? "secondary" : "outline"}
                  disabled={isSwitching}
                  onClick={() => onSwitchModel("minimax")}
                >
                  {switchingTarget === "minimax" ? (
                    <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                  ) : null}
                  Use MiniMax
                </Button>
              </div>
            </div>
          </SettingsRow>

          {(dirty || saving || settings.requires_restart) ? (
            <SettingsFooter
              dirty={dirty}
              saving={saving}
              saved={settings.requires_restart && !dirty}
              onSave={onSave}
            />
          ) : null}
        </SettingsGroup>
      </section>

      <section>
        <h2 className="mb-2 px-2 text-xs font-medium text-muted-foreground">Interface</h2>
        <SettingsGroup>
          <SettingsRow title="Language">
            <LanguageSwitcher />
          </SettingsRow>
        </SettingsGroup>
      </section>
    </div>
  );
}

function SettingsGroup({ children }: { children: React.ReactNode }) {
  return (
    <div className="overflow-hidden rounded-xl border border-border/60 bg-card/80">
      <div className="divide-y divide-border/50">{children}</div>
    </div>
  );
}

function SettingsRow({
  title,
  children,
}: {
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex min-h-[52px] flex-col gap-3 px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="text-sm font-medium leading-5">{title}</div>
      </div>
      {children ? <div className="shrink-0 sm:ml-6">{children}</div> : null}
    </div>
  );
}

function SettingsFooter({
  dirty,
  saving,
  saved,
  onSave,
}: {
  dirty: boolean;
  saving: boolean;
  saved: boolean;
  onSave: () => void;
}) {
  return (
    <div className="flex min-h-[52px] items-center justify-between gap-4 px-3 py-2.5">
      <div className="text-sm text-muted-foreground">
        {saved ? "Saved. Restart nanobot to apply." : "Unsaved changes."}
      </div>
      <Button size="sm" variant="outline" onClick={onSave} disabled={!dirty || saving}>
        {saving ? "Saving" : "Save"}
      </Button>
    </div>
  );
}
